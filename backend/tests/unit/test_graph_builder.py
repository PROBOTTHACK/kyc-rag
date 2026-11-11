"""M3 S2: graph topology snapshot, dependency injection, stubs and the recursion limit."""

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
from app.llm.fake import FakeLLMProvider

SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "m3_graph_topology.json"
EXAMPLE_ENV = REPO_ROOT / "infra" / ".env.example"
COMPLETE: dict[str, Any] = {
    "security_status": "safe",
    "intake_complete": True,
    "retrieval_meta": {"mode": "dense", "failed": False},
    "evidence": [{"evidence_eligibility": "primary"}],
    "form_analysis_required": True,
    "draft": {"requirements": []},
}


def steps(graph: Any, state: dict[str, Any], config: Any) -> list[str]:
    return [next(iter(u)) for u in graph.stream(state, config, stream_mode="updates")]


def test_topology_matches_the_committed_snapshot() -> None:
    assert topology(build_graph(GraphDeps())) == json.loads(SNAPSHOT.read_text("utf-8"))


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


# --- stub behaviour along the paths ---------------------------------------------------------


def test_an_empty_state_fails_closed_to_blocked() -> None:
    deps = GraphDeps()
    g = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="t")
    assert steps(g, {}, config) == [r.INPUT_SECURITY, r.FINALIZE]
    assert g.get_state(config).values["outcome"] == "BLOCKED"


def test_the_full_path_with_forms() -> None:
    deps = GraphDeps()
    g = build_graph(deps)
    assert steps(g, COMPLETE, run_config(deps, thread_id="t")) == [
        r.INPUT_SECURITY,
        r.ENTITY_INTAKE,
        r.POLICY_RETRIEVAL,
        r.FORM_ANALYSIS,
        r.EVIDENCE_ASSOCIATION,
        r.COMPLIANCE_SYNTHESIS,
        r.OUTPUT_VALIDATION,
        r.FINALIZE,
    ]


def test_the_path_without_forms_and_the_insufficient_evidence_path() -> None:
    deps = GraphDeps()
    g = build_graph(deps)
    no_forms = COMPLETE | {"form_analysis_required": False}
    assert r.FORM_ANALYSIS not in steps(g, no_forms, run_config(deps, thread_id="a"))
    no_evidence = COMPLETE | {"evidence": []}
    assert steps(g, no_evidence, run_config(deps, thread_id="b")) == [
        r.INPUT_SECURITY, r.ENTITY_INTAKE, r.POLICY_RETRIEVAL, r.FINALIZE,
    ]  # fmt: skip
    assert g.invoke(no_evidence, run_config(deps, thread_id="c"))["outcome"] == (
        "INSUFFICIENT_EVIDENCE"
    )


@pytest.mark.parametrize("rounds", [0, 1, 2])
def test_the_clarification_loop_is_bounded(rounds: int) -> None:
    deps = GraphDeps(max_clarification_rounds=rounds)
    g = build_graph(deps)
    state = {"security_status": "safe", "intake_complete": False}
    expected = [r.INPUT_SECURITY, r.ENTITY_INTAKE]
    expected += [r.CLARIFY, r.ENTITY_INTAKE] * rounds + [r.FINALIZE]
    assert steps(g, state, run_config(deps, thread_id="t")) == expected
    final = g.invoke(state, run_config(deps, thread_id="u"))
    assert final.get("clarification_rounds", 0) == rounds
    assert final["outcome"] == "INSUFFICIENT_INFORMATION"


def test_stub_nodes_never_call_the_injected_llm() -> None:
    llm = FakeLLMProvider([])  # raises if called
    deps = GraphDeps(llm=llm)
    build_graph(deps).invoke(COMPLETE, run_config(deps, thread_id="t"))
    assert llm.requests == []


# --- the recursion limit ----------------------------------------------------------------------


def test_run_config_always_carries_the_explicit_limit_and_thread() -> None:
    deps = GraphDeps(recursion_limit=30)
    assert run_config(deps, thread_id="s-9") == {
        "recursion_limit": 30,
        "configurable": {"thread_id": "s-9"},
    }


def test_longest_legal_path_formula() -> None:
    assert [longest_path_nodes(n) for n in (0, 1, 2)] == [8, 10, 12]
    assert [required_recursion_limit(n) for n in (0, 1, 2)] == [9, 11, 13]
    assert GraphDeps().recursion_limit >= required_recursion_limit(2)


def test_a_limit_below_the_longest_legal_path_is_refused() -> None:
    with pytest.raises(ValueError, match="below what the longest legal path needs"):
        GraphDeps(max_clarification_rounds=2, recursion_limit=12)
    GraphDeps(max_clarification_rounds=2, recursion_limit=13)
    GraphDeps(max_clarification_rounds=0, recursion_limit=9)
    with pytest.raises(ValueError):
        GraphDeps(max_clarification_rounds=0, recursion_limit=8)
    with pytest.raises(ValueError):
        GraphDeps(max_clarification_rounds=3)


def test_the_limit_is_enforced_by_langgraph_at_the_exact_boundary() -> None:
    """The 8-node full path (the longest the stubs can reach) finishes at exactly
    `required_recursion_limit(0)` = 9 and is stopped at 8: the formula matches LangGraph
    and the limit is real, not advisory."""
    limit = required_recursion_limit(0)
    g = build_graph(GraphDeps(max_clarification_rounds=0, recursion_limit=limit))
    assert steps(g, COMPLETE, {"recursion_limit": limit})[-1] == r.FINALIZE
    with pytest.raises(GraphRecursionError):
        steps(g, COMPLETE, {"recursion_limit": limit - 1})


def test_the_clarification_path_also_matches_the_formula() -> None:
    """With 2 rounds and intake never completing, the stub path is security,
    (intake, clarify) x 2, intake, finalize = 7 nodes, so it needs a limit of 8."""
    g = build_graph(GraphDeps())
    state = {"security_status": "safe", "intake_complete": False}
    assert steps(g, state, {"recursion_limit": 8})[-1] == r.FINALIZE
    with pytest.raises(GraphRecursionError):
        steps(g, state, {"recursion_limit": 7})


def test_settings_drive_the_dependencies() -> None:
    s = Settings(_env_file=EXAMPLE_ENV, MAX_CLARIFICATION_ROUNDS=1,  # type: ignore[call-arg]
                 GRAPH_RECURSION_LIMIT=40)  # fmt: skip
    deps = GraphDeps.from_settings(s)
    assert (deps.max_clarification_rounds, deps.recursion_limit) == (1, 40)
    default = Settings(_env_file=EXAMPLE_ENV)  # type: ignore[call-arg]
    assert (default.MAX_CLARIFICATION_ROUNDS, default.GRAPH_RECURSION_LIMIT) == (2, 25)
    with pytest.raises(ValueError):
        Settings(_env_file=EXAMPLE_ENV, MAX_CLARIFICATION_ROUNDS=3)  # type: ignore[call-arg]
