"""M1 dense retrieval golden regression (M2 Stage 0 snapshot): never skipped.

Runs against the development database and collection (`make ingest`) with the real
embedding model, read-only. If the collection, the corpus or the model is missing or
differs from the snapshot's provenance, the test FAILS with the reason: a regression check
that silently skipped would prove nothing. Run: `make check-dense-golden`.
"""

import json
from typing import Any

import pytest

from app.config import REPO_ROOT, get_settings
from app.retrieval.embeddings import embedder_from_settings
from app.retrieval.evaluation import evaluate, load_cases
from app.retrieval.qdrant_store import store_from_settings
from app.retrieval.search import SearchFilters, search

pytestmark = pytest.mark.integration
GOLDEN = REPO_ROOT / "backend" / "tests" / "fixtures" / "m1_dense_golden.json"


def test_dense_retrieval_reproduces_the_m1_golden_snapshot() -> None:
    golden: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    settings = get_settings()
    store = store_from_settings(settings)
    try:
        exists = store.client.collection_exists(store.collection)
    except Exception as exc:  # noqa: BLE001 - report any connection problem as a failure
        pytest.fail(f"Qdrant unreachable ({type(exc).__name__}: {exc}); run `make up`")
    if not exists:
        pytest.fail(f"collection {store.collection!r} missing; run `make ingest`")
    assert store.collection == golden["collection"]
    assert store.count() == golden["qdrant_points"], "dev collection differs from the snapshot"
    embedder = embedder_from_settings(settings)
    assert (embedder.model_name, embedder.revision) == (
        golden["embedding_model"], golden["embedding_revision"]
    )  # fmt: skip
    _ = embedder.dimension  # loads the model; an EmbeddingError fails the test

    cases = load_cases(REPO_ROOT / golden["dataset"])
    assert [c.id for c in cases] == [q["id"] for q in golden["queries"]]
    tol = float(golden["score_tolerance"])
    for case, expected in zip(cases, golden["queries"], strict=True):
        hits = search(case.query, embedder, store, top_k=golden["k"],
                      filters=SearchFilters(entity_terms=tuple(case.entity_terms)))  # fmt: skip
        assert [h.chunk_id for h in hits] == [e["chunk_id"] for e in expected["top5"]], case.id
        assert [h.citation for h in hits] == [e["citation"] for e in expected["top5"]], case.id
        for h, e in zip(hits, expected["top5"], strict=True):
            assert abs(h.score - e["score"]) <= tol, (case.id, h.rank, h.score, e["score"])

    report = evaluate(cases, embedder, store, k=golden["k"], dataset=golden["dataset"])
    m = golden["metrics"]
    assert (report.mean_recall, report.hit_rate, report.mrr) == pytest.approx(
        (m["mean_recall"], m["hit_rate"], m["mrr"])
    )
