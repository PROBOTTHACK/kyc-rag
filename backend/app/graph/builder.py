"""The workflow graph: topology, dependency injection and run limits (M3 S2; M3_PLAN §3).

- `GraphDeps` carries everything a node may use. Nodes are `(state, deps) -> partial`
  functions; the builder binds `deps` once, so tests inject fakes and nodes never read
  settings, environment variables or globals.
- `build_graph` registers exactly the edges documented in `app/graph/routing.py`.
  Conditional edges have explicit path maps, so the topology (`topology`) is complete
  and deterministic.
- The recursion limit is explicit: `run_config` always passes it, and `GraphDeps` refuses
  a limit below what the longest legal path needs, so a correct run can never hit it
  while a loop bug always does (LangGraph raises `GraphRecursionError`). LangGraph counts
  the input as a super-step too, so a path of n nodes needs a limit of n + 1.
- Checkpointing: callers pass a checkpointer. Tests use LangGraph's `InMemorySaver`
  (`MemorySaver`) and, in integration tests, `AsyncPostgresSaver`; the production
  Postgres checkpointer is wired in S6. A checkpointer is required for clarification
  (`interrupt()`).
- Execution is async (`ainvoke` / `astream`): `entity_intake` awaits the LLM layer;
  LangGraph runs the sync nodes in a worker thread.
- After a clarification answer the graph returns to `input_security` (M3 S3), so a
  resumed answer is always screened again before intake sees it.
"""

from dataclasses import dataclass
from functools import partial
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.config import Settings
from app.graph import routing as r
from app.graph.corpus import CorpusReader, SupportsRetrieve
from app.graph.nodes.claim_verification import claim_verification
from app.graph.nodes.clarify import clarify
from app.graph.nodes.compliance_synthesis import compliance_synthesis
from app.graph.nodes.entity_intake import entity_intake
from app.graph.nodes.evidence_association import evidence_association
from app.graph.nodes.finalize import finalize
from app.graph.nodes.form_analysis import form_analysis
from app.graph.nodes.input_security import input_security
from app.graph.nodes.output_validation import output_validation
from app.graph.nodes.policy_retrieval import policy_retrieval
from app.graph.report import ReportStore
from app.graph.state import KYCInput, KYCState
from app.llm.cache import SyntheticAllowlist
from app.llm.provider import LLMProvider
from app.verification.decide import Thresholds
from app.verification.nli import NLIScorer

# Nodes on the longest path apart from the clarification loop:
# input_security, entity_intake, policy_retrieval, form_analysis, evidence_association,
# compliance_synthesis, output_validation, claim_verification (M4), finalize.
_FIXED_PATH_NODES = 9
_INPUT_STEP = 1  # LangGraph counts the input as a super-step


def longest_path_nodes(max_clarification_rounds: int) -> int:
    """Nodes on the longest legal path: each clarification round adds clarify, another
    input_security and another entity_intake."""
    return _FIXED_PATH_NODES + 3 * max_clarification_rounds


def required_recursion_limit(max_clarification_rounds: int) -> int:
    """The smallest LangGraph `recursion_limit` that lets the longest legal path finish."""
    return longest_path_nodes(max_clarification_rounds) + _INPUT_STEP


@dataclass(frozen=True)
class GraphDeps:
    """Dependencies bound into the nodes. Optional services stay None until the stage
    that uses them. Without an `llm`, intake records a degradation and clarification
    asks for every field. Without a `retriever` or `corpus` (S4), retrieval reports
    `retrieval_failed` (never an empty "no evidence" result). The S6 API builds the real
    ones (`retriever_from_settings`, `PostgresCorpusReader`); tests inject fakes."""

    max_clarification_rounds: int = 2
    recursion_limit: int = 25
    llm: LLMProvider | None = None
    retriever: SupportsRetrieve | None = None
    corpus: CorpusReader | None = None
    reports: ReportStore | None = None  # S5 finalize; without one, the report stays in state
    retrieval_mode: str = "dense"
    rerank: bool = False
    query_max_chars: int = 2000
    node_timeout_s: float = 180.0
    synthetic_allowlist: SyntheticAllowlist = SyntheticAllowlist()
    # M4 claim verification. Both None = NLI disabled (the S4 outcome `m4-s4-v1`: no
    # configuration qualified): every claim is `not_verified` and the run is DEGRADED.
    # `from_settings` never sets them; only tests inject a scripted scorer.
    nli: NLIScorer | None = None
    nli_thresholds: Thresholds | None = None
    nli_max_pairs: int = 400

    def __post_init__(self) -> None:
        if not 0 <= self.max_clarification_rounds <= 2:
            raise ValueError("max_clarification_rounds must be 0..2")
        if not 0 < self.node_timeout_s <= 600:
            raise ValueError("node_timeout_s must be in (0, 600]")
        if self.query_max_chars <= 0:
            raise ValueError("query_max_chars must be positive")
        if (self.nli is None) != (self.nli_thresholds is None):
            raise ValueError("an NLI scorer and its thresholds are configured together")
        if self.nli_max_pairs < 0:
            raise ValueError("nli_max_pairs must be >= 0")
        needed = required_recursion_limit(self.max_clarification_rounds)
        if self.recursion_limit < needed:
            raise ValueError(
                f"recursion_limit {self.recursion_limit} is below what the longest legal "
                f"path needs ({needed})"
            )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        llm: LLMProvider | None = None,
        retriever: SupportsRetrieve | None = None,
        corpus: CorpusReader | None = None,
        reports: ReportStore | None = None,
    ) -> "GraphDeps":
        return cls(
            max_clarification_rounds=settings.MAX_CLARIFICATION_ROUNDS,
            recursion_limit=settings.GRAPH_RECURSION_LIMIT,
            llm=llm,
            retriever=retriever,
            corpus=corpus,
            reports=reports,
            retrieval_mode=settings.RETRIEVAL_MODE,
            rerank=settings.RERANK_ENABLED,
            query_max_chars=settings.QUERY_MAX_CHARS,
            node_timeout_s=settings.NODE_TIMEOUT_S,
            synthetic_allowlist=SyntheticAllowlist.load(settings.LLM_CACHE_ALLOWLIST),
            # NLI stays disabled (M4 S5): no model is built, loaded or called from
            # settings, whatever NLI_MODEL says, until a selection record selects one.
            nli=None,
            nli_thresholds=None,
            nli_max_pairs=settings.NLI_MAX_PAIRS,
        )


