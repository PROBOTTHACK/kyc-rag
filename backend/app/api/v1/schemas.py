"""The documented API contract (M5 S1, decision M5-D2).

These models describe the JSON the existing workflow routes already return, so OpenAPI
can publish it and the frontend can generate types from it. They are **documentation
models only**:
- The routes never validate or re-serialize through them. Responses are exactly what
  `WorkflowService` returns, unchanged.
- Request bodies are still read raw and screened before anything is parsed (M3 S6).

Contract tests check that real responses validate against these models (`extra="forbid"`
everywhere, so an undocumented key fails) and that every response is byte-identical to the
service output.

Optional keys: a field with a default (e.g. `WorkflowView.clarification`) may be **absent**
from a response. It is present only in the state named in its description.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.events import ProgressEvent
from app.graph.intake import Question
from app.graph.state import DataClass, Outcome, ReviewReason, RunStatus


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- errors ------------------------------------------------------------------------------


class ErrorDetail(_Doc):
    code: str = Field(description="A stable error code, e.g. SESSION_NOT_FOUND.")
    message: str = Field(description="A fixed, safe message. Never contains request input.")
    categories: list[str] | None = Field(
        default=None,
        description="Screening category codes; only on SENSITIVE_DATA_DETECTED and "
        "INPUT_REJECTED.",
    )
    retry_after: int | None = Field(
        default=None,
        description="Seconds until a retry may succeed; only on some RUN_IN_PROGRESS errors "
        "(also sent as the Retry-After header).",
    )


class ErrorResponse(_Doc):
    """Every error: `{"error": {"code", "message", ...}}`."""

    error: ErrorDetail


# --- requests (documentation of the raw bodies; see the module docstring) --------------------


class CreateSessionRequest(_Doc):
    data_class: Literal["synthetic_demo", "unspecified"] = Field(
        default=DataClass.UNSPECIFIED.value,
        description="Use synthetic_demo for demo data. The body may be omitted.",
    )


class MessageRequest(_Doc):
    message: str = Field(min_length=1, description="The question. Synthetic data only.")


class ResumeRequest(_Doc):
    checkpoint_id: str = Field(
        description="The current `workflow.clarification.checkpoint_id` (a lowercase UUID)."
    )
    answers: dict[str, str] = Field(
        description="One entry per asked question: `{field: one of its options}`."
    )


class RetryRequest(_Doc):
    """An empty object. The body may be omitted."""


# --- sessions ----------------------------------------------------------------------------


class SessionCreated(_Doc):
    session_id: str
    status: Literal["active", "expired"]
    data_class: Literal["synthetic_demo", "unspecified"]
    expires_at: str = Field(description="ISO 8601 timestamp.")


class NodeEvent(_Doc):
    seq: int = Field(ge=1, description="Gap-free per run, starting at 1.")
    node: str = Field(description="A graph node name, or `error:<ExceptionClass>`.")
    status: Literal["completed", "failed", "recovered"]
    at: str = Field(description="ISO 8601 timestamp.")


class PendingClarification(_Doc):
    checkpoint_id: str = Field(description="Send back unchanged with POST /resume.")
    round: int | None
    max_rounds: int | None
    questions: list[Question]


class WorkflowView(_Doc):
    run_id: str
    thread_id: str
    status: RunStatus
    started_at: str = Field(description="ISO 8601 timestamp.")
    finished_at: str | None = Field(description="ISO 8601 timestamp, or null while open.")
    node_events: list[NodeEvent]
    clarification: PendingClarification | None = Field(
        default=None,
        description="Present only while `status` is awaiting_clarification (null if the "
        "pending question cannot be read).",
    )
    report_id: str | None = Field(
        default=None,
        description="Present only when `status` is completed (null if no report is stored).",
    )
    outcome: Outcome | None = Field(
        default=None,
        description="Present only when `status` is completed (null if no report is stored). "
        "No outcome approves or rejects anything.",
    )


class SessionView(SessionCreated):
    workflow: WorkflowView | None = Field(
        description="The latest run of the session, or null before the first message."
    )


class RunAccepted(_Doc):
    session_id: str
    run_id: str
    status: Literal["running"]


# --- reports -----------------------------------------------------------------------------


class ReviewCase(_Doc):
    id: str
    requirement_id: str
    claim_id: str | None = Field(description="Always null in M4.")
    reason: ReviewReason
    status: Literal["open"] = Field(description="Review cases are open-only (M4 D5).")
    created_at: str = Field(description="ISO 8601 timestamp.")


class ReviewCasesResponse(_Doc):
    """Read-only. No decision, approval, rejection or resolution route exists."""

    report_id: str
    review_cases: list[ReviewCase]


# --- OpenAPI helpers ---------------------------------------------------------------------


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries for the error envelope."""
    return {s: {"model": ErrorResponse, "description": "Error (codes only)."} for s in statuses}


def json_body(model: type[BaseModel], *, required: bool) -> dict[str, Any]:
    """`openapi_extra=` documenting a raw JSON request body. The schema is inlined: the
    request models are kept flat (no nested definitions), which a test checks."""
    return {
        "requestBody": {
            "required": required,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }


# Models referenced only by `$ref` (the SSE event payload), not by a route's `responses`.
EXTRA_SCHEMAS: tuple[type[BaseModel], ...] = (ProgressEvent,)


def add_extra_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Register EXTRA_SCHEMAS under `components/schemas` (flat models only)."""
    components = spec.setdefault("components", {}).setdefault("schemas", {})
    for model in EXTRA_SCHEMAS:
        components[model.__name__] = model.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
    return spec
