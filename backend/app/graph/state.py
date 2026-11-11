"""Workflow state (M3 S2; M3_PLAN §4–§5).

`KYCState` is the LangGraph state: a `TypedDict` whose values are **JSON-safe** (str,
int, bool, None, lists and dicts). Nested values are the `model_dump(mode="json")` of the
Pydantic models below. Nodes read them back through the models (`read`/`read_list`) and
`validate_state` checks a whole state. Plain JSON keeps checkpoints portable: the Postgres
checkpointer (S6) never has to deserialise application classes.

What never goes into state: secrets, binaries, prompts, raw model output, or text the
API screening blocked. `query` holds only the screened text (M3_PLAN §6).

Two status vocabularies, never merged (M3_PLAN §5):
- `RunStatus`: the execution lifecycle of one graph run (stored on `graph_runs` in S6).
- `Outcome`: exactly one per report. Every outcome requires human review; no value means
  approval or rejection of anything.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict, TypeVar, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.schemas.taxonomy import (
    AccountType,
    EntityType,
    EvidenceEligibility,
    ResidencyStatus,
    SourceStatus,
    TextReliability,
)

M = TypeVar("M", bound=BaseModel)


class SecurityStatus(StrEnum):
    SAFE = "safe"
    BLOCKED = "blocked"
    REVIEW = "review"  # not processed in M3: reported as BLOCKED (M4 makes a review case)


class Outcome(StrEnum):
    """The single outcome of a report, in precedence order (highest first)."""

    BLOCKED = "BLOCKED"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    DEGRADED = "DEGRADED"
    CHECKLIST_REVIEW_REQUIRED = "CHECKLIST_REVIEW_REQUIRED"


OUTCOME_PRECEDENCE: tuple[Outcome, ...] = tuple(Outcome)


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    COMPLETED = "completed"
    FAILED = "failed"  # unhandled error after bounded retries; no report
    ABANDONED = "abandoned"  # lease expired and taken over by /retry, or session expired


class DataClass(StrEnum):
    SYNTHETIC_DEMO = "synthetic_demo"
    UNSPECIFIED = "unspecified"


class LinkKind(StrEnum):
    """How a form field relates to a policy passage. A relationship, never an obligation
    (M3_PLAN §7): links are not citations and never establish MANDATORY."""

    SAME_CHUNK = "same_chunk"
    SAME_SECTION = "same_section"
    TOPIC_MATCH = "topic_match"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- request and screening ------------------------------------------------------------


class ScreenRecord(_Model):
    """What screening did: categories and counts only, never the matched text."""

    stage: Literal["messages", "resume", "input_security"]
    action: Literal["allow", "redact", "review", "block"]
    categories: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ClarificationAnswer(_Model):
    """A structured clarification answer (enum values only; no free text, M3 D7)."""

    field: Literal["entity_type", "residency", "account_type"]
    value: str = Field(min_length=1, max_length=64)
    round: int = Field(ge=1, le=2)


# --- intake ---------------------------------------------------------------------------


class ProfileValue(_Model):
    """A value the user stated or answered, or nothing. Never an inferred default.

    The two sources stay distinguishable (M3 S3):
    - `user_stated`: read from the user's question by the intake model. It must carry
      `evidence_span`, the exact words of the (screened) question that state it; the
      intake node checks the span really occurs there. It is still an interpretation.
    - `user_answered`: a structured clarification answer chosen by the user. No span.
    """

    value: str | None = None
    source: Literal["user_stated", "user_answered", "unknown"] = "unknown"
    evidence_span: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _known_iff_valued(self) -> "ProfileValue":
        if (self.value is None) != (self.source == "unknown"):
            raise ValueError("a value must have a user source, and only a value may")
        if (self.evidence_span is not None) != (self.source == "user_stated"):
            raise ValueError("exactly the user_stated values carry an evidence span")
        return self


class EntityProfile(_Model):
    entity_type: ProfileValue = ProfileValue()
    residency: ProfileValue = ProfileValue()
    account_type: ProfileValue = ProfileValue()

    @model_validator(mode="after")
    def _taxonomy(self) -> "EntityProfile":
        if self.entity_type.value is not None:
            EntityType(self.entity_type.value)
        if self.residency.value is not None:
            ResidencyStatus(self.residency.value)
        if self.account_type.value is not None:
            AccountType(self.account_type.value)
        return self


# --- retrieval and forms ----------------------------------------------------------------


class RetrievalFilters(_Model):
    """The JSON form of `app.retrieval.search.SearchFilters`."""

    include_context_only: bool = False
    mandatory_evidence_only: bool = False
    banks: list[str] = Field(default_factory=list)
    document_keys: list[str] = Field(default_factory=list)
    entity_terms: list[str] = Field(default_factory=list)


class EvidenceItem(_Model):
    """One retrieved chunk with its provenance. `text` is public corpus text and is
    untrusted: it is always delimited when placed in a prompt."""

    evidence_id: str  # = chunk_id
    document_id: str
    document_key: str
    bank: Literal["RBI", "HDFC"]
    page: int = Field(ge=1)
    page_end: int | None = Field(default=None, ge=1)
    paragraph: str | None = None
    section: str | None = None
    source_status: SourceStatus
    evidence_eligibility: EvidenceEligibility
    text_reliability: TextReliability
    can_support_mandatory: bool
    rank: int = Field(ge=1)
    score_kind: str
    text: str
    expanded_parent_id: str | None = None

    @model_validator(mode="after")
    def _mandatory_rule(self) -> "EvidenceItem":
        expected = (
            self.evidence_eligibility == EvidenceEligibility.PRIMARY
            and self.text_reliability != TextReliability.OCR_UNCERTAIN
        )
        if self.can_support_mandatory != expected:
            raise ValueError("can_support_mandatory must equal primary AND NOT ocr_uncertain")
        return self


class RetrievalMeta(_Model):
    mode: str
    degraded: list[str] = Field(default_factory=list)  # e.g. ["bm25"], ["rerank"]
    failed: bool = False  # every retriever failed (RetrievalError)
    failure_kinds: list[str] = Field(default_factory=list)  # exception class names only
    timings_ms: dict[str, float] = Field(default_factory=dict)
    score_kind: str | None = None


class FormFieldRef(_Model):
    """The subset of a stored form field the workflow uses (never `mandatory_under_policy`)."""

    printed_label: str
    label: str
    page_number: int = Field(ge=1)
    chunk_id: str | None = None
    required_as_shown: bool | None = None
    verification_status: str


class FormRef(_Model):
    form_key: str
    method: Literal["m1_chunks", "gemini_multimodal"]
    version: int = Field(ge=1)
    row_id: str
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: list[FormFieldRef] = Field(default_factory=list)


class FieldEvidenceLink(_Model):
    form_key: str
    field_label: str
    field_chunk_id: str | None = None
    evidence_id: str
    link_kind: LinkKind
    relationship_only: Literal[True] = True  # never a citation, never an obligation


# --- synthesis and outcome --------------------------------------------------------------


class LLMCallRecord(_Model):
    """One LLM call: provenance and cost only (no prompt, no output text, no key)."""

    node: str
    provider: str
    model_id: str
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit: bool = False
    attempts: int = Field(ge=0)
    result: Literal["ok", "invalid", "error"]


class Degradation(_Model):
    component: Literal["retrieval", "rerank", "forms", "llm", "rate_limiter", "events"]
    reason: str = Field(max_length=200)  # a code or exception class name, never content


class ValidationFinding(_Model):
    kind: str
    requirement_id: str | None = None
    detail: str = Field(max_length=500)


# --- the state ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Validates:
    """`Annotated` marker: the JSON value is `model.model_dump(mode="json")` (or a list of
    them when `each`). Deliberately not callable, so LangGraph never mistakes it for a
    reducer; every key is last-value-wins."""

    model: type[BaseModel]
    each: bool = False


@dataclass(frozen=True)
class OneOf:
    """`Annotated` marker: the string must be a value of `enum`."""

    enum: type[StrEnum]


JSON = dict[str, Any]


class KYCState(TypedDict, total=False):
    # request
    session_id: str
    run_id: str
    # `synthetic_demo` only for sessions created as such (S6); it is what lets the intake
    # node offer an allowlisted synthetic input to the opt-in LLM cache (M3 D8).
    data_class: Annotated[str, OneOf(DataClass)]
    query: str  # screened text only
    clarification_rounds: int
    clarification_answers: Annotated[list[JSON], Validates(ClarificationAnswer, each=True)]
    screening: Annotated[list[JSON], Validates(ScreenRecord, each=True)]
    # security
    security_status: Annotated[str, OneOf(SecurityStatus)]
    security_reasons: list[str]
    # intake
    entity_profile: Annotated[JSON, Validates(EntityProfile)]
    missing_fields: list[str]
    ambiguities: list[str]
    intake_complete: bool
    # retrieval
    retrieval_query: str
    filters: Annotated[JSON, Validates(RetrievalFilters)]
    evidence: Annotated[list[JSON], Validates(EvidenceItem, each=True)]
    retrieval_meta: Annotated[JSON, Validates(RetrievalMeta)]
    # forms
    form_analysis_required: bool
    forms: Annotated[list[JSON], Validates(FormRef, each=True)]
    form_flags: list[str]
    # association
    associations: Annotated[list[JSON], Validates(FieldEvidenceLink, each=True)]
    # synthesis (the ChecklistDraft model is defined with the synthesis node in S5)
    draft: JSON | None
    synthesis_attempts: int
    llm_calls: Annotated[list[JSON], Validates(LLMCallRecord, each=True)]
    # outcome
    degradations: Annotated[list[JSON], Validates(Degradation, each=True)]
    validation_findings: Annotated[list[JSON], Validates(ValidationFinding, each=True)]
    outcome: Annotated[str, OneOf(Outcome)]
    report_id: str | None


class KYCInput(TypedDict, total=False):
    """What a caller may put into a new run: exactly what `app.graph.inputs.start_input`
    produces (M3 S3.1). The graph's `input_schema`, so every other key in the input
    (a pre-filled profile, evidence, a security status, an outcome) is dropped and can
    never short-cut a node."""

    session_id: str
    run_id: str
    data_class: Annotated[str, OneOf(DataClass)]
    query: str
    clarification_rounds: int
    screening: Annotated[list[JSON], Validates(ScreenRecord, each=True)]


STATE_KEYS: frozenset[str] = frozenset(KYCState.__annotations__)
INPUT_KEYS: frozenset[str] = frozenset(KYCInput.__annotations__)


def dump(model: BaseModel) -> JSON:
    """The JSON-safe form stored in state."""
    return model.model_dump(mode="json")


def dump_list(models: list[M]) -> list[JSON]:
    return [dump(m) for m in models]


def read(state: Mapping[str, Any], key: str, model: type[M]) -> M | None:
    """A nested value validated back into its model (None when absent)."""
    raw = state.get(key)
    return None if raw is None else model.model_validate(raw)


def read_list(state: Mapping[str, Any], key: str, model: type[M]) -> list[M]:
    return [model.model_validate(v) for v in state.get(key) or []]


def validate_state(state: Mapping[str, Any]) -> None:
    """Raise `ValueError` unless `state` is a well-formed, JSON-safe `KYCState`:
    known keys only, base types correct, nested values valid for their models, enum
    strings in their vocabularies, and a lossless JSON round trip."""
    unknown = set(state) - STATE_KEYS
    if unknown:
        raise ValueError(f"unknown state keys: {sorted(unknown)}")
    hints = get_type_hints(KYCState, include_extras=True)
    for key, value in state.items():
        hint = hints[key]
        meta = getattr(hint, "__metadata__", ())
        base = hint.__origin__ if meta else hint
        try:
            TypeAdapter(base).validate_python(value, strict=True)
            for marker in meta:
                if isinstance(marker, Validates):
                    items = value if marker.each else [value]
                    for item in items:
                        marker.model.model_validate(item)
                elif isinstance(marker, OneOf):
                    marker.enum(value)
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise ValueError(f"invalid state value for {key!r}: {exc}") from exc
    try:
        round_tripped = json.loads(json.dumps(dict(state)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"state is not JSON-serialisable: {type(exc).__name__}") from exc
    if round_tripped != dict(state):
        raise ValueError("state is not JSON round-trippable")
