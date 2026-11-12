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
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, TypedDict, TypeVar, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.schemas.taxonomy import (
    AccountType,
    Applicability,
    DocumentType,
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
    # "entity": the chunk is tagged with the customer's entity type. "entity_group": found
    # only through the entity's taxonomy group (docs/15: a retrieval convenience); such
    # evidence can never on its own establish MANDATORY (enforced in S5). M3 S4.
    match_scope: Literal["entity", "entity_group"] = "entity"
    text_truncated: bool = False

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
    # M3 S4 ("no evidence, no answer"): which of four distinct results occurred.
    # found: entity-scope primary-capable evidence was RETRIEVED (not verified as relevant
    #   or correct: scores are uncalibrated; S5 judges support). partial: eligible evidence,
    #   but none entity-scope primary-capable. none: the search ran and returned nothing
    #   eligible. retrieval_failed: a technical failure. Absence is a gap, never "not required".
    evidence_status: Literal["found", "partial", "none", "retrieval_failed"] | None = None
    gaps: list[str] = Field(default_factory=list)  # codes only, never a negative finding
    dropped_count: int = Field(default=0, ge=0)  # dense hits with no Postgres row (not cited)


class FormFieldRef(_Model):
    """The subset of a stored form field the workflow uses (never `mandatory_under_policy`)."""

    printed_label: str
    label: str
    section: str | None = None
    page_number: int = Field(ge=1)
    chunk_id: str | None = None
    required_as_shown: bool | None = None
    verification_status: str


class FormRef(_Model):
    """A stored form schema the run used: which row (method, version, id, hash), a bounded
    subset of its fields, and reviewer-relevant flags. Nothing here is generated."""

    form_key: str
    method: Literal["m1_chunks", "gemini_multimodal"]
    version: int = Field(ge=1)
    row_id: str
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: list[FormFieldRef] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    section_chunk_ids: dict[str, list[str]] = Field(default_factory=dict)


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
    # "verification" (M4): the claim-verification stage could not run or finish.
    component: Literal[
        "retrieval", "rerank", "forms", "llm", "rate_limiter", "events", "verification"
    ]
    reason: str = Field(max_length=200)  # a code or exception class name, never content


class ValidationFinding(_Model):
    kind: str
    requirement_id: str | None = None
    detail: str = Field(max_length=500)


# --- validated checklist and report (M3 S5) -----------------------------------------------------

NOTICE: Final = "Decision support only. Not a KYC decision. Not claim-verified (M4)."
# Report v2 (M4): claims may have been checked against the cited text. Never a legal finding.
NOTICE_V2: Final = (
    "Decision support only. Not a KYC decision. Where shown, claims were checked against "
    "the cited text by an automated model; this is not a legal determination."
)
DISCLAIMER = (
    "Independent educational prototype. Not affiliated with, endorsed by, or an official "
    "system of HDFC Bank."
)


class Citation(_Model):
    """A cited stored chunk, copied from Postgres (never from model output)."""

    evidence_id: str
    document_key: str
    page: int = Field(ge=1)
    page_end: int | None = Field(default=None, ge=1)
    paragraph: str | None = None
    match_scope: Literal["entity", "entity_group"]
    evidence_eligibility: EvidenceEligibility
    text_reliability: TextReliability
    source_status: SourceStatus
    can_support_mandatory: bool


class RelatedField(_Model):
    """A form field linked (S4) to a cited passage: a relationship, never an obligation."""

    form_key: str
    field_label: str
    link_kind: LinkKind
    note: Literal["related form field (relationship only)"] = (
        "related form field (relationship only)"
    )


APPLICABILITY_STRICTNESS: Final[dict[Applicability, int]] = {
    Applicability.UNCLEAR: 0,
    Applicability.OPTIONAL: 1,
    Applicability.CONDITIONAL: 2,
    Applicability.MANDATORY: 3,
}


def never_upgraded(before: Applicability, after: Applicability) -> bool:
    """The invariant of M3 S5 and M4: a later stage may only keep or lower applicability."""
    return APPLICABILITY_STRICTNESS[after] <= APPLICABILITY_STRICTNESS[before]


# Requirement-level result of M4 claim verification (M4_PLAN §G). Absence of support is
# never "not required"; `not_verified` is a technical failure, distinct from `unsupported`.
RequirementVerdict = Literal[
    "supported",
    "supported_weaker_than_asserted",
    "partially_supported",
    "unsupported",
    "contradicted",
    "conflicting",
    "not_verified",
]
ExplanationStatus = Literal[
    "model_drafted_not_verified",  # M3 (report v1) and any v2 item not checked
    "verified_supported",
    "partially_verified",
    "unverified",
]


class ValidatedRequirement(_Model):
    """A requirement that survived `output_validation`. Its applicability is the
    validator's (never above the model's proposal); its explanation is model prose.
    M4 widens the two status fields; the M3 defaults are unchanged."""

    requirement_id: str
    name: str = Field(min_length=1, max_length=120)
    explanation: str = Field(max_length=600)
    explanation_status: ExplanationStatus = "model_drafted_not_verified"
    applicability: Applicability
    proposed_applicability: Applicability
    citations: list[Citation] = Field(min_length=1)
    related_form_fields: list[RelatedField] = Field(default_factory=list)
    uncertainty_label: str
    verification_status: Literal["not_claim_verified"] | RequirementVerdict = "not_claim_verified"

    @model_validator(mode="after")
    def _never_above_proposal(self) -> "ValidatedRequirement":
        if not never_upgraded(self.proposed_applicability, self.applicability):
            raise ValueError("applicability can never exceed the proposed applicability")
        return self


# --- M4 claim verification (M4_PLAN §D–§I) --------------------------------------------------
#
# Three questions stay separate: semantic support (does this text say this?), source
# eligibility/scope (may this source count?), and obligation (is it required?). Model scores
# are uncalibrated signals, never confidence. Every value here is a code, an id, an offset,
# a score or model prose already in the report; never user words.

NLI_LABELS: Final[tuple[str, str, str]] = ("entailment", "neutral", "contradiction")
"""Canonical order of stored logits/probabilities, whatever the model's own label order."""
_PROB_TOLERANCE = 1e-6


class ClaimType(StrEnum):
    POLICY_OBLIGATION = "policy_obligation"  # the only type that can bear MANDATORY
    FORM_REQUEST = "form_request"  # a form asks for it: never an obligation
    SUPPORTING_STATEMENT = "supporting_statement"  # a non-primary, non-form source says it
    EXPLANATORY_CLAIM = "explanatory_claim"  # a verbatim sentence of the model's explanation


class EvidenceRole(StrEnum):
    """What a cited chunk may support, derived from stored metadata (`evidence_role`)."""

    PRIMARY_POLICY = "primary_policy"
    FORM = "form"
    SUPPORTING_REFERENCE = "supporting_reference"


def evidence_role(document_type: DocumentType, eligibility: EvidenceEligibility) -> EvidenceRole:
    """Forms are always `form` (never primary, docs/15); other primary text is policy."""
    if document_type == DocumentType.FORM:
        return EvidenceRole.FORM
    if eligibility == EvidenceEligibility.PRIMARY:
        return EvidenceRole.PRIMARY_POLICY
    return EvidenceRole.SUPPORTING_REFERENCE


# Which evidence role may be judged for which claim type (M4_PLAN §D.1, §D.3). A form chunk
# is never scored against an obligation hypothesis.
ALLOWED_ROLES: Final[dict[ClaimType, frozenset[EvidenceRole]]] = {
    ClaimType.POLICY_OBLIGATION: frozenset({EvidenceRole.PRIMARY_POLICY}),
    ClaimType.FORM_REQUEST: frozenset({EvidenceRole.FORM}),
    ClaimType.SUPPORTING_STATEMENT: frozenset({EvidenceRole.SUPPORTING_REFERENCE}),
    ClaimType.EXPLANATORY_CLAIM: frozenset(EvidenceRole),
}

ClaimVerdictLabel = Literal["supported", "insufficient", "contradicted", "conflicting",
                            "not_verified"]  # fmt: skip
ReviewReason = Literal[
    "contradicted",
    "conflicting_sources",
    "unsupported",
    "mandatory_not_established",
    "explanation_overstates_source",
    "not_verified",
]


class AtomicClaim(_Model):
    """One checkable proposition. Typed claims come from a versioned template; explanatory
    claims are verbatim model sentences (never user words)."""

    claim_id: str = Field(min_length=1, max_length=64)
    requirement_id: str = Field(min_length=1, max_length=64)
    claim_type: ClaimType
    asserted_strength: Literal["unconditional", "conditional", "none"]
    text: str = Field(min_length=1, max_length=300)
    template_version: str | None = Field(default=None, min_length=1, max_length=32)
    source_sentence_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _shape(self) -> "AtomicClaim":
        explanatory = self.claim_type == ClaimType.EXPLANATORY_CLAIM
        if explanatory != (self.source_sentence_index is not None):
            raise ValueError("exactly the explanatory claims carry a source sentence index")
        if explanatory == (self.template_version is not None):
            raise ValueError("exactly the typed (non-explanatory) claims carry a template version")
        obligation = self.claim_type == ClaimType.POLICY_OBLIGATION
        if obligation and self.asserted_strength == "none":
            raise ValueError("a policy obligation is unconditional or conditional")
        if self.claim_type in (ClaimType.FORM_REQUEST, ClaimType.SUPPORTING_STATEMENT) and (
            self.asserted_strength != "none"
        ):
            raise ValueError("form requests and supporting statements assert no obligation")
        return self


class StatementAnalysis(_Model):
    """Deterministic reading of one evidence window: a necessary condition, never sufficient."""

    modality: Literal["obligation", "permission", "conditional", "none"]
    polarity: Literal["affirmative", "negated", "ambiguous"]
    object_match: bool
    subject: str | None = Field(default=None, min_length=1, max_length=80)
    scope: Literal["match", "mismatch", "unknown"]
    cues: list[str] = Field(default_factory=list, max_length=20)  # cue codes, not text


def _finite_triplet(values: list[float], name: str) -> None:
    if len(values) != len(NLI_LABELS):
        raise ValueError(f"{name} must have exactly {len(NLI_LABELS)} values")
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"{name} must be finite")


