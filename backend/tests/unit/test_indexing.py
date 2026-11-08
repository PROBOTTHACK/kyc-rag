"""Stage D indexing and retrieval logic on an in-memory Qdrant with a fake embedder.

Real BGE + real Qdrant are covered in tests/integration/test_qdrant_indexing.py.
"""

import hashlib
import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from qdrant_client import QdrantClient

from app.ingestion.chunking import ChunkingResult, ChunkRecord
from app.retrieval.embeddings import (
    BGE_QUERY_INSTRUCTION,
    EMBEDDING_INPUT_VERSION,
    embedding_input,
    input_sha256,
)
from app.retrieval.indexing import (
    INDEX_KEY,
    find_orphans,
    index_document,
    payload_sha256,
    prune_orphans,
)
from app.retrieval.qdrant_store import ChunkStore, QdrantSchemaError
from app.retrieval.search import SearchFilters, build_filter, search
from app.schemas.corpus import ChunkMetadata, DocumentMetadata
from app.schemas.taxonomy import (
    ChunkingProfile,
    ChunkRole,
    ContentKind,
    EntityType,
    ExtractionMethod,
)

# In-memory Qdrant ignores payload indexes (the real server is covered by integration tests).
pytestmark = pytest.mark.filterwarnings(
    "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
)
DIM = 8