def build_graph(
    deps: GraphDeps, *, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[KYCState, None, KYCInput, KYCState]:
    # input_schema: a caller can set only what `start_input` produces (M3 S3.1).
    g: StateGraph[KYCState, None, KYCInput, KYCState] = StateGraph(KYCState, input_schema=KYCInput)
    g.add_node(r.INPUT_SECURITY, partial(input_security, deps=deps))
    g.add_node(r.ENTITY_INTAKE, partial(entity_intake, deps=deps))
    g.add_node(r.CLARIFY, partial(clarify, deps=deps))
    g.add_node(r.POLICY_RETRIEVAL, partial(policy_retrieval, deps=deps))
    g.add_node(r.FORM_ANALYSIS, partial(form_analysis, deps=deps))
    g.add_node(r.EVIDENCE_ASSOCIATION, partial(evidence_association, deps=deps))
    g.add_node(r.COMPLIANCE_SYNTHESIS, partial(compliance_synthesis, deps=deps))
    g.add_node(r.OUTPUT_VALIDATION, partial(output_validation, deps=deps))
    g.add_node(r.CLAIM_VERIFICATION, partial(claim_verification, deps=deps))
    g.add_node(r.FINALIZE, partial(finalize, deps=deps))

    g.add_edge(START, r.INPUT_SECURITY)
    g.add_conditional_edges(
        r.INPUT_SECURITY,
        r.route_after_security,
        {r.ENTITY_INTAKE: r.ENTITY_INTAKE, r.FINALIZE: r.FINALIZE},
    )
    g.add_conditional_edges(
        r.ENTITY_INTAKE,
        partial(r.route_after_intake, max_rounds=deps.max_clarification_rounds),
        {r.POLICY_RETRIEVAL: r.POLICY_RETRIEVAL, r.CLARIFY: r.CLARIFY, r.FINALIZE: r.FINALIZE},
    )
    g.add_edge(r.CLARIFY, r.INPUT_SECURITY)  # answers are always re-screened
    g.add_conditional_edges(
        r.POLICY_RETRIEVAL,
        r.route_after_retrieval,
        {
            r.FORM_ANALYSIS: r.FORM_ANALYSIS,
            r.EVIDENCE_ASSOCIATION: r.EVIDENCE_ASSOCIATION,
            r.FINALIZE: r.FINALIZE,
        },
    )
    g.add_edge(r.FORM_ANALYSIS, r.EVIDENCE_ASSOCIATION)
    g.add_edge(r.EVIDENCE_ASSOCIATION, r.COMPLIANCE_SYNTHESIS)
    g.add_edge(r.COMPLIANCE_SYNTHESIS, r.OUTPUT_VALIDATION)
    # M4 (D8): claim verification after the M3 gate, only for a non-empty checklist.
    g.add_conditional_edges(
        r.OUTPUT_VALIDATION,
        r.route_after_validation,
        {r.CLAIM_VERIFICATION: r.CLAIM_VERIFICATION, r.FINALIZE: r.FINALIZE},
    )
    g.add_edge(r.CLAIM_VERIFICATION, r.FINALIZE)
    g.add_edge(r.FINALIZE, END)
    return g.compile(checkpointer=checkpointer)


def run_config(deps: GraphDeps, *, thread_id: str) -> RunnableConfig:
    """The config for every invocation: the explicit recursion limit and the thread
    (= session id) the checkpointer keys on."""
    return {"recursion_limit": deps.recursion_limit, "configurable": {"thread_id": thread_id}}


def topology(graph: CompiledStateGraph[Any, Any, Any, Any]) -> dict[str, list[Any]]:
    """A deterministic description of the compiled graph: sorted node ids and sorted
    (source, target, conditional) edges."""
    drawable = graph.get_graph()
    nodes = sorted(drawable.nodes)
    edges = sorted({(e.source, e.target, bool(e.conditional)) for e in drawable.edges})
    return {"nodes": nodes, "edges": [list(e) for e in edges]}