class EvidenceJudgement(_Model):
    """One (claim, cited chunk) result. Provenance is copied from the stored chunk; scores
    are the best window's raw logits and their softmax, in `NLI_LABELS` order."""

    claim_id: str = Field(min_length=1, max_length=64)
    evidence_id: str
    document_key: str
    document_type: DocumentType
    page: int = Field(ge=1)
    page_end: int | None = Field(default=None, ge=1)
    # Offsets index into the stored `content` of `window_chunk_id`: the cited chunk itself
    # (None) or, for a child chunk, the parent paragraph it was expanded to (M4 S2).
    window_chunk_id: str | None = None
    window_start: int = Field(ge=0)
    window_end: int = Field(ge=1)
    evidence_role: EvidenceRole
    evidence_eligibility: EvidenceEligibility
    match_scope: Literal["entity", "entity_group"]
    text_reliability: TextReliability
    source_status: SourceStatus
    can_support_mandatory: bool
    analysis: StatementAnalysis | None = None
    # None when the pair was not scored (no model, failure, cap, window too long).
    logits: list[float] | None = None
    probs: list[float] | None = None
    score_semantics: Literal["softmax_of_logits_uncalibrated"] = "softmax_of_logits_uncalibrated"
    not_scored_reason: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _consistent(self) -> "EvidenceJudgement":
        if self.window_end <= self.window_start:
            raise ValueError("an evidence window must be non-empty")
        if self.page_end is not None and self.page_end < self.page:
            raise ValueError("page_end precedes page")
        if self.evidence_role != evidence_role(self.document_type, self.evidence_eligibility):
            raise ValueError("evidence_role must follow from document type and eligibility")
        expected = (
            self.evidence_eligibility == EvidenceEligibility.PRIMARY
            and self.text_reliability != TextReliability.OCR_UNCERTAIN
        )
        if self.can_support_mandatory != expected:
            raise ValueError("can_support_mandatory must equal primary AND NOT ocr_uncertain")
        if (self.logits is None) != (self.probs is None):
            raise ValueError("logits and probabilities are stored together or not at all")
        if (self.logits is None) == (self.not_scored_reason is None):
            raise ValueError("exactly the unscored judgements carry a not_scored_reason")
        if self.logits is not None and self.probs is not None:
            _finite_triplet(self.logits, "logits")
            _finite_triplet(self.probs, "probs")
            if any(p < 0 or p > 1 for p in self.probs):
                raise ValueError("probabilities must lie in [0, 1]")
            if abs(sum(self.probs) - 1.0) > _PROB_TOLERANCE:
                raise ValueError("probabilities must sum to 1")
        return self