class FakeEmbedder:
    """Deterministic unit vectors from a hash of the text; counts calls."""

    model_name = "fake/bge"

    def __init__(self, revision: str | None = "rev1", fail: bool = False) -> None:
        self.revision = revision
        self.fail = fail
        self.passages: list[str] = []
        self.queries: list[str] = []

    @property
    def dimension(self) -> int:
        return DIM

    @staticmethod
    def _vec(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b - 127.5 for b in digest[:DIM]]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("model unavailable")
        self.passages.extend(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vec(text)


def doc(key: str = "synthetic_doc", **kw: Any) -> DocumentMetadata:
    data: dict[str, Any] = {
        "document_key": key,
        "file": f"x/{key}.pdf",
        "sha256": hashlib.sha256(key.encode()).hexdigest(),
        "bank": "RBI",
        "document_type": "regulatory",
        "source_authority": "RBI",
        "attribution_basis": "document_text",
        "title": "Synthetic Directions",
        "source_status": "active",
        "entity_types": ["INDIVIDUAL", "COMPANY", "PARTNERSHIP"],
    }
    data.update(kw)
    return DocumentMetadata.model_validate(data)


def chunk(d: DocumentMetadata, i: int, text: str, **kw: Any) -> ChunkRecord:
    meta = ChunkMetadata.for_document(
        d,
        chunk_index=i,
        page_number=1 + i,
        content=text,
        extraction_method=ExtractionMethod.TEXT,
        **kw,
    )
    return ChunkRecord(metadata=meta, extras={"raw_text": text})


def result(d: DocumentMetadata, records: list[ChunkRecord]) -> ChunkingResult:
    return ChunkingResult(d.document_key, ChunkingProfile.RBI_DIRECTIONS, records)


def sample(d: DocumentMetadata) -> ChunkingResult:
    parent = chunk(
        d, 0, "7. Full paragraph text.", chunk_role=ChunkRole.PARENT, embedding_eligible=False
    )
    pid = parent.metadata.chunk_id
    return result(
        d,
        [
            parent,
            chunk(d, 1, "(1) First part.", chunk_role=ChunkRole.CHILD, parent_chunk_id=pid),
            chunk(
                d,
                2,
                "(2) Second part.",
                chunk_role=ChunkRole.CHILD,
                parent_chunk_id=pid,
                context="7. Full paragraph lead",
            ),
            chunk(d, 3, "8. A company shall submit documents.", entity_types=[]),
        ],
    )


@pytest.fixture
def store() -> ChunkStore:
    s = ChunkStore(QdrantClient(":memory:"), "test_chunks")
    s.ensure_collection(DIM)
    return s


def payloads(store: ChunkStore) -> dict[str, dict[str, Any]]:
    points, _ = store.client.scroll(store.collection, limit=100, with_payload=True)
    return {str(p.id): p.payload or {} for p in points}


# --- embedding input ------------------------------------------------------------------


def test_embedding_input_is_context_plus_content() -> None:
    d = doc()
    single = chunk(d, 0, "8. Text.", section="Chapter VI > C. Legal Entities").metadata
    child = chunk(d, 1, "(2) Part.", context="7. Lead").metadata
    assert embedding_input(single) == "8. Text."  # title/section not embedded
    assert embedding_input(child) == "7. Lead\n(2) Part."
    assert EMBEDDING_INPUT_VERSION == 2
    assert BGE_QUERY_INSTRUCTION.startswith("Represent this sentence")


# --- collection -----------------------------------------------------------------------


def test_ensure_collection_is_idempotent_and_refuses_mismatch() -> None:
    s = ChunkStore(QdrantClient(":memory:"), "c")
    assert s.ensure_collection(DIM) is True
    assert s.ensure_collection(DIM) is False
    with pytest.raises(QdrantSchemaError, match="never recreated automatically"):
        s.ensure_collection(DIM + 1)


# --- indexing -------------------------------------------------------------------------


def test_first_index_skips_parents_and_stores_metadata(store: ChunkStore) -> None:
    d = doc()
    res = sample(d)
    emb = FakeEmbedder()
    report = index_document(res, emb, store)
    assert (report.chunks, report.eligible, report.embedded) == (4, 3, 3)
    stored = payloads(store)
    eligible = [c for c in res.chunks if c.embeddable]
    assert set(stored) == {c.metadata.chunk_id for c in eligible}  # parent not indexed
    for rec in eligible:
        p = stored[rec.metadata.chunk_id]
        body = {k: v for k, v in p.items() if k != INDEX_KEY}
        assert body == rec.metadata.to_qdrant_payload()
        idx = p[INDEX_KEY]
        assert idx["embedding_model"] == "fake/bge" and idx["embedding_revision"] == "rev1"
        assert idx["input_version"] == EMBEDDING_INPUT_VERSION
        assert idx["input_sha256"] == input_sha256(
            f"{EMBEDDING_INPUT_VERSION}|{embedding_input(rec.metadata)}"
        )
        assert idx["payload_sha256"] == payload_sha256(p)
    child = stored[res.chunks[2].metadata.chunk_id]
    assert child["parent_chunk_id"] == res.chunks[0].metadata.chunk_id  # expandable


def test_reindex_is_idempotent(store: ChunkStore) -> None:
    res = sample(doc())
    index_document(res, FakeEmbedder(), store)
    before = payloads(store)
    emb = FakeEmbedder()
    report = index_document(res, emb, store)
    assert (report.embedded, report.payload_updated, report.unchanged, report.stale_deleted) == (
        0,
        0,
        3,
        0,
    )
    assert emb.passages == [] and not report.changed
    assert payloads(store) == before


def test_force_reembeds_everything(store: ChunkStore) -> None:
    res = sample(doc())
    index_document(res, FakeEmbedder(), store)
    emb = FakeEmbedder()
    assert index_document(res, emb, store, force=True).embedded == 3
    assert len(emb.passages) == 3


def test_metadata_only_change_keeps_vector(store: ChunkStore) -> None:
    d = doc()
    res = sample(d)
    index_document(res, FakeEmbedder(), store)
    target = res.chunks[3]
    changed = replace(
        target, metadata=target.metadata.model_copy(update={"section": "New section"})
    )
    res2 = result(d, [*res.chunks[:3], changed])
    emb = FakeEmbedder()
    report = index_document(res2, emb, store)
    assert (report.embedded, report.payload_updated, report.unchanged) == (0, 1, 2)
    assert emb.passages == []
    assert payloads(store)[target.metadata.chunk_id]["section"] == "New section"


def test_content_or_model_change_reembeds(store: ChunkStore) -> None:
    d = doc()
    res = sample(d)
    index_document(res, FakeEmbedder(), store)
    target = res.chunks[3]
    edited = replace(
        target, metadata=target.metadata.model_copy(update={"content": "8. Edited text."})
    )
    assert index_document(result(d, [*res.chunks[:3], edited]), FakeEmbedder(), store).embedded == 1
    # A new model revision invalidates every vector.
    assert (
        index_document(result(d, [*res.chunks[:3], edited]), FakeEmbedder("rev2"), store).embedded
        == 3
    )


def test_stale_points_are_removed_after_upsert(store: ChunkStore) -> None:
    d = doc()
    res = sample(d)
    index_document(res, FakeEmbedder(), store)
    gone = res.chunks[3].metadata.chunk_id
    report = index_document(result(d, res.chunks[:3]), FakeEmbedder(), store)
    assert report.stale_ids == [gone] and report.stale_deleted == 1
    assert gone not in payloads(store) and store.count() == 2


def test_policy_excluded_chunk_is_unindexed(store: ChunkStore) -> None:
    d = doc()
    res = sample(d)
    index_document(res, FakeEmbedder(), store)
    target = res.chunks[3]
    excluded = replace(
        target, metadata=target.metadata.model_copy(update={"embedding_eligible": False})
    )
    report = index_document(result(d, [*res.chunks[:3], excluded]), FakeEmbedder(), store)
    assert report.eligible == 2 and report.stale_deleted == 1
    assert target.metadata.chunk_id not in payloads(store)


def test_embedding_failure_leaves_existing_vectors(store: ChunkStore) -> None:
    d = doc()
    res = sample(d)
    index_document(res, FakeEmbedder(), store)
    before = payloads(store)
    with pytest.raises(RuntimeError, match="model unavailable"):
        index_document(res, FakeEmbedder(fail=True), store, force=True)
    assert payloads(store) == before


def test_documents_are_isolated_and_orphans_need_prune(store: ChunkStore) -> None:
    a, b = doc("doc_a"), doc("doc_b")
    index_document(sample(a), FakeEmbedder(), store)
    index_document(sample(b), FakeEmbedder(), store)
    # Re-indexing doc_a with fewer chunks never touches doc_b.
    index_document(result(a, sample(a).chunks[:2]), FakeEmbedder(), store)
    assert store.document_keys() == {"doc_a": 1, "doc_b": 3}
    assert find_orphans(store, ["doc_a"]) == {"doc_b": 3}
    assert store.count() == 4  # reporting deletes nothing
    assert prune_orphans(store, ["doc_b"]) == 3
    assert store.document_keys() == {"doc_a": 1}


# --- search ---------------------------------------------------------------------------


def corpus_store(store: ChunkStore) -> FakeEmbedder:
    emb = FakeEmbedder()
    rbi = doc("rbi_doc")
    form = doc(
        "form_doc",
        bank="HDFC",
        document_type="form",
        source_authority="HDFC",
        source_status="unknown",
        status_reason="synthetic",
    )
    ref = doc("ref_doc", bank="HDFC", document_type="reference", source_authority="HDFC")
    rbi_chunk = chunk(rbi, 0, "company documents", entity_types=[EntityType.COMPANY])
    index_document(result(rbi, [rbi_chunk]), emb, store)
    index_document(result(form, [chunk(form, 0, "form instruction")]), emb, store)
    index_document(result(ref, [chunk(ref, 0, "netbanking resolution")]), emb, store)
    return emb


def keys(hits: list[Any]) -> set[str]:
    return {h.payload["document_key"] for h in hits}


def test_search_eligibility_and_entity_filters(store: ChunkStore) -> None:
    emb = corpus_store(store)
    assert keys(search("anything", emb, store, top_k=10)) == {"rbi_doc", "form_doc"}
    assert keys(
        search("anything", emb, store, top_k=10, filters=SearchFilters(include_context_only=True))
    ) == {
        "rbi_doc",
        "form_doc",
        "ref_doc",
    }
    only = search("x", emb, store, filters=SearchFilters(mandatory_evidence_only=True))
    assert keys(only) == {"rbi_doc"} and all(h.payload["can_support_mandatory"] for h in only)
    legal = search("x", emb, store, top_k=10, filters=SearchFilters(entity_terms=("LEGAL_ENTITY",)))
    assert "rbi_doc" in keys(legal)  # COMPANY is a LEGAL_ENTITY member
    hdfc = search("x", emb, store, top_k=10, filters=SearchFilters(banks=("HDFC",)))
    assert keys(hdfc) == {"form_doc"}
    assert emb.queries[-1] == "x"  # the instruction is added inside the real embedder
    with pytest.raises(ValueError, match="empty"):
        search("  ", emb, store)


def test_citation_format() -> None:
    from app.retrieval.search import SearchHit

    hit = SearchHit(
        1,
        0.9,
        "id",
        {
            "document_key": "rbi",
            "page_number": 37,
            "page_end": 38,
            "region": None,
            "paragraph": "34",
        },
    )
    assert hit.citation == "rbi, pp.37-38, para 34"
    fold = SearchHit(
        1,
        0.9,
        "id",
        {
            "document_key": "form",
            "page_number": 1,
            "page_end": None,
            "region": "left",
            "paragraph": None,
        },
    )
    assert fold.citation == "form, p.1, left half"


def test_default_filter_excludes_context_only() -> None:
    flt = build_filter(SearchFilters())
    cond = flt.must[0]  # type: ignore[index]
    assert cond.key == "evidence_eligibility"  # type: ignore[union-attr]
    assert sorted(cond.match.any) == ["primary", "supporting"]  # type: ignore[union-attr]


def test_content_kind_is_part_of_payload(store: ChunkStore) -> None:
    d = doc()
    index_document(
        result(d, [chunk(d, 0, "row", content_kind=ContentKind.TABLE_ROW)]), FakeEmbedder(), store
    )
    assert next(iter(payloads(store).values()))["content_kind"] == "table_row"
