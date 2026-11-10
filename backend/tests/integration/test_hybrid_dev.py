"""M2 A2 hybrid retrieval with the REAL embedding model on the development stack.

Read-only against the development database and collection (`make ingest`). Like the dense
golden regression, it fails (never skips) when the collection or model is unavailable.
Checks that hybrid keeps the original dense ranks and scores of the M1 golden snapshot in
its provenance, uses Postgres payloads, and labels its scores as RRF. Development-set
queries only; the frozen held-out set is never read.
"""

import json
from typing import Any

import pytest

from app.config import REPO_ROOT, get_settings
from app.db.repository import eligible_chunk_payloads
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.retrieval.embeddings import embedder_from_settings
from app.retrieval.evaluation import load_cases
from app.retrieval.keyword import KeywordIndexManager
from app.retrieval.qdrant_store import store_from_settings
from app.retrieval.retrieve import Retriever, ScoreKind
from app.retrieval.search import SearchFilters
from tests.integration.test_dense_golden import GOLDEN

pytestmark = pytest.mark.integration


def test_hybrid_on_the_dev_stack_preserves_dense_provenance() -> None:
    golden: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    settings = get_settings()
    store = store_from_settings(settings)
    if not store.client.collection_exists(store.collection):
        pytest.fail(f"collection {store.collection!r} missing; run `make ingest`")
    sessions = create_sync_sessionmaker(create_sync_engine(settings))
    with sessions() as s:
        pg = {p["chunk_id"]: p for p in eligible_chunk_payloads(s)}
    assert len(pg) == store.count() == golden["qdrant_points"]
    embedder = embedder_from_settings(settings)
    r = Retriever(embedder=lambda: embedder, store=lambda: store,
                  keyword=lambda: KeywordIndexManager(sessions))  # fmt: skip
    cases = {c.id: c for c in load_cases(REPO_ROOT / golden["dataset"])}
    checked = 0
    for q in golden["queries"]:
        case = cases[q["id"]]
        f = SearchFilters(entity_terms=tuple(case.entity_terms))
        res = r.retrieve(case.query, mode="hybrid", top_k=10, filters=f)
        assert not res.is_degraded and res.score_kind is ScoreKind.RRF
        top5 = q["top5"]
        for h in res.hits:
            assert h.payload == pg[h.chunk_id]
            p = res.provenance[h.chunk_id]
            if p.dense_rank is not None and p.dense_rank <= len(top5):
                expected = top5[p.dense_rank - 1]
                assert h.chunk_id == expected["chunk_id"], (q["id"], p.dense_rank)
                assert p.dense_score == pytest.approx(
                    expected["score"], abs=golden["score_tolerance"]
                )
                checked += 1
    assert checked >= 40  # most golden dense hits survive into the fused top 10
