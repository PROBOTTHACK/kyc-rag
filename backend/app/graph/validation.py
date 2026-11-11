"""Output validation: the deterministic, authoritative gate on a drafted checklist (M3 S5;
M3_PLAN §7, "no evidence, no answer").

The model's draft is a proposal. For every drafted requirement:

1. Citations: an id survives only if it was SUPPLIED to the model (state `evidence`) AND
   re-resolves to a stored chunk through the corpus reader (Postgres). Everything shown
   for a citation is copied from the stored chunk, never from model output.
2. A requirement with no surviving citation is DROPPED (decision S5-D1): an uncited item
   is an invented answer. Only a count is kept, never its name.
3. MANDATORY survives only with a citation that is primary, able to support MANDATORY
   (not OCR-uncertain), from an active source, AND found for this entity type
   (`match_scope="entity"`). Otherwise it becomes CONDITIONAL, with a finding saying why.
   Entity-group, form, supporting, dated or OCR-uncertain evidence can never on its own
   establish MANDATORY. Association links are never citations.
4. Applicability is never upgraded: the result is the less strict of the proposal and
   what the evidence allows.

Explanations stay model prose, labelled as not verified (claim verification is M4).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.db.repository import EvidenceContext
from app.graph.state import (
    Citation,
    EvidenceItem,
    FieldEvidenceLink,
    RelatedField,
    ValidatedRequirement,
    ValidationFinding,
)
from app.graph.synthesis import ChecklistDraft
from app.schemas.taxonomy import Applicability

STRICTNESS = {
    Applicability.UNCLEAR: 0,
    Applicability.OPTIONAL: 1,
    Applicability.CONDITIONAL: 2,
    Applicability.MANDATORY: 3,
}
UNCERTAINTY = {
    Applicability.MANDATORY: (
        "Stated in a cited primary source for this customer type; proposed, not "
        "claim-verified (M4)."
    ),
    Applicability.CONDITIONAL: (
        "Supported by the cited sources but not established as mandatory; may depend on "
        "circumstances."
    ),
    Applicability.OPTIONAL: "The cited sources present this as optional.",
    Applicability.UNCLEAR: "Applicability could not be established from the cited sources.",
}
MAX_RELATED_FIELDS = 5


@dataclass(frozen=True)
class ValidationResult:
    checklist: list[ValidatedRequirement]
    findings: list[ValidationFinding]
    dropped: int


def _citation(item: EvidenceItem, ctx: EvidenceContext) -> Citation:
    stored = ctx.retrieved.metadata
    shown = ctx.context.metadata
    return Citation(
        evidence_id=stored.chunk_id,
        document_key=stored.document_key,
        page=shown.page_number,
        page_end=shown.page_end,
        paragraph=stored.paragraph,
        match_scope=item.match_scope,  # how it was found (S4); not model output
        evidence_eligibility=stored.evidence_eligibility,
        text_reliability=stored.text_reliability,
        source_status=stored.source_status,
        can_support_mandatory=stored.can_support_mandatory,
    )


def mandatory_capable(c: Citation) -> bool:
    return (
        c.evidence_eligibility == "primary"
        and c.can_support_mandatory
        and c.source_status == "active"
        and c.match_scope == "entity"
    )


def _why_not_mandatory(citations: Sequence[Citation]) -> str:
    if all(c.match_scope == "entity_group" for c in citations if c.can_support_mandatory) and any(
        c.can_support_mandatory for c in citations
    ):
        return "only_entity_group"
    if any(c.evidence_eligibility == "primary" and c.text_reliability == "ocr_uncertain"
           for c in citations):  # fmt: skip
        return "only_ocr_uncertain"
    if any(c.evidence_eligibility == "primary" and c.source_status != "active" for c in citations):
        return "not_active"
    return "only_form_or_supporting"


def validate_draft(
    draft: ChecklistDraft,
    supplied: Sequence[EvidenceItem],
    resolved: Mapping[str, EvidenceContext],
    links: Sequence[FieldEvidenceLink],
) -> ValidationResult:
    by_id = {e.evidence_id: e for e in supplied}
    checklist: list[ValidatedRequirement] = []
    findings: list[ValidationFinding] = []
    dropped = 0
    for draft_req in draft.requirements:
        req_id = f"REQ_{len(checklist) + 1:03d}"
        citations: list[Citation] = []
        for eid in dict.fromkeys(draft_req.evidence_ids):
            if eid in by_id and eid in resolved:
                citations.append(_citation(by_id[eid], resolved[eid]))
            else:
                detail = "a cited id was not supplied or not stored"
                findings.append(ValidationFinding(kind="unknown_evidence_id", detail=detail))
        if not citations:
            dropped += 1
            detail = "a drafted requirement had no valid citation"
            findings.append(ValidationFinding(kind="dropped_uncited", detail=detail))
            continue
        proposed = draft_req.applicability
        allowed = (
            Applicability.MANDATORY
            if any(mandatory_capable(c) for c in citations)
            else Applicability.CONDITIONAL
        )
        final = min(proposed, allowed, key=lambda a: STRICTNESS[a])
        if proposed == Applicability.MANDATORY and final != Applicability.MANDATORY:
            findings.append(
                ValidationFinding(
                    kind="mandatory_downgraded",
                    requirement_id=req_id,
                    detail=_why_not_mandatory(citations),
                )
            )
        cited = {c.evidence_id for c in citations}
        related = [
            RelatedField(form_key=link.form_key, field_label=link.field_label,
                         link_kind=link.link_kind)
            for link in links
            if link.evidence_id in cited
        ][:MAX_RELATED_FIELDS]  # fmt: skip
        checklist.append(
            ValidatedRequirement(
                requirement_id=req_id,
                name=draft_req.name,
                explanation=draft_req.explanation,
                applicability=final,
                proposed_applicability=proposed,
                citations=citations,
                related_form_fields=related,
                uncertainty_label=UNCERTAINTY[final],
            )
        )
    return ValidationResult(checklist=checklist, findings=findings, dropped=dropped)
