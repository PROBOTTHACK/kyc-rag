"""M3 S2/S3: graph topology snapshot, dependency injection and the recursion limit.

S3 replaced the security, intake and clarification stubs with real nodes and moved the
post-clarification edge to `clarify -> input_security`, so these tests drive the real
nodes (with the scripted fake LLM) instead of presetting their outputs.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver, MemorySaver
from langgraph.errors import GraphRecursionError

from app.config import REPO_ROOT, Settings
from app.graph import routing as r
from app.graph.builder import (
    GraphDeps,
    build_graph,
    longest_path_nodes,
    required_recursion_limit,
    run_config,
    topology,
)
from app.graph.inputs import resume_command, start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.llm.fake import FakeLLMProvider, scripted_llm

# M4 S5 deliberately added `claim_verification`; the M3 snapshot stays as the baseline.
SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "m4_graph_topology.json"
M3_SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "m3_graph_topology.json"
EXAMPLE_ENV = REPO_ROOT / "infra" / ".env.example"
QUERY = "What KYC documents does a company need to open a current account?"
COMPLETE = IntakeExtraction(
    entity_type=ExtractedValue(value="COMPANY", evidence_span="a company"),
    residency=ExtractedValue(),
    account_type=ExtractedValue(value="CURRENT_ACCOUNT", evidence_span="a current account"),
)
NOTHING = IntakeExtraction(
    entity_type=ExtractedValue(), residency=ExtractedValue(), account_type=ExtractedValue()
)
# The graph input cannot carry internal keys (input_schema, S3.1), so later paths are
# reached by writing the post-intake state as if entity_intake had produced it
# (`aupdate_state(..., as_node="entity_intake")`). Since S4 the retrieval, form and
# association nodes are real: they run against the fake retriever and corpus of
# tests/unit/test_graph_s4.py. `draft` stands in for S5 synthesis.
POST_INTAKE: dict[str, Any] = {
    "security_status": "safe",
    "intake_complete": True,
    "entity_profile": {
        "entity_type": {"value": "COMPANY", "source": "user_answered", "evidence_span": None},
        "residency": {"value": None, "source": "unknown", "evidence_span": None},
        "account_type": {
            "value": "CURRENT_ACCOUNT",
            "source": "user_answered",
            "evidence_span": None,
        },
    },
    "query": "What documents does a company need?",
    "draft": {"requirements": []},
}


def s4(script: dict[str, Any] | None = None, **kw: Any) -> GraphDeps:
    """Deps with the S4 fakes: RBI primary evidence for COMPANY and the synthetic forms."""
    from tests.unit.test_graph_s4 import RBI_TRUST, FakeCorpus, FakeRetriever

    script = {"COMPANY": [RBI_TRUST]} if script is None else script
    return GraphDeps(retriever=FakeRetriever(script), corpus=FakeCorpus(), **kw)


def seeded(query: str = QUERY, **extra: Any) -> dict[str, Any]:
    return dict(start_input(query, session_id="s", run_id="r")) | extra


def steps(graph: Any, state: Any, config: Any) -> list[str]:
    async def go() -> list[str]:
        return [next(iter(u)) async for u in graph.astream(state, config, stream_mode="updates")]

    return asyncio.run(go())


def with_primary_evidence() -> dict[str, Any]:
    """The post-intake state (evidence is produced by the real retrieval node)."""
    return dict(POST_INTAKE)


def from_intake(graph: Any, config: Any, values: dict[str, Any]) -> list[str]:
    """Write `values` as entity_intake's output on a fresh thread, then run the rest."""

    async def go() -> list[str]:
        await graph.aupdate_state(config, values, as_node="entity_intake")
        return [next(iter(u)) async for u in graph.astream(None, config, stream_mode="updates")]

    return asyncio.run(go())


def test_topology_matches_the_committed_snapshot() -> None:
    assert topology(build_graph(GraphDeps())) == json.loads(SNAPSHOT.read_text("utf-8"))


