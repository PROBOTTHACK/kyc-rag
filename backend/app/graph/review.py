"""Open human-review cases derived from a report (M4 S7; M4_PLAN §J, decision D5).

A review case records WHAT verification left open for a human, never a decision:
- one case per (report, requirement, reason), from the requirement's `review_flags` in the
  report's `claim_verification` (a requirement with several distinct flags gets several
  cases; a repeated flag never makes a second one);
- `claim_id` is always None in M4 (the flags are requirement-level);
- `status` is always `open`: there is no decision, approval, rejection, resolution or
  dismissal in M4 (they need authentication, a later milestone);
- the id is a UUIDv5 of report id, requirement id and reason, so deriving twice from the
  same report gives the same cases, and storing them twice stores nothing new.

`review_cases` is pure: it reads a validated report and never changes it. Reports without
verification results (v1, blocked, insufficient, failed synthesis, a hidden checklist)
yield no cases.
"""

import uuid
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.graph.state import ComplianceReport, ReviewReason

REVIEW_CASE_NAMESPACE: Final = uuid.UUID("3d1f6c2a-8b4e-5f7a-9c1d-2e6b7a4f0c95")
REVIEW_REASONS: Final[tuple[str, ...]] = get_args(ReviewReason)


class ReviewCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    report_id: str
    session_id: str | None
    run_id: str | None
    requirement_id: str = Field(min_length=1, max_length=64)
    claim_id: None = None  # requirement-level in M4
    reason: ReviewReason
    status: Literal["open"] = "open"


def review_case_id(report_id: str, requirement_id: str, reason: str) -> str:
    return str(uuid.uuid5(REVIEW_CASE_NAMESPACE, f"{report_id}/{requirement_id}/{reason}"))


def review_cases(report: ComplianceReport) -> list[ReviewCase]:
    """The open cases a report implies, ordered by requirement id, then reason."""
    if report.report_version != 2 or not report.claim_verification:
        return []
    keys = sorted(
        {(r.requirement_id, flag) for r in report.claim_verification for flag in r.review_flags}
    )
    return [
        ReviewCase(
            id=review_case_id(report.report_id, requirement_id, reason),
            report_id=report.report_id,
            session_id=report.session_id,
            run_id=report.run_id,
            requirement_id=requirement_id,
            reason=reason,
        )
        for requirement_id, reason in keys
    ]
