"""Stage E row mapping and pre-write validation (no database)."""

from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.db.corpus import (
    chunk_metadata_from_rows,
    chunk_record_from_rows,
    chunk_row,
    document_row,
)
from app.db.persistence import (
    PersistError,
    completion_status,
    derive_quality,
    ingestion_summary,
    validate_prepared,
)
from app.ingestion.chunking import ChunkRecord, Exclusion
from app.ingestion.pages import ProcessedPage
from app.ingestion.prepare import PreparedDocument
from app.schemas.corpus import FootnoteRef
from app.schemas.taxonomy import (
    ContentKind,
    DocumentQuality,
    ExtractionMethod,
    IngestionStatus,
    PageStatus,
    TextReliability,
)
from tests.unit.test_indexing import chunk, doc, result, sample


def page(n: int, method: ExtractionMethod | None, status: PageStatus = PageStatus.OK) -> Any:
    return SimpleNamespace(page_number=n, extraction_method=method, status=status, error=None)


def prepared(records: list[ChunkRecord], pages: list[Any] | None = None) -> PreparedDocument:
    d = doc()
    pages = pages or [page(n, ExtractionMethod.TEXT) for n in range(1, 10)]
    return PreparedDocument(
        entry=d, pages=cast(list[ProcessedPage], pages), result=result(d, records)
    )


# --- round trip: rows -> ChunkMetadata ------------------------------------------------


def test_rows_round_trip_to_identical_metadata_and_extras() -> None:
    d = doc(version="v1", effective_from=date(2026, 1, 1))
    records = sample(d).chunks + [
        ChunkRecord(
            metadata=chunk(d, 4, "Proof of address").metadata.model_copy(
                update={
                    "extraction_method": ExtractionMethod.OCR,
                    "text_reliability": TextReliability.OCR_UNCERTAIN,
                    "ocr_confidence": 0.61,
                    "page_status": PageStatus.OCR_LOW_CONFIDENCE,
                    "region": "left",
                    "printed_page": 4,
                    "content_kind": ContentKind.FORM_FIELDS,
                    "paragraph": "S)",
                    "footnotes": [FootnoteRef(marker="1", text="Note.", page_number=5)],
                }
            ),
            extras={
                "raw_text": "Proof of  address",
                "excluded_lines": [
                    {"text": "~=", "page_number": 5, "region": "left", "confidence": 12.5}
                ],
            },
        )
    ]
    drow = document_row(d)
    for rec in records:
        crow = chunk_row(rec.metadata, rec.extras)
        assert chunk_metadata_from_rows(crow, drow) == rec.metadata
        back = chunk_record_from_rows(crow, drow)
        assert back.metadata.to_qdrant_payload() == rec.metadata.to_qdrant_payload()
        assert back.extras == rec.extras


def test_rows_from_another_document_are_refused() -> None:
    a, b = doc("doc_a"), doc("doc_b")
    with pytest.raises(ValueError, match="does not belong"):
        chunk_metadata_from_rows(chunk_row(chunk(a, 0, "x").metadata), document_row(b))


# --- derived document fields ------------------------------------------------------------


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        ([ExtractionMethod.TEXT, ExtractionMethod.TEXT], DocumentQuality.TEXT),
        ([ExtractionMethod.OCR, ExtractionMethod.OCR], DocumentQuality.SCANNED),
        ([ExtractionMethod.TEXT, ExtractionMethod.OCR], DocumentQuality.MIXED),
        ([ExtractionMethod.OCR, None], DocumentQuality.SCANNED),  # failed page: no method
        ([None, None], None),
    ],
)
def test_derive_quality(methods: list[Any], expected: DocumentQuality | None) -> None:
    pages = [page(i + 1, m) for i, m in enumerate(methods)]
    assert derive_quality(cast(list[ProcessedPage], pages)) is expected


def test_failed_pages_make_the_document_partial_not_completed() -> None:
    ok = prepared(sample(doc()).chunks)
    assert completion_status(ok) is IngestionStatus.COMPLETED
    pages = [page(n, ExtractionMethod.TEXT) for n in range(1, 10)]
    pages[2] = page(3, None, PageStatus.FAILED)
    assert completion_status(prepared(sample(doc()).chunks, pages)) is IngestionStatus.PARTIAL


def test_ingestion_summary_counts_roles_and_exclusions() -> None:
    p = prepared(sample(doc()).chunks)
    p.result.exclusions = [Exclusion(1, None, "noise", "~"), Exclusion(2, None, "noise", "=")]
    s = ingestion_summary(p)
    assert s["chunks"] == 4 and s["embedding_eligible"] == 3
    assert s["chunk_roles"] == {"child": 2, "parent": 1, "single": 1}
    assert s["exclusions_by_reason"] == {"noise": 2}


# --- validation before any write ---------------------------------------------------------


def test_valid_family_passes() -> None:
    validate_prepared(prepared(sample(doc()).chunks))


def test_child_without_its_parent_is_refused() -> None:
    parent, c1, c2, single = sample(doc()).chunks
    with pytest.raises(PersistError, match="has no parent chunk"):
        validate_prepared(prepared([c1, c2, single]))


def test_parent_without_children_is_refused() -> None:
    parent, _, _, single = sample(doc()).chunks
    with pytest.raises(PersistError, match="has no children"):
        validate_prepared(prepared([parent, single]))


def test_embeddable_parent_is_refused() -> None:
    parent, c1, c2, _ = sample(doc()).chunks
    bad = ChunkRecord(parent.metadata.model_copy(update={"embedding_eligible": True}), {})
    with pytest.raises(PersistError, match="embedding-eligible"):
        validate_prepared(prepared([bad, c1, c2]))


def test_chunk_on_unprocessed_page_is_refused() -> None:
    records = sample(doc()).chunks  # pages 1-4
    with pytest.raises(PersistError, match="was not processed"):
        validate_prepared(prepared(records, [page(1, ExtractionMethod.TEXT)]))


def test_chunk_of_another_document_is_refused() -> None:
    other = chunk(doc("other_doc"), 3, "Foreign chunk.")
    parent, c1, c2, _ = sample(doc()).chunks
    with pytest.raises(PersistError, match="another document"):
        validate_prepared(prepared([parent, c1, c2, other]))


def test_single_with_parent_reference_is_refused() -> None:
    parent, c1, c2, single = sample(doc()).chunks
    bad = ChunkRecord(
        single.metadata.model_copy(update={"parent_chunk_id": parent.metadata.chunk_id}), {}
    )
    with pytest.raises(PersistError, match="single chunk 3 has a parent"):
        validate_prepared(prepared([parent, c1, c2, bad]))
