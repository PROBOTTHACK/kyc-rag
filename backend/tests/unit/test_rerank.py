"""M2 B1: optional cross-encoder reranking (fake models; the real model is exercised in
tests/integration/test_rerank_live.py). Disabled by default; reorders only; payloads and
citations unchanged; original retrieval ranks/scores kept apart from raw cross-encoder
scores; failures labelled degraded."""

from collections.abc import Sequence
from typing import Any

import pytest

from app.config import get_settings
from app.ingestion import cli
from app.retrieval import rerank as rerank_mod
from app.retrieval.keyword import index_text
from app.retrieval.rerank import CrossEncoderReranker, RerankError, rerank
from app.retrieval.retrieve import Retriever, ScoreKind
from app.retrieval.search import SearchFilters, SearchHit
from tests.unit.test_hybrid import INDEX, FakeEmbedder, FakeManager, dense_hits

QUERY = "trust deed documents"


class FakeReranker:
    """Scores by a fixed per-text table; counts calls; can fail on demand."""

    model_name: str = "fake/ce"
    revision: str | None = "r0"

    def __init__(self, scores: dict[str, float] | None = None, fail: bool = False) -> None:
        self.scores = scores or {}
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []
        self.loaded = True
        self.load_ms = 0.0

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        if self.fail:
            raise RerankError("reranker inference failed: boom")
        return [self.scores.get(t, 0.0) for t in texts]


# --- rerank() ----------------------------------------------------------------------------------


def test_rerank_reorders_and_keeps_payloads_and_citations() -> None:
    hits = dense_hits(4)
    texts = [index_text(h.payload) for h in hits]

    class Positional(FakeReranker):  # two fixture chunks share a text, so score by position
        def score(self, query: str, texts: Sequence[str]) -> list[float]:
            self.calls.append((query, list(texts)))
            return [1.0, -2.0, 7.5, 3.0]

    fake = Positional()
    pairs = rerank(QUERY, hits, fake, top_k=3)
    assert [old.rank for _, old in pairs] == [3, 4, 1]
    assert [new.rank for new, _ in pairs] == [1, 2, 3]
    for new, old in pairs:
        assert new.payload is old.payload  # same object: nothing copied or edited
        assert new.citation == old.citation and new.chunk_id == old.chunk_id
    assert [new.score for new, _ in pairs] == [7.5, 3.0, 1.0]  # raw, unchanged
    assert fake.calls == [(QUERY, texts)]  # the child's context + content, as embedded


def test_rerank_ties_keep_the_original_retrieval_order() -> None:
    hits = dense_hits(4)
    pairs = rerank(QUERY, hits, FakeReranker(), top_k=4)  # every score 0.0
    assert [old.rank for _, old in pairs] == [1, 2, 3, 4]


def test_rerank_rejects_a_score_count_mismatch() -> None:
    class Short(FakeReranker):
        def score(self, query: str, texts: Sequence[str]) -> list[float]:
            return [0.0]

    with pytest.raises(RerankError, match="scores for"):
        rerank(QUERY, dense_hits(3), Short(), top_k=3)


# --- Retriever ---------------------------------------------------------------------------------


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"dense_calls": [], "factory_calls": 0}

    def fake_search(query: str, embedder: Any, store: Any, *, top_k: int, filters: Any) -> Any:
        state["dense_calls"].append(top_k)
        return dense_hits(top_k)

    monkeypatch.setattr("app.retrieval.retrieve.search", fake_search)
    return state


def retriever(state: dict[str, Any], reranker: Any, *, candidates: int = 20) -> Retriever:
    def factory() -> Any:
        state["factory_calls"] += 1
        if isinstance(reranker, Exception):
            raise reranker
        return reranker

    embedder: Any = FakeEmbedder
    store: Any = object
    keyword: Any = lambda: FakeManager(INDEX)  # noqa: E731
    return Retriever(embedder=embedder, store=store, keyword=keyword, reranker=factory,
                     rerank_candidates=candidates)  # fmt: skip