class ClaimVerdict(_Model):
    claim_id: str = Field(min_length=1, max_length=64)
    verdict: ClaimVerdictLabel
    best_evidence_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _evidence_for_support(self) -> "ClaimVerdict":
        if self.verdict in ("supported", "conflicting") and self.best_evidence_id is None:
            raise ValueError("a supported or conflicting claim names its best evidence")
        if self.verdict == "not_verified" and self.best_evidence_id is not None:
            raise ValueError("an unverified claim has no best evidence")
        return self


def mandatory_established(
    claims: dict[str, AtomicClaim],
    verdicts: list[ClaimVerdict],
    judgements: list[EvidenceJudgement],
) -> bool:
    """M4_PLAN §E: MANDATORY needs, on ONE primary citation, a supported unconditional
    policy-obligation claim whose evidence passes the M3 gate and whose window reads as an
    affirmative obligation naming the item, with no entity-scope mismatch."""
    for v in verdicts:
        claim = claims[v.claim_id]
        if not (
            v.verdict == "supported"
            and claim.claim_type == ClaimType.POLICY_OBLIGATION
            and claim.asserted_strength == "unconditional"
        ):
            continue
        for j in judgements:
            a = j.analysis
            if (
                j.claim_id == v.claim_id
                and j.evidence_id == v.best_evidence_id
                and j.evidence_role == EvidenceRole.PRIMARY_POLICY
                and j.can_support_mandatory
                and j.source_status == SourceStatus.ACTIVE
                and j.match_scope == "entity"
                and j.probs is not None
                and a is not None
                and a.modality == "obligation"
                and a.polarity == "affirmative"
                and a.object_match
                and a.scope != "mismatch"
            ):
                return True
    return False


