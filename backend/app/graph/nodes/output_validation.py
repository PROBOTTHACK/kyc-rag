"""`output_validation` node (M3 S5; M3_PLAN §7): the authoritative deterministic gate.

Re-resolves every cited id through the corpus reader (Postgres), drops uncited
requirements, enforces the MANDATORY gate and never upgrades an applicability
(`app.graph.validation.validate_draft`). No LLM call.

- No draft (synthesis failed): nothing to validate; `checklist` stays None (DEGRADED).
- No corpus reader: citations cannot be re-resolved, so nothing can be shown as cited;
  every requirement is dropped and the reason is recorded. Never trusted blindly.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import (
    Degradation,
    EvidenceItem,
    FieldEvidenceLink,
    KYCState,
    ValidationFinding,
    dump,
    dump_list,
    read_list,
)
from app.graph.synthesis import ChecklistDraft
from app.graph.validation import validate_draft

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def output_validation(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    raw = state.get("draft")
    if raw is None:
        return {"checklist": None}
    draft = ChecklistDraft.model_validate(raw)
    evidence = read_list(state, "evidence", EvidenceItem)
    links = read_list(state, "associations", FieldEvidenceLink)
    update: dict[str, Any] = {}
    if deps.corpus is None:
        resolved = {}
        update["degradations"] = [
            *(state.get("degradations") or []),
            dump(Degradation(component="retrieval", reason="citations_unresolvable")),
        ]
    else:
        cited = {eid for r in draft.requirements for eid in r.evidence_ids}
        resolved = deps.corpus.evidence(sorted(cited & {e.evidence_id for e in evidence}))
    result = validate_draft(draft, evidence, resolved, links)
    findings = [
        *read_list(state, "validation_findings", ValidationFinding),
        *result.findings,
    ]
    return update | {
        "checklist": dump_list(result.checklist),
        "validation_findings": dump_list(findings),
    }
