"""M3 S2: every routing edge, fail-closed defaults and the outcome precedence."""

from typing import Any

import pytest

from app.graph import routing as r
from app.graph.builder import GraphDeps, build_graph, topology
from app.graph.state import LinkKind, Outcome, RunStatus, SecurityStatus
from app.schemas.taxonomy import Applicability

OK_META = {"mode": "dense", "failed": False}
PRIMARY = [{"evidence_eligibility": "primary"}]

SECURITY_CASES: list[tuple[dict[str, Any], str]] = [
    ({"security_status": "safe"}, r.ENTITY_INTAKE),
    ({"security_status": "blocked"}, r.FINALIZE),
    ({"security_status": "review"}, r.FINALIZE),
    ({}, r.FINALIZE),  # missing: fail closed
    ({"security_status": "SAFE"}, r.FINALIZE),  # not the vocabulary value
    ({"security_status": None}, r.FINALIZE),
]

INTAKE_CASES: list[tuple[dict[str, Any], int, str]] = [
    ({"intake_complete": True}, 2, r.POLICY_RETRIEVAL),
    ({"intake_complete": True, "clarification_rounds": 2}, 2, r.POLICY_RETRIEVAL),
    ({"intake_complete": False, "clarification_rounds": 0}, 2, r.CLARIFY),
    ({"intake_complete": False, "clarification_rounds": 1}, 2, r.CLARIFY),
    ({"intake_complete": False, "clarification_rounds": 2}, 2, r.FINALIZE),
    ({"intake_complete": False, "clarification_rounds": 1}, 1, r.FINALIZE),
    ({"intake_complete": False}, 0, r.FINALIZE),  # no clarification allowed
    ({}, 2, r.CLARIFY),  # missing completeness is "not complete"
    ({"intake_complete": "true"}, 2, r.CLARIFY),  # only a real True counts
]

RETRIEVAL_CASES: list[tuple[dict[str, Any], str]] = [
    ({"retrieval_meta": OK_META, "evidence": PRIMARY, "form_analysis_required": True},
     r.FORM_ANALYSIS),  # fmt: skip
    ({"retrieval_meta": OK_META, "evidence": PRIMARY, "form_analysis_required": False},
     r.EVIDENCE_ASSOCIATION),  # fmt: skip
    ({"retrieval_meta": OK_META, "evidence": [{"evidence_eligibility": "supporting"}]},
     r.EVIDENCE_ASSOCIATION),  # fmt: skip
    ({"retrieval_meta": OK_META, "evidence": []}, r.FINALIZE),
    ({"retrieval_meta": OK_META}, r.FINALIZE),  # no evidence key
    ({"retrieval_meta": OK_META, "evidence": [{"evidence_eligibility": "context_only"}]},
     r.FINALIZE),  # fmt: skip
    ({"retrieval_meta": {"mode": "hybrid", "failed": True}, "evidence": PRIMARY}, r.FINALIZE),
    ({"evidence": PRIMARY}, r.FINALIZE),  # no retrieval metadata: fail closed
    ({"retrieval_meta": {"mode": "dense"}, "evidence": PRIMARY}, r.FINALIZE),  # failed unset
    # partial degradation still proceeds (the outcome becomes DEGRADED later)
    ({"retrieval_meta": {"mode": "hybrid", "failed": False, "degraded": ["bm25"]},
      "evidence": PRIMARY}, r.EVIDENCE_ASSOCIATION),  # fmt: skip
]


@pytest.mark.parametrize(("state", "expected"), SECURITY_CASES)
def test_route_after_security(state: dict[str, Any], expected: str) -> None:
    assert r.route_after_security(state) == expected


@pytest.mark.parametrize(("state", "max_rounds", "expected"), INTAKE_CASES)
def test_route_after_intake(state: dict[str, Any], max_rounds: int, expected: str) -> None:
    assert r.route_after_intake(state, max_rounds=max_rounds) == expected