class RequirementVerification(_Model):
    """All of M4's work on one validated requirement, and its downgrade-only effect."""

    requirement_id: str = Field(min_length=1, max_length=64)
    # Empty only when the item asserts nothing checkable (M4 S2): then it is `not_verified`.
    claims: list[AtomicClaim] = Field(default_factory=list, max_length=12)
    judgements: list[EvidenceJudgement] = Field(default_factory=list, max_length=200)
    verdicts: list[ClaimVerdict] = Field(default_factory=list, max_length=12)
    requirement_verdict: RequirementVerdict
    applicability_before: Applicability
    applicability_after: Applicability
    review_flags: list[ReviewReason] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "RequirementVerification":
        if not never_upgraded(self.applicability_before, self.applicability_after):
            raise ValueError("claim verification can never upgrade applicability")
        if not self.claims and self.requirement_verdict != "not_verified":
            raise ValueError("a requirement with no checkable claim is not_verified")
        claims = {c.claim_id: c for c in self.claims}
        if len(claims) != len(self.claims):
            raise ValueError("claim ids must be unique")
        if any(c.requirement_id != self.requirement_id for c in self.claims):
            raise ValueError("every claim belongs to this requirement")
        if sorted(v.claim_id for v in self.verdicts) != sorted(claims):
            raise ValueError("exactly one verdict per claim")
        for j in self.judgements:
            claim = claims.get(j.claim_id)
            if claim is None:
                raise ValueError("a judgement refers to an unknown claim")
            if j.evidence_role not in ALLOWED_ROLES[claim.claim_type]:
                raise ValueError(
                    f"{j.evidence_role} evidence cannot be judged for a {claim.claim_type} claim"
                )
        judged = {(j.claim_id, j.evidence_id) for j in self.judgements}
        for v in self.verdicts:
            if v.best_evidence_id is not None and (v.claim_id, v.best_evidence_id) not in judged:
                raise ValueError("a verdict's best evidence must be one of its judgements")
        if len(set(self.review_flags)) != len(self.review_flags):
            raise ValueError("review flags must be unique")
        if self.applicability_after == Applicability.MANDATORY and not mandatory_established(
            claims, self.verdicts, self.judgements
        ):
            raise ValueError("MANDATORY needs a supported obligation on eligible primary evidence")
        weak = ("supported_weaker_than_asserted", "unsupported", "contradicted", "conflicting",
                "not_verified")  # fmt: skip
        if self.requirement_verdict in weak and self.applicability_after == Applicability.MANDATORY:
            raise ValueError(f"a {self.requirement_verdict} requirement cannot stay MANDATORY")
        return self


