"""Pure routing functions for the workflow graph (M3 S2; M3_PLAN §3, §5).

Every function reads only the state it is given and returns a node name. They fail
closed: a missing or unexpected value routes to `finalize`, whose outcome then follows
the precedence in `decide_outcome`. No routing function can produce an approval.

Edges (the builder registers exactly these):
- START -> input_security
- input_security -> entity_intake | finalize            (`route_after_security`)
- entity_intake -> policy_retrieval | clarify | finalize (`route_after_intake`)
- clarify -> entity_intake
- policy_retrieval -> form_analysis | evidence_association | finalize
                                                          (`route_after_retrieval`)
- form_analysis -> evidence_association -> compliance_synthesis -> output_validation
  -> finalize -> END
"""

from collections.abc import Mapping
from typing import Any, Final, Literal

from app.graph.state import Outcome, SecurityStatus
from app.schemas.taxonomy import EvidenceEligibility

INPUT_SECURITY: Final = "input_security"
ENTITY_INTAKE: Final = "entity_intake"
CLARIFY: Final = "clarify"
POLICY_RETRIEVAL: Final = "policy_retrieval"
FORM_ANALYSIS: Final = "form_analysis"
EVIDENCE_ASSOCIATION: Final = "evidence_association"
COMPLIANCE_SYNTHESIS: Final = "compliance_synthesis"
OUTPUT_VALIDATION: Final = "output_validation"
FINALIZE: Final = "finalize"

NODE_ORDER: tuple[str, ...] = (
    INPUT_SECURITY,
    ENTITY_INTAKE,
    CLARIFY,
    POLICY_RETRIEVAL,
    FORM_ANALYSIS,
    EVIDENCE_ASSOCIATION,
    COMPLIANCE_SYNTHESIS,
    OUTPUT_VALIDATION,
    FINALIZE,
)

# Evidence that may enter compliance reasoning; `context_only` never does by default.
ELIGIBLE = frozenset({EvidenceEligibility.PRIMARY.value, EvidenceEligibility.SUPPORTING.value})

AfterSecurity = Literal["entity_intake", "finalize"]
AfterIntake = Literal["policy_retrieval", "clarify", "finalize"]
AfterRetrieval = Literal["form_analysis", "evidence_association", "finalize"]


def route_after_security(state: Mapping[str, Any]) -> AfterSecurity:
    """Only an explicit `safe` continues; blocked, review and missing all finalize."""
    return ENTITY_INTAKE if state.get("security_status") == SecurityStatus.SAFE else FINALIZE


def route_after_intake(state: Mapping[str, Any], *, max_rounds: int) -> AfterIntake:
    """Complete -> retrieval. Incomplete -> another clarification round while fewer than
    `max_rounds` were asked, else finalize (INSUFFICIENT_INFORMATION)."""
    if state.get("intake_complete") is True:
        return POLICY_RETRIEVAL
    rounds = state.get("clarification_rounds") or 0
    return CLARIFY if rounds < max_rounds else FINALIZE


def has_eligible_evidence(state: Mapping[str, Any]) -> bool:
    return any(
        isinstance(item, Mapping) and item.get("evidence_eligibility") in ELIGIBLE
        for item in state.get("evidence") or []
    )


def retrieval_failed(state: Mapping[str, Any]) -> bool:
    meta = state.get("retrieval_meta")
    return not isinstance(meta, Mapping) or meta.get("failed") is not False


def route_after_retrieval(state: Mapping[str, Any]) -> AfterRetrieval:
    """No eligible evidence, a failed retrieval or missing retrieval metadata ->
    finalize (INSUFFICIENT_EVIDENCE); otherwise form analysis only when required."""
    if retrieval_failed(state) or not has_eligible_evidence(state):
        return FINALIZE
    return FORM_ANALYSIS if state.get("form_analysis_required") is True else EVIDENCE_ASSOCIATION


def decide_outcome(state: Mapping[str, Any]) -> Outcome:
    """The report outcome, by precedence (M3_PLAN §5):
    BLOCKED > INSUFFICIENT_INFORMATION > INSUFFICIENT_EVIDENCE > DEGRADED >
    CHECKLIST_REVIEW_REQUIRED. Every outcome still requires human review."""
    if state.get("security_status") != SecurityStatus.SAFE:
        return Outcome.BLOCKED
    if state.get("intake_complete") is not True:
        return Outcome.INSUFFICIENT_INFORMATION
    if retrieval_failed(state) or not has_eligible_evidence(state):
        return Outcome.INSUFFICIENT_EVIDENCE
    meta = state.get("retrieval_meta")
    partial_retrieval = isinstance(meta, Mapping) and bool(meta.get("degraded"))
    if state.get("degradations") or partial_retrieval or state.get("draft") is None:
        return Outcome.DEGRADED
    return Outcome.CHECKLIST_REVIEW_REQUIRED
