"""M2 A2: RRF fusion, retrieval modes, degraded results, score semantics, provenance,
evaluation of modes, and the hybrid CLI. Services are faked; real-service checks are in
tests/integration/test_hybrid_live.py."""

from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.ingestion import cli
from app.retrieval import retrieve as retrieve_mod
from app.retrieval.evaluation import (
    DegradedRunError,
    RetrievalCase,
    evaluate_retrieval,
    render_comparison,
    score_case,
)
from app.retrieval.fusion import rrf_fuse
from app.retrieval.keyword import KeywordIndex, KeywordIndexUnavailable
from app.retrieval.retrieve import (
    RetrievalError,
    RetrievalMode,
    RetrievalResult,
    Retriever,
    ScoreKind,
)
from app.retrieval.search import SearchFilters, SearchHit
from tests.unit.test_keyword import corpus

# --- RRF ---------------------------------------------------------------------------------------


def test_rrf_scores_are_hand_computed() -> None:
    fused = rrf_fuse({"dense": ["a", "b", "c"], "bm25": ["c", "a"]}, k=60)
    assert [c.chunk_id for c in fused] == ["a", "c", "b"]
    by = {c.chunk_id: c for c in fused}
    assert by["a"].score == pytest.approx(1 / 61 + 1 / 62)
    assert by["c"].score == pytest.approx(1 / 63 + 1 / 61)
    assert by["b"].score == pytest.approx(1 / 62)  # one-sided
    assert by["a"].ranks == {"dense": 1, "bm25": 2} and by["b"].ranks == {"dense": 2}


def test_rrf_ties_break_by_best_rank_then_chunk_id() -> None:
    # x and y: dense #1 / bm25 #1 respectively, nowhere else -> equal fused scores and best
    # rank 1 -> chunk_id decides. p and q tie on score with different best ranks.
    fused = rrf_fuse({"dense": ["y", "p"], "bm25": ["x", "q"]}, k=60)
    assert [c.chunk_id for c in fused] == ["x", "y", "p", "q"]
    same = rrf_fuse({"d": ["m", "n"], "b": ["n", "m"]}, k=60)  # equal score, equal best rank
    assert [c.chunk_id for c in same] == ["m", "n"]
    assert rrf_fuse({"d": ["m", "n"], "b": ["n", "m"]}) == same  # deterministic


def test_rrf_edge_cases() -> None:
    assert rrf_fuse({"dense": [], "bm25": []}) == []
    assert len(rrf_fuse({"dense": ["a", "b", "c"]}, top_k=2)) == 2
    assert len(rrf_fuse({"dense": ["a"]}, top_k=10)) == 1
    with pytest.raises(ValueError, match=">= 1"):
        rrf_fuse({"dense": ["a"]}, k=0)
    with pytest.raises(ValueError, match="more than once"):
        rrf_fuse({"dense": ["a", "a"]})


# --- Retriever with fake services --------------------------------------------------------------


class FakeManager:
    def __init__(self, index: KeywordIndex, fail: bool = False) -> None:
        self.index, self.fail = index, fail
        self.last_check_ms, self.last_build_ms = 1.5, 0.0
        self.calls = 0

    def get(self) -> KeywordIndex:
        self.calls += 1
        if self.fail:
            raise KeywordIndexUnavailable("keyword index unavailable: postgres down")
        return self.index


class FakeEmbedder:
    model_name, revision = "fake/bge", "r1"
    dimension = 4

    def embed_query(self, text: str) -> list[float]:
        return [0.0] * 4

    def embed_passages(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


PAYLOADS = corpus()
INDEX = KeywordIndex.build(PAYLOADS)
# Dense ranking over the same chunks (Qdrant payloads carry `index` bookkeeping).
DENSE_ORDER = [4, 1, 0, 5]  # chunk positions in PAYLOADS, best first


def dense_hits(top_k: int) -> list[SearchHit]:
    return [
        SearchHit(r, 0.9 - r / 10, PAYLOADS[i]["chunk_id"], PAYLOADS[i] | {"index": {"x": 1}})
        for r, i in enumerate(DENSE_ORDER[:top_k], start=1)
    ]


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"dense_fail": False, "dense_calls": [], "factories": []}

    def fake_search(query: str, embedder: Any, store: Any, *, top_k: int, filters: Any) -> Any:
        state["dense_calls"].append(top_k)
        embedder.embed_query(query)
        if state["dense_fail"]:
            raise ConnectionError("qdrant unreachable")
        return dense_hits(top_k)

    monkeypatch.setattr(retrieve_mod, "search", fake_search)
    state["manager"] = FakeManager(INDEX)
    return state