def test_disabled_path_never_builds_the_reranker(services: dict[str, Any]) -> None:
    res = retriever(services, FakeReranker()).retrieve(QUERY, mode="dense", top_k=3)
    assert res.hits == dense_hits(3) and not res.reranked
    assert res.score_kind is ScoreKind.DENSE and services["factory_calls"] == 0
    assert services["dense_calls"] == [3]  # no extra candidates fetched


def test_dense_rerank_fetches_candidates_and_keeps_scores_apart(services: dict[str, Any]) -> None:
    base = dense_hits(4)
    texts = {h.chunk_id: index_text(h.payload) for h in base}
    order = [base[2], base[0], base[3], base[1]]
    fake = FakeReranker({texts[h.chunk_id]: 10.0 - i for i, h in enumerate(order)})
    res = retriever(services, fake, candidates=20).retrieve(QUERY, mode="dense", top_k=3,
                                                             rerank=True)  # fmt: skip
    assert services["dense_calls"] == [20]  # RERANK_CANDIDATES retrieved first
    assert [h.chunk_id for h in res.hits] == [h.chunk_id for h in order[:3]]
    assert res.reranked and res.score_kind is ScoreKind.CROSS_ENCODER and not res.is_degraded
    by_id = {h.chunk_id: h for h in base}
    for h in res.hits:
        p = res.provenance[h.chunk_id]
        orig = by_id[h.chunk_id]
        assert h.score == p.rerank_score  # the hit carries the raw cross-encoder score
        assert (p.pre_rerank_rank, p.pre_rerank_score) == (orig.rank, orig.score)
        assert (p.dense_rank, p.dense_score) == (orig.rank, orig.score)  # retrieval kept
        assert h.payload is orig.payload or h.payload == orig.payload
        assert h.citation == orig.citation
    assert {"retrieval_ms", "rerank_ms", "total_ms", "dense_ms"} <= res.timings_ms.keys()


def test_hybrid_rerank_keeps_fused_provenance(services: dict[str, Any]) -> None:
    res = retriever(services, FakeReranker()).retrieve(QUERY, mode="hybrid", top_k=3, rerank=True)
    assert res.reranked and res.score_kind is ScoreKind.CROSS_ENCODER
    for h in res.hits:
        p = res.provenance[h.chunk_id]
        assert p.fused_score is not None and p.pre_rerank_score == p.fused_score


@pytest.mark.parametrize(
    "broken", [FakeReranker(fail=True), RerankError("cannot load reranker: missing")]
)
def test_rerank_failure_degrades_to_the_original_order(
    services: dict[str, Any], broken: Any
) -> None:
    res = retriever(services, broken).retrieve(QUERY, mode="dense", top_k=3, rerank=True)
    assert res.degraded == ("rerank",) and res.failures[0].retriever == "rerank"
    assert [h.chunk_id for h in res.hits] == [h.chunk_id for h in dense_hits(3)]
    assert [h.score for h in res.hits] == [h.score for h in dense_hits(3)]  # original scores
    assert not res.reranked and res.score_kind is ScoreKind.DENSE  # never labelled reranked
    assert all(res.provenance[h.chunk_id].rerank_score is None for h in res.hits)


def test_no_reranker_configured_is_a_labelled_degradation(services: dict[str, Any]) -> None:
    embedder: Any = FakeEmbedder
    store: Any = object
    keyword: Any = lambda: FakeManager(INDEX)  # noqa: E731
    r = Retriever(embedder=embedder, store=store, keyword=keyword)
    res = r.retrieve(QUERY, mode="dense", top_k=3, rerank=True)
    assert res.degraded == ("rerank",) and "no reranker" in res.failures[0].error


# --- CrossEncoderReranker: lazy, pinned, raw scores --------------------------------------------


