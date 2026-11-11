"""The report and the user-facing message (M3 S5; M3_PLAN §5, "no evidence, no answer").

Everything here is deterministic: no model writes the message. Rules:
- No evidence, or no requirement surviving validation -> the owner's fallback text,
  verbatim, with the HDFC Bank referral.
- A technical retrieval failure is reported as a failure, never as "no information".
- A missing form schema is reported on its own line, never as a retrieval failure.
- Partial evidence is said to be partial; nothing is said to be "not required" or
  "not applicable".
- BLOCKED reports carry reason codes only: no question text, no quoted user words.
- Every message ends with the independence disclaimer; every report requires review.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from app.graph.state import (
    DISCLAIMER,
    ComplianceReport,
    Degradation,
    EntityProfile,
    FormRef,
    FormSummary,
    Outcome,
    ProfileSummary,
    RetrievalMeta,
    ValidatedRequirement,
    ValidationFinding,
    read,
    read_list,
)


class ReportStore(Protocol):
    """Where finalize stores the run's report (insert-only; idempotent per report id)."""

    def save(self, report: ComplianceReport) -> None: ...


REPORT_NAMESPACE = uuid.UUID("8f2b0f4e-3c1a-5d7e-9b6a-2e4c1d0a7f53")

FALLBACK = (
    "I don't have sufficient verified information in my available knowledge base to answer "
    "this query. Please visit https://www.hdfcbank.com/ or contact HDFC Bank customer service "
    "for accurate information."
)
REFERRAL = (
    "For accurate information, please visit https://www.hdfcbank.com/ or contact HDFC Bank "
    "customer service."
)
RETRIEVAL_FAILED = (
    "A technical problem prevented searching the knowledge base, so no conclusion was drawn "
    "about your question. Please try again later."
)
CHECKLIST_FOUND = (
    "Below is a draft checklist built only from the cited sources. Each item shows its "
    "source document and page. It is decision support for human review, not a KYC decision."
)
PARTIAL_NOTE = (
    "Some points could not be tied to primary sources specific to this customer type; they "
    "are shown as conditional or unclear. This does not mean they do not apply."
)
DEGRADED_NOTE = (
    "Part of the processing did not complete normally ({components}), so this result may be "
    "incomplete."
)
SCHEMA_NOTE = "The stored structure of {count} relevant form(s) was unavailable."
CAPPED_NOTE = "Only a subset of the fields of {forms} was analysed."
INSUFFICIENT_INFORMATION = (
    "The question could not be answered because required details were not provided: {fields}."
)
BLOCKED = "This request could not be processed."
NO_SUPPORTED_REQUIREMENTS = "no_supported_requirements"


def report_id(session_id: str | None, run_id: str | None) -> str:
    """Deterministic per run (S5-D3): a repeated finalize writes the same report."""
    return str(uuid.uuid5(REPORT_NAMESPACE, f"{session_id or '-'}/{run_id or '-'}"))


def _with_disclaimer(*parts: str) -> str:
    return " ".join(p for p in parts if p) + "\n\n" + DISCLAIMER


def _form_summaries(forms: list[FormRef]) -> list[FormSummary]:
    return [
        FormSummary(
            form_key=f.form_key,
            method=f.method,
            version=f.version,
            flags=f.flags,
            fields_analysed=len(f.fields),
            subset_only="fields_capped" in f.flags,
        )
        for f in forms
    ]