def make(state: dict[str, Any], **kw: Any) -> Retriever:
    def factory(name: str, value: Any) -> Any:
        def build() -> Any:
            state["factories"].append(name)
            return value

        return build

    return Retriever(embedder=factory("embedder", FakeEmbedder()), store=factory("store", object()),
                     keyword=factory("keyword", state["manager"]), **kw)  # fmt: skip


def test_dense_mode_is_exactly_search(services: dict[str, Any]) -> None:
    res = make(services).retrieve("trust deed", mode="dense", top_k=3)
    assert res.hits == dense_hits(3)  # M1 hits, payloads with `index`, original scores
    assert res.score_kind is ScoreKind.DENSE and not res.is_degraded
    assert res.provenance[res.hits[0].chunk_id].dense_rank == 1
    assert services["dense_calls"] == [3] and "keyword" not in services["factories"]


def test_bm25_mode_is_exactly_the_keyword_index(services: dict[str, Any]) -> None:
    res = make(services).retrieve("trust deed", mode="bm25", top_k=3)
    assert res.hits == INDEX.search("trust deed", top_k=3)
    assert res.score_kind is ScoreKind.BM25
    assert services["factories"] == ["keyword"]  # no model, no Qdrant
    assert {"freshness_ms", "bm25_scoring_ms", "total_ms"} <= res.timings_ms.keys()


def test_hybrid_fuses_with_rrf_and_keeps_original_scores(services: dict[str, Any]) -> None:
    res = make(services, candidates=10).retrieve("trust deed", mode="hybrid", top_k=4)
    dense, bm25 = dense_hits(10), INDEX.search("trust deed", top_k=10)
    expected = rrf_fuse({"dense": [h.chunk_id for h in dense],
                         "bm25": [h.chunk_id for h in bm25]}, k=60, top_k=4)  # fmt: skip
    assert [h.chunk_id for h in res.hits] == [c.chunk_id for c in expected]
    assert res.score_kind is ScoreKind.RRF and res.degraded == () and res.failures == ()
    assert services["dense_calls"] == [10]  # candidates per retriever, not top_k
    d_by, b_by = {h.chunk_id: h for h in dense}, {h.chunk_id: h for h in bm25}
    for h, c in zip(res.hits, expected, strict=True):
        p = res.provenance[h.chunk_id]
        assert h.score == p.fused_score == pytest.approx(c.score)  # hybrid score = RRF
        assert (p.dense_rank, p.dense_score) == (
            (d_by[h.chunk_id].rank, d_by[h.chunk_id].score) if h.chunk_id in d_by else (None, None)
        )  # fmt: skip
        assert (p.bm25_rank, p.bm25_score) == (
            (b_by[h.chunk_id].rank, b_by[h.chunk_id].score) if h.chunk_id in b_by else (None, None)
        )  # fmt: skip
        # Postgres-built payload (no Qdrant `index`), whichever retriever found the chunk.
        assert h.payload == next(x for x in PAYLOADS if x["chunk_id"] == h.chunk_id)
        assert "index" not in h.payload
    assert any(p.retrievers == ("dense", "bm25") for p in res.provenance.values())
    assert {"dense_ms", "embed_ms", "qdrant_ms", "bm25_ms", "fusion_ms"} <= res.timings_ms.keys()