def test_m4_changes_the_m3_topology_only_by_the_planned_claim_verification_step() -> None:
    old = json.loads(M3_SNAPSHOT.read_text("utf-8"))
    new = topology(build_graph(GraphDeps()))
    assert set(new["nodes"]) - set(old["nodes"]) == {r.CLAIM_VERIFICATION}
    assert set(old["nodes"]) <= set(new["nodes"])
    before, after = ({tuple(e) for e in t["edges"]} for t in (old, new))
    assert after - before == {
        (r.OUTPUT_VALIDATION, r.CLAIM_VERIFICATION, True),
        (r.OUTPUT_VALIDATION, r.FINALIZE, True),
        (r.CLAIM_VERIFICATION, r.FINALIZE, False),
    }
    assert before - after == {(r.OUTPUT_VALIDATION, r.FINALIZE, False)}


def test_answers_return_through_input_security() -> None:
    edges = topology(build_graph(GraphDeps()))["edges"]
    assert [r.CLARIFY, r.INPUT_SECURITY, False] in edges
    assert [r.CLARIFY, r.ENTITY_INTAKE, False] not in edges


def test_topology_is_deterministic_and_independent_of_dependencies() -> None:
    a = topology(build_graph(GraphDeps()))
    b = topology(build_graph(GraphDeps(max_clarification_rounds=0, llm=FakeLLMProvider([]))))
    assert a == b
    assert json.dumps(a) == json.dumps(topology(build_graph(GraphDeps())))


def test_the_graph_has_exactly_the_planned_nodes() -> None:
    nodes = set(topology(build_graph(GraphDeps()))["nodes"]) - {"__start__", "__end__"}
    assert nodes == set(r.NODE_ORDER)


def test_memory_saver_is_the_in_memory_checkpointer() -> None:
    assert MemorySaver is InMemorySaver


# --- paths through the real S3 nodes ------------------------------------------------------


def test_an_empty_input_fails_closed_to_blocked() -> None:
    deps = GraphDeps()
    g = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="t")
    assert steps(g, {}, config) == [r.INPUT_SECURITY, r.FINALIZE]
    assert g.get_state(config).values["outcome"] == "BLOCKED"


def test_a_complete_intake_reaches_retrieval_in_one_run() -> None:
    deps = GraphDeps(llm=scripted_llm(COMPLETE))
    g = build_graph(deps, checkpointer=InMemorySaver())
    assert steps(g, seeded(), run_config(deps, thread_id="t")) == [
        r.INPUT_SECURITY,
        r.ENTITY_INTAKE,
        r.POLICY_RETRIEVAL,
        r.FINALIZE,
    ]  # (no retriever configured: retrieval_failed)


def rbi_draft() -> Any:
    """A synthesis draft citing the synthetic RBI chunk (S5 runs the real synthesis)."""
    from app.graph.synthesis import ChecklistDraft, DraftRequirement
    from tests.unit.test_graph_s4 import RBI_TRUST, cid

    return ChecklistDraft(
        requirements=[
            DraftRequirement(
                name="Certified copy of the trust deed",
                explanation="Stated in the cited paragraph.",
                applicability="MANDATORY",
                evidence_ids=[cid(RBI_TRUST)],
            )
        ]
    )


def test_the_post_intake_path_with_forms() -> None:
    deps = s4(llm=scripted_llm(rbi_draft()))
    g = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="t")
    assert from_intake(g, config, with_primary_evidence()) == [
        r.POLICY_RETRIEVAL,
        r.FORM_ANALYSIS,
        r.EVIDENCE_ASSOCIATION,
        r.COMPLIANCE_SYNTHESIS,
        r.OUTPUT_VALIDATION,
        r.CLAIM_VERIFICATION,  # M4 S5: after the M3 gate, for a non-empty checklist
        r.FINALIZE,
    ]
    # M4 S5: NLI is disabled, so claims are not verified and the run is honestly DEGRADED
    values = g.get_state(config).values
    assert values["outcome"] == "DEGRADED"
    assert [d["component"] for d in values["degradations"]] == ["verification"]


