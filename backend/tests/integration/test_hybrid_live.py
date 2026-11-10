"""M2 A2 on real services: hybrid retrieval, degraded results and reconcile.

Uses the full-corpus throwaway database and Qdrant collection of the A1 tests (vectors
from the deterministic `FakeEmbedder`; the real model is exercised in
tests/integration/test_hybrid_dev.py). Failures are injected for real: an unreachable
Qdrant port, an unreachable Postgres port, and both.
"""

import itertools
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from qdrant_client import QdrantClient
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Chunk
from app.db.repository import eligible_chunk_payloads
from app.ingestion import cli
from app.retrieval.keyword import KeywordIndexManager
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.retrieve import RetrievalError, Retriever, ScoreKind
from app.retrieval.search import SearchFilters
from tests.integration.test_keyword_live import (
    BANKS,
    CONTEXT_ONLY,
    DOCUMENT_KEYS,
    ENTITY_TERMS,
    MANDATORY_ONLY,
)
from tests.unit.test_indexing import FakeEmbedder

pytestmark = pytest.mark.integration


@pytest.fixture
def store(corpus_store: ChunkStore) -> ChunkStore:
    return corpus_store


@pytest.fixture
def sessions(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    return corpus_db  # requesting `corpus_store` guarantees the corpus is ingested


def retriever(sessions: sessionmaker[Session], store: ChunkStore, **kw: Any) -> Retriever:
    return Retriever(embedder=FakeEmbedder, store=lambda: store,
                     keyword=lambda: KeywordIndexManager(sessions), **kw)  # fmt: skip


def dead_store() -> ChunkStore:
    client = QdrantClient(url="http://localhost:1", timeout=1, check_compatibility=False)
    s = ChunkStore(client, "nowhere")
    s.sleep = lambda _: None  # keep the bounded retries, skip their waiting
    return s


def dead_sessions() -> sessionmaker[Session]:
    bad = make_url(get_settings().DATABASE_URL.get_secret_value()).set(port=1)
    return sessionmaker(sa.create_engine(bad, connect_args={"connect_timeout": 2}))


QUERY = "documents for opening an account of a trust"


# --- normal hybrid: parity of filters, payloads, citations -------------------------------------


def test_hybrid_respects_every_filter_combination_with_postgres_payloads(
    sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    with sessions() as s:
        pg = {p["chunk_id"]: p for p in eligible_chunk_payloads(s)}
    r = retriever(sessions, store)
    nonempty = 0
    for ctx, mand, banks, docs, ents in itertools.product(
        CONTEXT_ONLY, MANDATORY_ONLY, BANKS, DOCUMENT_KEYS, ENTITY_TERMS
    ):
        f = SearchFilters(include_context_only=ctx, mandatory_evidence_only=mand, banks=banks,
                          document_keys=docs, entity_terms=ents)  # fmt: skip
        res = r.retrieve(QUERY, mode="hybrid", top_k=10, filters=f)
        assert not res.is_degraded and res.score_kind is ScoreKind.RRF
        for h in res.hits:
            assert f.matches(h.payload), (f, h.citation)
            assert h.payload == pg[h.chunk_id]  # Postgres-built payload, no `index`
            assert h.score == res.provenance[h.chunk_id].fused_score
        nonempty += bool(res.hits)
    assert nonempty > 100  # the matrix is not vacuous


def test_hybrid_citations_match_the_single_modes(
    sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    r = retriever(sessions, store)
    hybrid = r.retrieve(QUERY, mode="hybrid", top_k=10)
    single = {
        h.chunk_id: h
        for mode in ("dense", "bm25")
        for h in r.retrieve(QUERY, mode=mode, top_k=50).hits
    }
    for h in hybrid.hits:
        p = hybrid.provenance[h.chunk_id]
        assert h.citation == single[h.chunk_id].citation
        if p.bm25_rank is not None:  # original BM25 rank and score preserved
            bm25 = r.retrieve(QUERY, mode="bm25", top_k=50).hits[p.bm25_rank - 1]
            assert (bm25.chunk_id, bm25.score) == (h.chunk_id, p.bm25_score)
        if p.dense_rank is not None:
            dense = r.retrieve(QUERY, mode="dense", top_k=50).hits[p.dense_rank - 1]
            assert (dense.chunk_id, dense.score) == (h.chunk_id, p.dense_score)


def test_hybrid_cli_expands_parents(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    with sessions() as s:
        child = s.scalars(
            sa.select(Chunk)
            .where(Chunk.chunk_role == "child")
            .order_by(Chunk.document_key, Chunk.chunk_index)
            .limit(1)
        ).one()
        parent = s.get(Chunk, child.parent_chunk_id)
    assert parent is not None
    query = " ".join(child.content.split()[:40])
    assert cli._search(get_settings(), query, top_k=5, filters=SearchFilters(), expand=True,
                       mode="hybrid", embedder=FakeEmbedder(), store=store,
                       sessions=sessions) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "scores: rrf" in out and "via: " in out and "DEGRADED" not in out
    assert "parent paragraph" in out
    assert " ".join(parent.content.split())[:60] in " ".join(out.split())


# --- failure injection (real unreachable services) ---------------------------------------------


def test_dense_failure_degrades_to_bm25(sessions: sessionmaker[Session], store: ChunkStore) -> None:
    res = retriever(sessions, dead_store()).retrieve(QUERY, mode="hybrid", top_k=5)
    assert res.degraded == ("dense",) and res.failures[0].retriever == "dense"
    assert res.score_kind is ScoreKind.BM25
    bm25 = retriever(sessions, store).retrieve(QUERY, mode="bm25", top_k=5)
    assert res.hits == bm25.hits  # BM25's own ranking and scores
    assert all(p.retrievers == ("bm25",) for p in res.provenance.values())


def test_bm25_failure_degrades_to_dense(sessions: sessionmaker[Session], store: ChunkStore) -> None:
    res = retriever(dead_sessions(), store).retrieve(QUERY, mode="hybrid", top_k=5)
    assert res.degraded == ("bm25",) and "KeywordIndexUnavailable" in res.failures[0].error
    assert res.score_kind is ScoreKind.DENSE
    dense = retriever(sessions, store).retrieve(QUERY, mode="dense", top_k=5)
    assert [(h.chunk_id, h.score) for h in res.hits] == [(h.chunk_id, h.score) for h in dense.hits]
    assert all("index" not in h.payload for h in res.hits)
    assert all(p.retrievers == ("dense",) for p in res.provenance.values())


def test_both_failing_is_an_error(sessions: sessionmaker[Session]) -> None:
    with pytest.raises(RetrievalError) as err:
        retriever(dead_sessions(), dead_store()).retrieve(QUERY, mode="hybrid")
    assert [f.retriever for f in err.value.failures] == ["dense", "bm25"]


def test_cli_exit_codes_for_degraded_and_failed_searches(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    def run(store: ChunkStore, sess: sessionmaker[Session]) -> int:
        return cli._search(get_settings(), QUERY, top_k=5, filters=SearchFilters(),
                           expand=False, mode="hybrid", embedder=FakeEmbedder(), store=store,
                           sessions=sess)  # fmt: skip

    assert run(dead_store(), sessions) == 0
    out = capsys.readouterr().out
    assert "DEGRADED: dense retrieval failed" in out and "scores: bm25" in out
    assert run(dead_store(), dead_sessions()) == 2
    assert "search failed: every retriever failed" in capsys.readouterr().out


# --- reconcile: the keyword line is informational only -----------------------------------------


def test_reconcile_verdicts_and_exit_codes_are_unchanged(
    sessions: sessionmaker[Session],
    store: ChunkStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reconcile(**kw: Any) -> int:
        return cli._reconcile(get_settings(), embedder=FakeEmbedder(), store=store,
                              sessions=sessions, **kw)  # fmt: skip

    assert reconcile(do_repair=False, delete_unknown=False) == 0
    out = capsys.readouterr().out
    assert "CONSISTENT" in out
    assert "keyword index (informational): 429 chunks; Postgres eligible 429; Qdrant 429" in out

    victim = next(iter(store.all_point_states()))
    store.delete([victim])
    try:
        assert reconcile(do_repair=False, delete_unknown=False) == 1  # M1 verdict and code
        out = capsys.readouterr().out
        assert "Qdrant 428" in out and "keyword index (informational)" in out
        broken = dead_sessions()
        monkeypatch.setattr(cli, "KeywordIndexManager", lambda s: KeywordIndexManager(broken))
        assert reconcile(do_repair=False, delete_unknown=False) == 1  # unaffected by it
        assert "keyword index (informational): unavailable" in capsys.readouterr().out
    finally:
        monkeypatch.undo()
        assert reconcile(do_repair=True, delete_unknown=False) == 0  # M1 repair restores
    assert store.count() == 429
    assert str(uuid.UUID(victim)) in store.all_point_states()