class VerificationMeta(_Model):
    """How verification ran: provenance and counts only (no text)."""

    status: Literal["ran", "partial", "not_configured", "failed"]
    model_id: str | None = None
    model_revision: str | None = None
    label_map: dict[str, str] | None = None  # the model's own id2label, as read at load time
    templates_version: str | None = None
    thresholds_version: str | None = None
    pairs_scored: int = Field(default=0, ge=0)
    pairs_capped: int = Field(default=0, ge=0)
    # M4 S6: the bounded source-conflict scan. `not_run` (NLI disabled) is never "no
    # conflicts found". The size counts UNIQUE passage pairs scored (each in both directions);
    # eligible pairs beyond the cap or unscoreable are counted as not scanned.
    contradiction_scan: Literal["not_run", "bounded", "failed"] = "not_run"
    contradiction_scan_size: int = Field(default=0, ge=0)
    contradiction_pairs_not_scanned: int = Field(default=0, ge=0)
    timings_ms: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _provenance(self) -> "VerificationMeta":
        if self.status in ("ran", "partial"):
            needed = (self.model_id, self.model_revision, self.label_map,
                      self.templates_version, self.thresholds_version)  # fmt: skip
            if any(v is None for v in needed):
                raise ValueError("a run records model, revision, label map and versions")
        if self.label_map is not None and set(self.label_map.values()) != set(NLI_LABELS):
            raise ValueError("the label map must name exactly entailment, neutral, contradiction")
        if self.status == "not_configured" and self.pairs_scored:
            raise ValueError("nothing is scored without a configured model")
        if self.status == "not_configured" and self.contradiction_scan != "not_run":
            raise ValueError("no conflict scan runs without a configured model")
        if self.contradiction_scan == "not_run" and (
            self.contradiction_scan_size or self.contradiction_pairs_not_scanned
        ):
            raise ValueError("a scan that did not run scored and skipped nothing")
        return self


ConflictResolution = Literal["unresolved", "superseded"]
# Structured codes only (M4 S6, D10): never exception text or free-form content.
ResolutionReason = Literal[
    "no_supersession_relation",
    "superseding_not_active",
    "version_review_not_confirmed",
    "relation_lookup_failed",
    "relation_incomplete",
    "supersession_confirmed",
]


class ConflictSide(_Model):
    """One side of a source conflict: provenance and window offsets only (no text)."""

    evidence_id: str = Field(min_length=1, max_length=64)
    document_key: str = Field(min_length=1, max_length=120)
    page: int = Field(ge=1)
    page_end: int | None = Field(default=None, ge=1)
    window_start: int = Field(ge=0)
    window_end: int = Field(ge=0)
    source_status: SourceStatus
    evidence_eligibility: EvidenceEligibility
    cited: bool  # cited by the requirement (else: other evidence retrieved in this run)

    @model_validator(mode="after")
    def _window(self) -> "ConflictSide":
        if self.window_end <= self.window_start:
            raise ValueError("a window has positive length")
        return self