@pytest.mark.parametrize(("state", "expected"), RETRIEVAL_CASES)
def test_route_after_retrieval(state: dict[str, Any], expected: str) -> None:
    assert r.route_after_retrieval(state) == expected


def test_the_tables_cover_every_conditional_edge_of_the_graph() -> None:
    covered = (
        {(r.INPUT_SECURITY, t) for _, t in SECURITY_CASES}
        | {(r.ENTITY_INTAKE, t) for _, _, t in INTAKE_CASES}
        | {(r.POLICY_RETRIEVAL, t) for _, t in RETRIEVAL_CASES}
    )
    graph_edges = {(s, t) for s, t, cond in topology(build_graph(GraphDeps()))["edges"] if cond}
    assert covered == graph_edges


# --- outcome precedence -------------------------------------------------------------------

SAFE: dict[str, Any] = {"security_status": "safe"}
COMPLETE: dict[str, Any] = SAFE | {"intake_complete": True}
EVIDENCED: dict[str, Any] = COMPLETE | {"retrieval_meta": OK_META, "evidence": PRIMARY}
DRAFTED: dict[str, Any] = EVIDENCED | {"draft": {"requirements": []}}

OUTCOME_CASES: list[tuple[dict[str, Any], Outcome]] = [
    ({}, Outcome.BLOCKED),
    ({"security_status": "blocked"} | DRAFTED | {"security_status": "blocked"}, Outcome.BLOCKED),
    (DRAFTED | {"security_status": "review"}, Outcome.BLOCKED),
    (SAFE, Outcome.INSUFFICIENT_INFORMATION),
    (DRAFTED | {"intake_complete": False}, Outcome.INSUFFICIENT_INFORMATION),
    (COMPLETE, Outcome.INSUFFICIENT_EVIDENCE),
    (DRAFTED | {"evidence": []}, Outcome.INSUFFICIENT_EVIDENCE),
    (DRAFTED | {"retrieval_meta": {"mode": "dense", "failed": True}},
     Outcome.INSUFFICIENT_EVIDENCE),  # fmt: skip
    (DRAFTED | {"evidence": [{"evidence_eligibility": "context_only"}]},
     Outcome.INSUFFICIENT_EVIDENCE),  # fmt: skip
    (EVIDENCED, Outcome.DEGRADED),  # no draft: synthesis failed
    (DRAFTED | {"degradations": [{"component": "llm", "reason": "ProviderTimeout"}]},
     Outcome.DEGRADED),  # fmt: skip
    (DRAFTED | {"retrieval_meta": {"mode": "hybrid", "failed": False, "degraded": ["bm25"]}},
     Outcome.DEGRADED),  # fmt: skip
    (DRAFTED, Outcome.CHECKLIST_REVIEW_REQUIRED),
    (DRAFTED | {"degradations": []}, Outcome.CHECKLIST_REVIEW_REQUIRED),
]


@pytest.mark.parametrize(("state", "expected"), OUTCOME_CASES)
def test_outcome_precedence(state: dict[str, Any], expected: Outcome) -> None:
    assert r.decide_outcome(state) is expected


def test_every_outcome_is_reachable_and_the_order_is_the_documented_precedence() -> None:
    assert {o for _, o in OUTCOME_CASES} == set(Outcome)
    assert [o.value for o in Outcome] == [
        "BLOCKED",
        "INSUFFICIENT_INFORMATION",
        "INSUFFICIENT_EVIDENCE",
        "DEGRADED",
        "CHECKLIST_REVIEW_REQUIRED",
    ]


def test_no_vocabulary_can_express_an_approval_or_rejection() -> None:
    forbidden = ("approv", "reject", "accept", "declin")
    for enum in (Outcome, RunStatus, SecurityStatus, LinkKind, Applicability):
        for member in enum:
            assert not any(f in member.value.lower() for f in forbidden), member