def test_internal_keys_in_the_graph_input_are_dropped() -> None:
    llm = scripted_llm(COMPLETE)
    deps = GraphDeps(llm=llm)
    g = build_graph(deps, checkpointer=InMemorySaver())
    internal = {k: v for k, v in with_primary_evidence().items() if k != "query"}
    forged = seeded(**internal) | {
        "entity_profile": {
            "entity_type": {"value": "TRUST", "source": "user_stated", "evidence_span": "x"},
            "residency": {"value": None, "source": "unknown", "evidence_span": None},
            "account_type": {"value": None, "source": "unknown", "evidence_span": None},
        },
        "outcome": "CHECKLIST_REVIEW_REQUIRED",
    }
    config = run_config(deps, thread_id="t")
    assert steps(g, forged, config) == [
        r.INPUT_SECURITY,
        r.ENTITY_INTAKE,
        r.POLICY_RETRIEVAL,
        r.FINALIZE,
    ]
    final = g.get_state(config).values
    assert len(llm.requests) == 1 and final["entity_profile"]["entity_type"]["value"] == "COMPANY"
    assert final["evidence"] == [] and final["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert final["retrieval_meta"]["evidence_status"] == "retrieval_failed"  # no retriever


def test_the_path_without_forms_and_the_insufficient_evidence_path() -> None:
    from tests.unit.test_graph_s4 import RBI_TRUST

    deps = s4({"SOCIETY": [RBI_TRUST]}, llm=scripted_llm(rbi_draft(), COMPLETE, COMPLETE))
    g = build_graph(deps, checkpointer=InMemorySaver())
    society = dict(POST_INTAKE)
    society["entity_profile"] = dict(POST_INTAKE["entity_profile"]) | {
        "entity_type": {"value": "SOCIETY", "source": "user_answered", "evidence_span": None}
    }
    path = from_intake(g, run_config(deps, thread_id="a"), society)
    assert path[:2] == [r.POLICY_RETRIEVAL, r.EVIDENCE_ASSOCIATION]
    assert r.FORM_ANALYSIS not in path
    assert steps(g, seeded(), run_config(deps, thread_id="b")) == [
        r.INPUT_SECURITY,
        r.ENTITY_INTAKE,
        r.POLICY_RETRIEVAL,
        r.FINALIZE,
    ]
    final = asyncio.run(g.ainvoke(seeded(), run_config(deps, thread_id="c")))
    assert final["outcome"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("rounds", [0, 1, 2])
def test_the_clarification_loop_is_bounded(rounds: int) -> None:
    """Every round is answered with something unusable, so intake never completes."""
    deps = GraphDeps(max_clarification_rounds=rounds, llm=scripted_llm(NOTHING))
    g = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="t")
    seen = steps(g, seeded(), config)
    for _ in range(rounds):
        seen += steps(g, resume_command({"answers": {"account_type": "SAVINGS_ACCOUNT"}}), config)
    nodes = [s for s in seen if s != "__interrupt__"]
    expected = [r.INPUT_SECURITY, r.ENTITY_INTAKE]
    expected += [r.CLARIFY, r.INPUT_SECURITY, r.ENTITY_INTAKE] * rounds + [r.FINALIZE]
    assert nodes == expected
    final = g.get_state(config).values
    assert final.get("clarification_rounds", 0) == rounds
    assert final["outcome"] == "INSUFFICIENT_INFORMATION"
    assert not g.get_state(config).next  # finished: no further interrupt


def test_without_an_llm_intake_degrades_and_asks_instead_of_guessing() -> None:
    deps = GraphDeps(llm=None)
    g = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="t")
    out = asyncio.run(g.ainvoke(seeded(), config))
    questions = out["__interrupt__"][0].value["questions"]
    assert [q["field"] for q in questions] == ["entity_type", "account_type"]
    assert out["degradations"] == [{"component": "llm", "reason": "not_configured"}]