class SourceConflict(_Model):
    """Two passages from different documents, both naming a requirement's item, that the
    model reads as contradicting each other (M4_PLAN §H). Both sides stay visible. Resolved
    only by a recorded, active, confirmed supersession (D10); never by date."""

    requirement_id: str = Field(min_length=1, max_length=64)
    side_a: ConflictSide
    side_b: ConflictSide
    detected_by: Literal["nli_contradiction"] = "nli_contradiction"
    max_contradiction: float = Field(ge=0.0, le=1.0)  # uncalibrated model signal
    resolution: ConflictResolution
    resolution_reason: ResolutionReason
    superseding_document_key: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "SourceConflict":
        if self.side_a.document_key == self.side_b.document_key:
            raise ValueError("a source conflict is between two different documents")
        if not (self.side_a.cited or self.side_b.cited):
            raise ValueError("a source conflict involves at least one cited passage")
        superseded = self.resolution == "superseded"
        if superseded != (self.resolution_reason == "supersession_confirmed"):
            raise ValueError("only a confirmed supersession resolves a conflict")
        keys = {self.side_a.document_key, self.side_b.document_key}
        if superseded != (self.superseding_document_key is not None) or (
            superseded and self.superseding_document_key not in keys
        ):
            raise ValueError("a resolved conflict names one of its two documents as superseding")
        return self


class UnverifiedItem(_Model):
    """A validated requirement whose claims the cited text did not support (decision D2):
    kept visible for review, out of the checklist. Never "not required"."""

    requirement_id: str
    name: str = Field(min_length=1, max_length=120)
    applicability_before: Applicability
    citations: list[Citation] = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)


class FormSummary(_Model):
    form_key: str
    method: Literal["m1_chunks", "gemini_multimodal"]
    version: int = Field(ge=1)
    flags: list[str] = Field(default_factory=list)
    fields_analysed: int = Field(ge=0)
    subset_only: bool  # True when `fields_capped`: the whole form was NOT analysed


class ProfileSummary(_Model):
    """A profile value and its source for the report (never the user's own words)."""

    value: str | None
    source: Literal["user_stated", "user_answered", "unknown"]