def user_message(
    outcome: Outcome,
    *,
    meta: RetrievalMeta | None,
    checklist: list[ValidatedRequirement] | None,
    degradations: list[Degradation],
    gaps: list[str],
    forms: list[FormSummary],
    missing_fields: list[str],
) -> str:
    schema_prefixes = ("no_stored_schema:", "schema_load_failed:")
    schema_gaps = sorted({g.split(":", 1)[1] for g in gaps if g.startswith(schema_prefixes)})
    schema_note = SCHEMA_NOTE.format(count=len(schema_gaps)) if schema_gaps else ""
    capped = [f.form_key for f in forms if f.subset_only]
    capped_note = CAPPED_NOTE.format(forms=", ".join(capped)) if capped else ""
    if outcome == Outcome.BLOCKED:
        return _with_disclaimer(BLOCKED)
    if outcome == Outcome.INSUFFICIENT_INFORMATION:
        fields = ", ".join(missing_fields) or "unknown"
        return _with_disclaimer(INSUFFICIENT_INFORMATION.format(fields=fields), REFERRAL)
    if outcome == Outcome.INSUFFICIENT_EVIDENCE:
        if meta is not None and meta.evidence_status == "retrieval_failed":
            return _with_disclaimer(RETRIEVAL_FAILED, REFERRAL)
        return _with_disclaimer(FALLBACK, schema_note)
    components = ", ".join(sorted({d.component for d in degradations})) or "unknown"
    if outcome == Outcome.DEGRADED and not checklist:
        return _with_disclaimer(DEGRADED_NOTE.format(components=components), FALLBACK,
                                schema_note)  # fmt: skip
    parts = [CHECKLIST_FOUND]
    if meta is not None and meta.evidence_status == "partial":
        parts.append(PARTIAL_NOTE)
    if outcome == Outcome.DEGRADED:
        parts.append(DEGRADED_NOTE.format(components=components))
    parts += [schema_note, capped_note]
    return _with_disclaimer(*parts)


def unresolved_items(gaps: list[str], findings: list[ValidationFinding], dropped: int) -> list[str]:
    items = list(gaps)
    items += sorted({f"{f.kind}:{f.detail}" for f in findings if f.kind == "mandatory_downgraded"})
    if dropped:
        items.append(f"dropped_uncited_requirements:{dropped}")
    return items


def build_report(state: Mapping[str, Any], outcome: Outcome) -> ComplianceReport:
    """The single report for this run, from state (codes and stored citations only)."""
    meta = read(state, "retrieval_meta", RetrievalMeta)
    raw_checklist = state.get("checklist")
    checklist = (
        [ValidatedRequirement.model_validate(c) for c in raw_checklist]
        if raw_checklist is not None
        else None
    )
    findings = read_list(state, "validation_findings", ValidationFinding)
    degradations = read_list(state, "degradations", Degradation)
    forms = _form_summaries(read_list(state, "forms", FormRef))
    gaps = list(meta.gaps) if meta else []
    if checklist is not None and not checklist and NO_SUPPORTED_REQUIREMENTS not in gaps:
        gaps.append(NO_SUPPORTED_REQUIREMENTS)
    dropped = sum(1 for f in findings if f.kind == "dropped_uncited")
    blocked = outcome == Outcome.BLOCKED
    profile = read(state, "entity_profile", EntityProfile)
    summary = (
        {
            name: ProfileSummary(value=getattr(profile, name).value,
                                 source=getattr(profile, name).source)
            for name in ("entity_type", "residency", "account_type")
        }
        if profile is not None and not blocked
        else {}
    )  # fmt: skip
    missing = list(state.get("missing_fields") or [])
    show_checklist = outcome in (Outcome.CHECKLIST_REVIEW_REQUIRED, Outcome.DEGRADED)
    return ComplianceReport(
        report_id=report_id(state.get("session_id"), state.get("run_id")),
        session_id=state.get("session_id"),
        run_id=state.get("run_id"),
        outcome=outcome,
        user_message=user_message(
            outcome,
            meta=meta,
            checklist=checklist if show_checklist else None,
            degradations=degradations,
            gaps=gaps,
            forms=forms,
            missing_fields=missing,
        ),
        profile=summary,
        missing_fields=missing if outcome == Outcome.INSUFFICIENT_INFORMATION else [],
        evidence_status=None if blocked or meta is None else meta.evidence_status,
        gaps=[] if blocked else gaps,
        checklist=checklist if show_checklist else None,
        dropped_requirements=dropped,
        unresolved_items=[] if blocked else unresolved_items(gaps, findings, dropped),
        findings=[] if blocked else findings,
        degradations=degradations,
        forms=[] if blocked else forms,
        security_reasons=list(state.get("security_reasons") or []) if blocked else [],
    )