def test_hybrid_degrades_to_bm25_when_dense_fails(services: dict[str, Any]) -> None:
    services["dense_fail"] = True
    res = make(services).retrieve("trust deed", mode="hybrid", top_k=3)
    assert res.degraded == ("dense",) and res.is_degraded
    assert res.failures[0].retriever == "dense" and "qdrant unreachable" in res.failures[0].error
    assert res.hits == INDEX.search("trust deed", top_k=3)  # original BM25 scores
    assert res.score_kind is ScoreKind.BM25
    assert all(p.retrievers == ("bm25",) for p in res.provenance.values())


def test_hybrid_degrades_to_dense_when_bm25_fails(services: dict[str, Any]) -> None:
    services["manager"].fail = True
    res = make(services).retrieve("trust deed", mode="hybrid", top_k=3)
    assert res.degraded == ("bm25",) and "postgres down" in res.failures[0].error
    assert [h.chunk_id for h in res.hits] == [h.chunk_id for h in dense_hits(3)]
    assert [h.score for h in res.hits] == [h.score for h in dense_hits(3)]  # original dense
    assert all("index" not in h.payload for h in res.hits)
    assert res.score_kind is ScoreKind.DENSE
    assert all(p.retrievers == ("dense",) for p in res.provenance.values())


def test_hybrid_with_both_failing_is_an_error(services: dict[str, Any]) -> None:
    services["dense_fail"] = True
    services["manager"].fail = True
    with pytest.raises(RetrievalError) as err:
        make(services).retrieve("trust deed", mode="hybrid")
    assert [f.retriever for f in err.value.failures] == ["dense", "bm25"]
    assert "qdrant unreachable" in str(err.value) and "postgres down" in str(err.value)


@pytest.mark.parametrize("mode", ["dense", "bm25"])
def test_single_modes_never_fall_back(services: dict[str, Any], mode: str) -> None:
    services["dense_fail"] = True
    services["manager"].fail = True
    with pytest.raises(RetrievalError):
        make(services).retrieve("trust deed", mode=mode)


def test_empty_query_is_an_input_error_not_a_degradation(services: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="empty"):
        make(services).retrieve("   ", mode="hybrid")
    assert services["factories"] == [] and services["dense_calls"] == []