class ComplianceReport(_Model):
    """One report per run (M3_PLAN §5). Every report requires human review; no field can
    express an approval or rejection. Holds codes and stored-source citations only:
    never the question text, quoted user words or matched sensitive text.

    Versions (M4_PLAN §I): `report_version` 1 is every M3 report (stored rows have no such
    key, so it defaults to 1) and must keep validating unchanged; 2 is M4. A v1 report can
    carry no M4 field; a v2 report's verification fields must agree with each other."""

    report_version: Literal[1, 2] = 1
    report_id: str
    session_id: str | None = None
    run_id: str | None = None
    outcome: Outcome
    human_review_required: Literal[True] = True
    notice: Literal[
        "Decision support only. Not a KYC decision. Not claim-verified (M4).",
        "Decision support only. Not a KYC decision. Where shown, claims were checked against "
        "the cited text by an automated model; this is not a legal determination.",
    ] = NOTICE
    disclaimer: str = DISCLAIMER
    user_message: str
    profile: dict[str, ProfileSummary] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    evidence_status: Literal["found", "partial", "none", "retrieval_failed"] | None = None
    gaps: list[str] = Field(default_factory=list)
    checklist: list[ValidatedRequirement] | None = None
    dropped_requirements: int = Field(default=0, ge=0)
    unresolved_items: list[str] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)
    degradations: list[Degradation] = Field(default_factory=list)
    forms: list[FormSummary] = Field(default_factory=list)
    security_reasons: list[str] = Field(default_factory=list)
    verification: Literal[
        "not_claim_verified", "claim_check_ran", "claim_check_partial", "claim_check_failed"
    ] = "not_claim_verified"
    # M4 (report v2 only)
    claim_verification: list[RequirementVerification] | None = None
    verification_meta: VerificationMeta | None = None
    unverified_items: list[UnverifiedItem] = Field(default_factory=list)
    source_conflicts: list[SourceConflict] = Field(default_factory=list)  # M4 S6

    @model_validator(mode="after")
    def _disclaimer_present(self) -> "ComplianceReport":
        if self.disclaimer != DISCLAIMER or DISCLAIMER not in self.user_message:
            raise ValueError("every report and message carries the independence disclaimer")
        return self

    @model_validator(mode="after")
    def _version_rules(self) -> "ComplianceReport":
        items = self.checklist or []
        if self.report_version == 1:
            if self.notice != NOTICE or self.verification != "not_claim_verified":
                raise ValueError("a v1 report carries the M3 notice and no claim check")
            if self.claim_verification is not None or self.verification_meta is not None:
                raise ValueError("a v1 report carries no M4 verification fields")
            if self.unverified_items or any(
                i.verification_status != "not_claim_verified"
                or i.explanation_status != "model_drafted_not_verified"
                for i in items
            ):
                raise ValueError("v1 checklist items are not claim-verified")
            if self.source_conflicts:
                raise ValueError("a v1 report carries no source conflicts")
            return self
        if self.notice != NOTICE_V2:
            raise ValueError("a v2 report carries the M4 notice")
        meta = self.verification_meta
        expected = (
            "not_claim_verified"
            if meta is None or meta.status == "not_configured"
            else {"ran": "claim_check_ran", "partial": "claim_check_partial",
                  "failed": "claim_check_failed"}[meta.status]
        )  # fmt: skip
        if self.verification != expected:
            raise ValueError("`verification` must agree with verification_meta.status")
        results = self.claim_verification
        if results is not None and meta is None:
            raise ValueError("claim results need their verification_meta")
        if self.source_conflicts and (meta is None or meta.contradiction_scan == "not_run"):
            raise ValueError("source conflicts need a conflict scan that ran")
        if self.source_conflicts and results is None:
            raise ValueError("source conflicts belong to listed requirements")
        if results is None:
            if self.unverified_items or any(
                i.verification_status != "not_claim_verified" for i in items
            ):
                raise ValueError("verification results are missing")
            return self
        by_id = {r.requirement_id: r for r in results}
        if len(by_id) != len(results):
            raise ValueError("one verification result per requirement")
        listed = [i.requirement_id for i in items]
        listed += [u.requirement_id for u in self.unverified_items]
        if len(set(listed)) != len(listed):
            raise ValueError("a requirement is either in the checklist or unverified, not both")
        if set(listed) != set(by_id):
            raise ValueError("every checklist and unverified item has exactly one result")
        for item in items:
            r = by_id[item.requirement_id]
            if item.applicability != r.applicability_after:
                raise ValueError("a checklist item shows its verified applicability")
            if r.requirement_verdict == "unsupported":
                raise ValueError("unsupported requirements are listed as unverified items")
            if item.verification_status != r.requirement_verdict:
                raise ValueError("a checklist item shows its requirement verdict")
            if not never_upgraded(item.proposed_applicability, r.applicability_before):
                raise ValueError("verification starts from at most the proposed applicability")
        for u in self.unverified_items:
            if by_id[u.requirement_id].requirement_verdict != "unsupported":
                raise ValueError("only unsupported requirements are listed as unverified items")
        for c in self.source_conflicts:
            flagged = by_id.get(c.requirement_id)
            if flagged is None or "conflicting_sources" not in flagged.review_flags:
                raise ValueError("a source conflict flags its requirement conflicting_sources")
        return self


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
    # M3 S5: the validated checklist (None until validation ran) and the report.
    checklist: Annotated[list[JSON] | None, Validates(ValidatedRequirement, each=True)]
    report: Annotated[JSON | None, Validates(ComplianceReport)]
    # M4 claim verification (M4_PLAN §C). None until the claim_verification node ran.
    claim_results: Annotated[list[JSON] | None, Validates(RequirementVerification, each=True)]
    verification_meta: Annotated[JSON | None, Validates(VerificationMeta)]
    unverified_items: Annotated[list[JSON], Validates(UnverifiedItem, each=True)]
    source_conflicts: Annotated[list[JSON], Validates(SourceConflict, each=True)]  # M4 S6


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
                    if value is None:
                        continue
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