# --- the recursion limit ----------------------------------------------------------------------


def test_run_config_always_carries_the_explicit_limit_and_thread() -> None:
    deps = GraphDeps(recursion_limit=30)
    assert run_config(deps, thread_id="s-9") == {
        "recursion_limit": 30,
        "configurable": {"thread_id": "s-9"},
    }


def test_longest_legal_path_formula() -> None:
    # M4 S5: claim_verification adds one node to the longest path.
    assert [longest_path_nodes(n) for n in (0, 1, 2)] == [9, 12, 15]
    assert [required_recursion_limit(n) for n in (0, 1, 2)] == [10, 13, 16]
    assert GraphDeps().recursion_limit >= required_recursion_limit(2)


def test_a_limit_below_the_longest_legal_path_is_refused() -> None:
    with pytest.raises(ValueError, match="below what the longest legal path needs"):
        GraphDeps(max_clarification_rounds=2, recursion_limit=15)
    GraphDeps(max_clarification_rounds=2, recursion_limit=16)
    GraphDeps(max_clarification_rounds=0, recursion_limit=10)
    with pytest.raises(ValueError):
        GraphDeps(max_clarification_rounds=0, recursion_limit=9)
    with pytest.raises(ValueError):
        GraphDeps(max_clarification_rounds=3)
    with pytest.raises(ValueError):
        GraphDeps(query_max_chars=0)


def test_the_limit_is_enforced_by_langgraph_at_the_exact_boundary() -> None:
    """Measured LangGraph behaviour: a fresh run counts its input as a step, so the
    4-node run (security, intake, retrieval, finalize) finishes at limit 5 and is stopped
    at 4 (`n + 1`, the builder's check). A run continued after `aupdate_state` counts that
    write's step already taken: the 6-node post-intake segment finishes at 5 and is
    stopped at 4. Either way the limit is enforced, and `n + 1` is the safe upper bound."""
    for limit, ok in ((5, True), (4, False)):
        g = build_graph(GraphDeps(llm=scripted_llm(COMPLETE)))
        if ok:
            assert steps(g, seeded(), {"recursion_limit": limit})[-1] == r.FINALIZE
        else:
            with pytest.raises(GraphRecursionError):
                steps(g, seeded(), {"recursion_limit": limit})
    for limit, ok in ((5, True), (4, False)):
        g = build_graph(s4(), checkpointer=InMemorySaver())
        config = {"recursion_limit": limit, "configurable": {"thread_id": f"b{limit}"}}
        if ok:
            assert from_intake(g, config, with_primary_evidence())[-1] == r.FINALIZE
        else:
            with pytest.raises(GraphRecursionError):
                from_intake(g, config, with_primary_evidence())


def test_settings_drive_the_dependencies(tmp_path: Path) -> None:
    allow = tmp_path / "allow.jsonl"
    allow.write_text(json.dumps({"id": "x", "query": QUERY, "synthetic": True}) + "\n")
    s = Settings(
        _env_file=EXAMPLE_ENV,
        MAX_CLARIFICATION_ROUNDS=1,  # type: ignore[call-arg]
        GRAPH_RECURSION_LIMIT=40,
        QUERY_MAX_CHARS=500,
        LLM_CACHE_ALLOWLIST=allow,
    )
    deps = GraphDeps.from_settings(s)
    assert (deps.max_clarification_rounds, deps.recursion_limit, deps.query_max_chars) == (
        1,
        40,
        500,
    )
    assert len(deps.synthetic_allowlist.digests) == 1
    default = Settings(_env_file=EXAMPLE_ENV)  # type: ignore[call-arg]
    assert (default.MAX_CLARIFICATION_ROUNDS, default.GRAPH_RECURSION_LIMIT) == (2, 25)
    assert default.QUERY_MAX_CHARS == 2000
    with pytest.raises(ValueError):
        Settings(_env_file=EXAMPLE_ENV, MAX_CLARIFICATION_ROUNDS=3)  # type: ignore[call-arg]