def test_dense_points_without_a_postgres_row_are_not_cited(
    services: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = SearchHit(1, 0.99, "00000000-0000-0000-0000-000000000000", PAYLOADS[0] | {"index": {}})
    monkeypatch.setattr(retrieve_mod, "search", lambda *a, **kw: [ghost, *dense_hits(3)])
    res = make(services).retrieve("trust deed", mode="hybrid", top_k=10)
    assert ghost.chunk_id not in res.provenance and res.dropped == (ghost.chunk_id,)


# --- evaluation of modes -----------------------------------------------------------------------


CASE = RetrievalCase.model_validate(
    {"id": "c1", "query": "trust deed", "targets": [{"document_key": "hdfc_form", "page": 5}]}
)


def test_evaluate_retrieval_scores_hits_and_reports_stages(services: dict[str, Any]) -> None:
    r = make(services)

    def run(q: str, f: SearchFilters, k: int) -> RetrievalResult:
        return r.retrieve(q, mode="hybrid", top_k=k, filters=f)

    report = evaluate_retrieval([CASE], run, mode="hybrid", description="d", timed_passes=2)
    assert report.results[0] == score_case(CASE, run("trust deed", SearchFilters(), 5).hits, 5)
    assert report.timing is not None and report.timing.passes == 2
    assert {"total", "fusion", "freshness", "embed"} <= report.timing.stages.keys()
    text = render_comparison([report, report.model_copy(update={"mode": "dense"})], note="n")
    assert "| hybrid |" in text and "Per-stage latency" in text and "| fusion |" in text


def test_evaluation_refuses_degraded_runs(services: dict[str, Any]) -> None:
    services["dense_fail"] = True
    r = make(services)

    def run(q: str, f: SearchFilters, k: int) -> RetrievalResult:
        return r.retrieve(q, mode="hybrid", top_k=k, filters=f)

    with pytest.raises(DegradedRunError, match="dense failed"):
        evaluate_retrieval([CASE], run, mode="hybrid", description="d")


# --- CLI ---------------------------------------------------------------------------------------


@pytest.fixture
def cli_services(services: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(cli, "_retriever", lambda *a, **kw: make(services))
    return services


def search_cli(**kw: Any) -> int:
    return cli._search(get_settings(), "trust deed", top_k=3, filters=SearchFilters(),
                       expand=False, mode="hybrid", sessions=object(), **kw)  # type: ignore[arg-type]  # fmt: skip


def test_cli_hybrid_shows_scores_and_provenance(
    cli_services: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert search_cli() == 0
    out = capsys.readouterr().out
    assert "; hybrid RRF k=60, 50 per retriever)" in out and "scores: rrf" in out
    assert "via: dense #" in out or "via: bm25 #" in out
    assert "DEGRADED" not in out


def test_cli_degraded_hybrid_is_exit_0_with_a_banner(
    cli_services: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    cli_services["dense_fail"] = True
    assert search_cli() == 0
    out = capsys.readouterr().out
    assert "DEGRADED: dense retrieval failed (ConnectionError: qdrant unreachable)" in out
    assert "showing bm25 results only" in out and "scores: bm25" in out


def test_cli_total_failure_is_exit_2(
    cli_services: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    cli_services["dense_fail"] = True
    cli_services["manager"].fail = True
    assert search_cli() == 2
    assert "search failed: every retriever failed" in capsys.readouterr().out


def test_cli_mode_defaults_to_retrieval_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_search", lambda *a, **kw: seen.update(kw) or 0)
    assert cli.main(["search", "q"]) == 0 and seen["mode"] == "dense"
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    get_settings.cache_clear()
    try:
        assert cli.main(["search", "q"]) == 0 and seen["mode"] == "hybrid"
        assert cli.main(["search", "q", "--mode", "bm25"]) == 0 and seen["mode"] == "bm25"
    finally:
        get_settings.cache_clear()


def test_invalid_retrieval_mode_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("RETRIEVAL_MODE", "sparse")
    with pytest.raises(ValidationError):
        Settings()


def test_compare_refuses_degraded_runs_and_the_heldout_set(
    cli_services: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    held = Path("data/eval/m2_heldout.jsonl")
    assert cli._evaluate(get_settings(), held, top_k=5, report_path=None, compare=True) == 2
    assert "frozen held-out set" in capsys.readouterr().out
    dev = tmp_path / "dev.jsonl"
    dev.write_text(CASE.model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_sessions", lambda s: object())
    cli_services["dense_fail"] = True  # dense-only fails outright; hybrid would degrade
    assert cli._evaluate(get_settings(), dev, top_k=5, report_path=None, compare=True) == 2
    assert "evaluation refused: RetrievalError: dense:" in capsys.readouterr().out
    assert cli._evaluate(get_settings(), dev, top_k=5, report_path=None, mode="hybrid") == 2
    assert "evaluation refused: DegradedRunError" in capsys.readouterr().out  # never scored


def test_compare_runs_three_modes_and_selects_nothing(
    cli_services: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dev = tmp_path / "dev.jsonl"
    dev.write_text(CASE.model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_sessions", lambda s: object())
    assert cli._evaluate(get_settings(), dev, top_k=5, report_path=None, compare=True) == 0
    out = capsys.readouterr().out
    for mode in ("| dense |", "| bm25 |", "| hybrid |"):
        assert mode in out
    assert "nothing is selected or tuned here" in out


def test_reconcile_keyword_line_never_raises(capsys: pytest.CaptureFixture[str]) -> None:
    def broken() -> Any:
        raise ConnectionError("postgres down")

    cli._print_keyword_info(broken, object())  # type: ignore[arg-type]
    assert "keyword index (informational): unavailable" in capsys.readouterr().out


def test_retrieval_modes_enum() -> None:
    assert [m.value for m in RetrievalMode] == ["dense", "bm25", "hybrid"]
    assert cli.MODES == ("dense", "bm25", "hybrid")