def test_cross_encoder_loads_lazily_with_pinned_revision_and_raw_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentence_transformers
    import torch

    built: list[dict[str, Any]] = []

    class SpyCE:
        def __init__(self, name: str, **kw: Any) -> None:
            built.append({"name": name, **kw})

        def predict(self, pairs: list[tuple[str, str]], **kw: Any) -> list[float]:
            return [float(len(p[1])) for p in pairs]

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", SpyCE)
    r = CrossEncoderReranker("org/model", "abc123")
    assert not r.loaded and built == []  # nothing loaded at construction
    assert r.score("q", []) == [] and built == []  # nor for an empty candidate list
    assert r.score("q", ["aa", "a"]) == [2.0, 1.0]
    assert r.loaded and len(built) == 1
    kw = built[0]
    assert kw["name"] == "org/model" and kw["revision"] == "abc123"
    assert kw["device"] == "cpu" and kw["max_length"] == 512
    assert isinstance(kw["activation_fn"], torch.nn.Identity)  # raw logits, never squashed
    r.score("q", ["b"])
    assert len(built) == 1  # loaded once


def test_cross_encoder_load_failure_is_a_rerank_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentence_transformers

    def boom(*a: Any, **kw: Any) -> Any:
        raise OSError("model files not found")

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", boom)
    with pytest.raises(RerankError, match="cannot load reranker org/m@rev"):
        CrossEncoderReranker("org/m", "rev").score("q", ["x"])


def test_settings_default_off_and_pinned() -> None:
    s = get_settings()
    assert s.RERANK_ENABLED is False and s.RETRIEVAL_MODE == "dense"
    assert s.RERANK_MODEL == "cross-encoder/ms-marco-MiniLM-L6-v2"
    assert s.RERANK_MODEL_REVISION == "233902d25c440f23af6f7d6e94d2946bac0bee0a"
    assert s.RERANK_CANDIDATES == 20
    r = rerank_mod.reranker_from_settings(s)
    assert (r.model_name, r.revision, r.loaded) == (s.RERANK_MODEL, s.RERANK_MODEL_REVISION, False)


# --- CLI ---------------------------------------------------------------------------------------


def test_rerank_flag_parsing_and_frozen_protocols() -> None:
    p = cli.build_parser(get_settings())
    assert p.parse_args(["search", "q"]).rerank is None  # -> RERANK_ENABLED (off)
    assert p.parse_args(["search", "q", "--rerank"]).rerank is True
    assert p.parse_args(["search", "q", "--no-rerank"]).rerank is False
    for argv in (["evaluate", "--select", "--rerank"],
                 ["evaluate", "--final-heldout", "--record", "x.json", "--rerank"],
                 ["evaluate", "--compare", "--rerank"]):  # fmt: skip
        with pytest.raises(SystemExit):
            cli.main(argv)


def test_cli_default_search_does_not_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_search", lambda *a, **kw: seen.update(kw) or 0)
    assert cli.main(["search", "q"]) == 0 and seen["rerank"] is False
    assert cli.main(["search", "q", "--rerank"]) == 0 and seen["rerank"] is True


def search_cli(services: dict[str, Any], monkeypatch: pytest.MonkeyPatch, reranker: Any) -> int:
    monkeypatch.setattr(cli, "_retriever", lambda *a, **kw: retriever(services, reranker))
    sessions: Any = object()
    return cli._search(get_settings(), QUERY, top_k=3, filters=SearchFilters(), expand=False,
                       mode="dense", rerank=True, sessions=sessions)  # fmt: skip


def test_cli_rerank_output_labels_scores_and_provenance(
    services: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert search_cli(services, monkeypatch, FakeReranker()) == 0
    out = capsys.readouterr().out
    assert "; reranked from 20 candidates)" in out
    assert "scores: cross_encoder (raw, uncalibrated: an ordering score, not a confidence)" in out
    assert "→ retrieval rank #1" in out and "cross-encoder 0.000" in out
    assert "DEGRADED" not in out


def test_cli_rerank_failure_is_exit_0_with_a_banner(
    services: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert search_cli(services, monkeypatch, FakeReranker(fail=True)) == 0
    out = capsys.readouterr().out
    assert "DEGRADED: reranking failed (RerankError: reranker inference failed: boom)" in out
    assert "showing the original dense_cosine ranking" in out and "scores: dense_cosine" in out


def test_searchhit_contract_is_unchanged() -> None:
    import dataclasses

    assert [f.name for f in dataclasses.fields(SearchHit)] == ["rank", "score", "chunk_id",
                                                               "payload"]  # fmt: skip
