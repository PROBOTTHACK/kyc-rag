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
    NOTICE_V2,
    ClaimType,
    ComplianceReport,
    Degradation,
    EntityProfile,
    ExplanationStatus,
    FormRef,
    FormSummary,
    Outcome,
    ProfileSummary,
    RequirementVerification,
    RetrievalMeta,
    UnverifiedItem,
    ValidatedRequirement,
    ValidationFinding,
    VerificationMeta,
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
# M4 (report v2). Worded so that a missing check never reads as a finding about the item.
VERIFICATION_OFF_NOTE = (
    "Automated checking of each claim against its cited text is not enabled, so every claim "
    "is marked not verified and no item is shown as more than conditional. This is not a "
    "finding that any item is wrong or unsupported; the cited sources and pages are unchanged."
)
VERIFICATION_INCOMPLETE_NOTE = (
    "Automated checking of claims against their cited text did not complete for every item; "
    "claims that were not checked are marked not verified."
)
UNVERIFIED_NOTE = (
    "{count} item(s) whose cited text did not support them are listed separately for review."
)
VERIFICATION_TEXT = {
    "not_configured": "not_claim_verified",
    "ran": "claim_check_ran",
    "partial": "claim_check_partial",
    "failed": "claim_check_failed",
}
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
    verification_notes: tuple[str, ...] = (),
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
    parts += [*verification_notes, schema_note, capped_note]
    return _with_disclaimer(*parts)


def unresolved_items(gaps: list[str], findings: list[ValidationFinding], dropped: int) -> list[str]:
    items = list(gaps)
    items += sorted({f"{f.kind}:{f.detail}" for f in findings if f.kind == "mandatory_downgraded"})
    if dropped:
        items.append(f"dropped_uncited_requirements:{dropped}")
    return items


def _explanation_status(r: RequirementVerification) -> ExplanationStatus:
    if r.requirement_verdict == "not_verified":
        return "model_drafted_not_verified"
    types = {c.claim_id: c.claim_type for c in r.claims}
    verdicts = [v.verdict for v in r.verdicts if types[v.claim_id] == ClaimType.EXPLANATORY_CLAIM]
    if not verdicts:
        return "model_drafted_not_verified"
    if all(v == "supported" for v in verdicts):
        return "verified_supported"
    return "partially_verified" if "supported" in verdicts else "unverified"


def verified_view(
    checklist: list[ValidatedRequirement], results: list[RequirementVerification]
) -> tuple[list[ValidatedRequirement], list[UnverifiedItem]]:
    """The checklist as verification left it (M4_PLAN §G): applicability lowered, verdict
    shown; `unsupported` items move to `unverified_items` (decision D2), never "not
    required". Citations, names, explanations and form links are kept as M3 produced them."""
    by_id = {r.requirement_id: r for r in results}
    items: list[ValidatedRequirement] = []
    unverified: list[UnverifiedItem] = []
    for item in checklist:
        r = by_id[item.requirement_id]
        if r.requirement_verdict == "unsupported":
            codes = sorted({code for v in r.verdicts for code in v.reason_codes})
            unverified.append(
                UnverifiedItem(
                    requirement_id=item.requirement_id,
                    name=item.name,
                    applicability_before=r.applicability_before,
                    citations=item.citations,
                    reason_codes=codes[:20],
                )
            )
            continue
        items.append(
            ValidatedRequirement.model_validate(
                item.model_dump()
                | {
                    "applicability": r.applicability_after,
                    "verification_status": r.requirement_verdict,
                    "explanation_status": _explanation_status(r),
                }
            )
        )
    return items, unverified


def verification_codes(results: list[RequirementVerification]) -> list[str]:
    """Review codes with ids only: what verification left open."""
    codes = []
    unchecked = sum(1 for r in results if r.requirement_verdict == "not_verified")
    if unchecked:
        codes.append(f"claims_not_verified:{unchecked}")
    codes += sorted(
        f"applicability_lowered_by_verification:{r.requirement_id}"
        for r in results
        if r.applicability_after != r.applicability_before
    )
    return codes


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
    shown = checklist if show_checklist else None
    unresolved = [] if blocked else unresolved_items(gaps, findings, dropped)
    # M4: report v2 whenever the claim_verification stage ran (verification_meta is set).
    vmeta = read(state, "verification_meta", VerificationMeta)
    v2: dict[str, Any] = {}
    notes: tuple[str, ...] = ()
    if vmeta is not None and not blocked:
        results = read_list(state, "claim_results", RequirementVerification)
        unverified: list[UnverifiedItem] = []
        shown_results: list[RequirementVerification] | None = None
        if shown is not None and results:
            shown, unverified = verified_view(shown, results)
            shown_results = results
            unresolved += verification_codes(results)
        if vmeta.status == "not_configured":
            notes = (VERIFICATION_OFF_NOTE,)
        elif vmeta.status in ("partial", "failed"):
            notes = (VERIFICATION_INCOMPLETE_NOTE,)
        if unverified:
            notes += (UNVERIFIED_NOTE.format(count=len(unverified)),)
        v2 = {
            "report_version": 2,
            "notice": NOTICE_V2,
            "verification": VERIFICATION_TEXT[vmeta.status],
            "verification_meta": vmeta,
            "claim_verification": shown_results,
            "unverified_items": unverified,
        }
    return ComplianceReport(
        report_id=report_id(state.get("session_id"), state.get("run_id")),
        session_id=state.get("session_id"),
        run_id=state.get("run_id"),
        outcome=outcome,
        user_message=user_message(
            outcome,
            meta=meta,
            checklist=shown,
            degradations=degradations,
            gaps=gaps,
            forms=forms,
            missing_fields=missing,
            verification_notes=notes,
        ),
        profile=summary,
        missing_fields=missing if outcome == Outcome.INSUFFICIENT_INFORMATION else [],
        evidence_status=None if blocked or meta is None else meta.evidence_status,
        gaps=[] if blocked else gaps,
        checklist=shown,
        dropped_requirements=dropped,
        unresolved_items=unresolved,
        findings=[] if blocked else findings,
        degradations=degradations,
        forms=[] if blocked else forms,
        security_reasons=list(state.get("security_reasons") or []) if blocked else [],
        **v2,
    )
