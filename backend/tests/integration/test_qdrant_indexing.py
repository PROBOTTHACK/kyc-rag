"""Stage D on real services: BGE-small-en-v1.5 and the docker Qdrant (`make up`).

Each test writes to its own throwaway collection (`test_<uuid>`), deleted afterwards;
the development collection is never touched. Needs the embedding model in the local
Hugging Face cache (first download ~135 MB) and the gitignored corpus PDFs.
"""

import math
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import REPO_ROOT, get_settings
from app.ingestion.chunking import ChunkingResult
from app.ingestion.manifest import load_manifest
from app.ingestion.prepare import ocr_setup, prepare_document
from app.retrieval.embeddings import (
    EmbeddingError,
    SentenceTransformerEmbedder,
    embedder_from_settings,
    embedding_input,
)
from app.retrieval.indexing import INDEX_KEY, index_document
from app.retrieval.qdrant_store import ChunkStore, store_from_settings
from app.retrieval.search import SearchFilters, search

pytestmark = pytest.mark.integration
CORPUS = REPO_ROOT / "data" / "raw_pdfs"
EXPECTED_ELIGIBLE = 429


@pytest.fixture(scope="module")
def embedder() -> SentenceTransformerEmbedder:
    emb = embedder_from_settings(get_settings())
    try:
        _ = emb.dimension
    except EmbeddingError as exc:
        pytest.skip(f"embedding model unavailable: {exc}")
    return emb


@pytest.fixture(scope="module")
def corpus() -> dict[str, ChunkingResult]:
    if not (CORPUS / "regulatory" / "169MD.pdf").exists():
        pytest.skip("source PDFs not present")
    settings = get_settings()
    setup, err = ocr_setup(settings)
    return {
        e.document_key: prepare_document(e, settings, setup=setup, setup_error=err)
        for e in load_manifest(settings.CORPUS_MANIFEST)
    }


@pytest.fixture
def store() -> Iterator[ChunkStore]:
    base = store_from_settings(get_settings())
    s = ChunkStore(base.client, f"test_{uuid.uuid4().hex[:12]}")
    try:
        yield s
    finally:
        if s.client.collection_exists(s.collection):
            s.client.delete_collection(s.collection)


# --- the model ------------------------------------------------------------------------


def test_model_shape_normalisation_and_determinism(embedder: SentenceTransformerEmbedder) -> None:
    assert embedder.dimension == 384 and embedder.max_tokens == 512
    assert embedder.revision == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    texts = ["33. For opening an account of a company ...", "Proof of Address (C)"]
    a, b = embedder.embed_passages(texts), embedder.embed_passages(texts)
    for va, vb in zip(a, b, strict=True):
        assert len(va) == 384
        assert math.isclose(math.sqrt(sum(x * x for x in va)), 1.0, abs_tol=1e-5)
        assert max(abs(x - y) for x, y in zip(va, vb, strict=True)) < 1e-6
    # Queries carry the BGE instruction, so they differ from the same text as a passage.
    q = embedder.embed_query(texts[1])
    assert max(abs(x - y) for x, y in zip(q, a[1], strict=True)) > 1e-3


def test_overlong_input_is_rejected_not_truncated(embedder: SentenceTransformerEmbedder) -> None:
    with pytest.raises(EmbeddingError, match="would be truncated"):
        embedder.embed_passages(["word " * 600])


def test_every_corpus_input_fits_the_model(
    embedder: SentenceTransformerEmbedder, corpus: dict[str, ChunkingResult]
) -> None:
    inputs = [
        embedding_input(c.metadata) for r in corpus.values() for c in r.chunks if c.embeddable
    ]
    assert len(inputs) == EXPECTED_ELIGIBLE
    assert max(embedder.token_count(t) for t in inputs) <= embedder.max_tokens


# --- indexing the corpus into real Qdrant ---------------------------------------------


def index_all(
    corpus: dict[str, ChunkingResult], emb: SentenceTransformerEmbedder, store: ChunkStore
) -> list[Any]:
    store.ensure_collection(emb.dimension)
    return [index_document(r, emb, store) for r in corpus.values()]


def test_corpus_indexing_is_complete_and_idempotent(
    embedder: SentenceTransformerEmbedder, corpus: dict[str, ChunkingResult], store: ChunkStore
) -> None:
    first = index_all(corpus, embedder, store)
    assert sum(r.embedded for r in first) == EXPECTED_ELIGIBLE
    assert store.count() == EXPECTED_ELIGIBLE
    schema = store.client.get_collection(store.collection).payload_schema
    assert {"evidence_eligibility", "can_support_mandatory", "text_reliability"} <= set(schema)

    expected = {
        c.metadata.chunk_id: c.metadata.to_qdrant_payload()
        for r in corpus.values() for c in r.chunks if c.embeddable
    }  # fmt: skip
    points, _ = store.client.scroll(store.collection, limit=1000, with_payload=True)
    assert len(points) == EXPECTED_ELIGIBLE
    for p in points:
        payload = p.payload or {}
        assert {k: v for k, v in payload.items() if k != INDEX_KEY} == expected[str(p.id)]
        assert payload["chunk_role"] != "parent"

    second = index_all(corpus, embedder, store)
    assert sum(r.embedded + r.payload_updated + r.stale_deleted for r in second) == 0
    assert sum(r.unchanged for r in second) == EXPECTED_ELIGIBLE
    assert store.count() == EXPECTED_ELIGIBLE


def test_reindex_removes_stale_vectors(
    embedder: SentenceTransformerEmbedder, corpus: dict[str, ChunkingResult], store: ChunkStore
) -> None:
    faq = corpus["rbi_kyc_faqs"]
    store.ensure_collection(embedder.dimension)
    index_document(faq, embedder, store)
    shorter = ChunkingResult(faq.document_key, faq.profile, faq.chunks[:-2], policy=faq.policy)
    report = index_document(shorter, embedder, store)
    assert report.stale_deleted == 2 and report.embedded == 0
    assert store.count() == len(faq.chunks) - 2
    assert index_document(faq, embedder, store).embedded == 2  # restored, not duplicated
    assert store.count() == len(faq.chunks)


# --- retrieval over the real index ----------------------------------------------------


def test_retrieval_respects_eligibility_and_finds_known_passages(
    embedder: SentenceTransformerEmbedder, corpus: dict[str, ChunkingResult], store: ChunkStore
) -> None:
    index_all(corpus, embedder, store)

    def cites(query: str, **kw: Any) -> list[tuple[str, int, str | None]]:
        hits = search(query, embedder, store, filters=SearchFilters(**kw))
        return [(h.payload["document_key"], h.payload["page_number"], h.payload["paragraph"])
                for h in hits]  # fmt: skip

    assert ("rbi_kyc_directions_2025", 37, "33") in cites(
        "certified copies needed from a private limited company customer"
    )
    assert ("hdfc_nri_rekyc", 3, None) in cites("overseas address proof list for NRI re-KYC")
    netbanking = "board resolution for netbanking facility"
    assert all(k != "hdfc_netbanking_corporate_application" for k, _, _ in cites(netbanking))
    assert cites(netbanking, include_context_only=True)[0][0] == (
        "hdfc_netbanking_corporate_application"
    )
    mandatory = search("documents for a partnership firm", embedder, store,
                       filters=SearchFilters(mandatory_evidence_only=True))  # fmt: skip
    assert mandatory and all(h.payload["can_support_mandatory"] for h in mandatory)
    assert all(h.payload["text_reliability"] == "native" for h in mandatory)
