"""M2 B1 with the REAL pinned cross-encoder (`RERANK_MODEL` @ `RERANK_MODEL_REVISION`, CPU)
over the full-corpus throwaway database and collection (`FakeEmbedder` dense vectors).

Synthetic queries only: no development or held-out query is used, so nothing here is an
evaluation (the B2 diagnostic is a separate, approved step). Checks that reranking only
reorders the filtered candidates, keeps payloads, citations and parent expansion, keeps
the retrieval rank/score apart from the raw cross-encoder score, and reports latency.
"""

from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Chunk
from app.ingestion import cli
from app.retrieval.keyword import KeywordIndexManager
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.rerank import CrossEncoderReranker, reranker_from_settings
from app.retrieval.retrieve import Retriever, ScoreKind
from app.retrieval.search import SearchFilters
from tests.unit.test_indexing import FakeEmbedder

pytestmark = pytest.mark.integration
QUERIES = (
    "what must a company submit to open a bank account",
    "how should banks screen employees during hiring",
    "documents a trust needs for an account",
)


@pytest.fixture
def store(corpus_store: ChunkStore) -> ChunkStore:
    return corpus_store


@pytest.fixture
def sessions(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    return corpus_db


@pytest.fixture(scope="module")
def reranker() -> CrossEncoderReranker:
    r = reranker_from_settings(get_settings())
    r.score("warm-up", ["loads the pinned model once for this module"])
    return r


def make(sessions: sessionmaker[Session], store: ChunkStore, reranker: Any) -> Retriever:
    return Retriever(embedder=FakeEmbedder, store=lambda: store,
                     keyword=lambda: KeywordIndexManager(sessions), reranker=lambda: reranker,
                     rerank_candidates=20)  # fmt: skip


def test_real_model_is_the_pinned_revision(reranker: CrossEncoderReranker) -> None:
    s = get_settings()
    assert (reranker.model_name, reranker.revision) == (s.RERANK_MODEL, s.RERANK_MODEL_REVISION)
    assert reranker.loaded and reranker.load_ms > 0


@pytest.mark.parametrize("query", QUERIES)
def test_reranking_reorders_the_filtered_candidates_only(
    sessions: sessionmaker[Session], store: ChunkStore, reranker: Any, query: str
) -> None:
    r = make(sessions, store, reranker)
    base = r.retrieve(query, mode="dense", top_k=20)
    res = r.retrieve(query, mode="dense", top_k=5, rerank=True)
    assert res.reranked and not res.is_degraded and res.score_kind is ScoreKind.CROSS_ENCODER
    by_id = {h.chunk_id: h for h in base.hits}
    assert {h.chunk_id for h in res.hits} <= by_id.keys() and len(res.hits) == 5
    scores = [h.score for h in res.hits]
    assert scores == sorted(scores, reverse=True)
    for h in res.hits:
        orig = by_id[h.chunk_id]
        p = res.provenance[h.chunk_id]
        assert h.payload == orig.payload and h.citation == orig.citation  # unchanged
        assert (p.pre_rerank_rank, p.pre_rerank_score) == (orig.rank, orig.score)
        assert (p.dense_rank, p.dense_score) == (orig.rank, orig.score)
        assert p.rerank_score == h.score
    assert res.timings_ms["rerank_ms"] > 0 and "retrieval_ms" in res.timings_ms


def test_filters_and_eligibility_hold_under_reranking(
    sessions: sessionmaker[Session], store: ChunkStore, reranker: Any
) -> None:
    f = SearchFilters(entity_terms=("TRUST",), mandatory_evidence_only=True)
    res = make(sessions, store, reranker).retrieve(QUERIES[2], mode="dense", top_k=5,
                                                   filters=f, rerank=True)  # fmt: skip
    assert res.hits and all(f.matches(h.payload) for h in res.hits)
    assert all(h.payload["can_support_mandatory"] is True for h in res.hits)


def test_hybrid_rerank_on_the_real_stores(
    sessions: sessionmaker[Session], store: ChunkStore, reranker: Any
) -> None:
    res = make(sessions, store, reranker).retrieve(QUERIES[0], mode="hybrid", top_k=5,
                                                   rerank=True)  # fmt: skip
    assert res.reranked and len(res.hits) == 5
    assert all(res.provenance[h.chunk_id].fused_score is not None for h in res.hits)


def test_cli_rerank_with_parent_expansion(
    sessions: sessionmaker[Session],
    store: ChunkStore,
    reranker: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A query aimed at text that exists only in a child chunk (169MD para 27(1)(v)); the
    # expected hits are computed with the same pinned model, then compared with the CLI.
    query = "face liveness and spoof detection in the video KYC application"
    res = make(sessions, store, reranker).retrieve(query, mode="dense", top_k=5, rerank=True)
    children = [h for h in res.hits if h.payload.get("parent_chunk_id")]
    assert children, "the reranked top 5 should contain a child chunk for this query"
    with sessions() as s:
        parents = {h.chunk_id: s.get(Chunk, h.payload["parent_chunk_id"]) for h in children}
    assert cli._search(get_settings(), query, top_k=5, filters=SearchFilters(), expand=True,
                       mode="dense", rerank=True, embedder=FakeEmbedder(), store=store,
                       sessions=sessions) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "reranked from 20 candidates" in out and "scores: cross_encoder (raw" in out
    assert "→ retrieval rank #" in out and "DEGRADED" not in out
    assert out.count("parent paragraph") == len(children)  # every child expands, once
    flat = " ".join(out.split())
    for parent in parents.values():
        assert parent is not None and " ".join(parent.content.split())[:60] in flat
