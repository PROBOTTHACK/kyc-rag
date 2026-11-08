"""Build ORM rows from the shared metadata schemas, so Postgres mirrors manifest and Qdrant.

The mapping is reversible: `chunk_metadata_from_rows` rebuilds the exact `ChunkMetadata`
(= Qdrant payload) from a `chunks` row and its `documents` row, so indexing and
reconciliation can work from Postgres, the source of truth, without re-running
extraction or OCR.
"""

import hashlib
import uuid
from typing import Any

from app.db.models import Chunk, Document, DocumentExclusion, DocumentPage
from app.ingestion.chunking import ChunkRecord, Exclusion
from app.ingestion.pages import ProcessedPage
from app.schemas.corpus import ChunkMetadata, DocumentMetadata

# Payload fields stored on `documents` rather than duplicated on every `chunks` row.
# Payload key -> documents column. Everything else in the payload is a `chunks` column.
DOCUMENT_LEVEL_PAYLOAD_FIELDS: dict[str, str] = {
    "document_name": "title",
    "file": "file",
    "source_authority": "source_authority",
    "version": "version",
    "effective_from": "effective_from",
    "effective_until": "effective_until",
}
# Payload key -> chunks column, where the names differ.
CHUNK_PAYLOAD_RENAMES: dict[str, str] = {"chunk_id": "id"}
# Payload fields stored as keys of chunks.metadata (JSONB) rather than as columns.
CHUNK_JSON_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "paragraph",
        "region",
        "printed_page",
        "content_kind",
        "page_status",
        "ocr_confidence",
        "footnotes",
        "context",
    }
)
# Postgres-only chunk data (`ChunkRecord.extras`) kept as its own column.
RAW_TEXT_EXTRA = "raw_text"
# Payload fields that are computed by ChunkMetadata, so never passed back into it.
COMPUTED_PAYLOAD_FIELDS: frozenset[str] = frozenset(ChunkMetadata.model_computed_fields)
# Page-record keys that describe this run, not the page (they would make re-ingestion
# look like a change).
_VOLATILE_PAGE_KEYS = frozenset({"from_cache"})


def document_row(doc: DocumentMetadata, **ingestion: Any) -> Document:
    """`ingestion` carries pipeline-owned columns (ingestion_status, quality, page_count)."""
    return Document(
        id=doc.row_id,
        document_key=doc.document_key,
        file=doc.file,
        sha256=doc.sha256,
        bank=doc.bank.value,
        document_type=doc.document_type.value,
        source_authority=doc.source_authority.value if doc.source_authority else None,
        attribution_basis=doc.attribution_basis.value,
        title=doc.title,
        version=doc.version,
        issue_date=doc.issue_date,
        effective_from=doc.effective_from,
        effective_until=doc.effective_until,
        source_status=doc.source_status.value,
        status_reason=doc.status_reason,
        evidence_eligibility=doc.evidence_eligibility.value,
        version_review_status=doc.version_review_status.value,
        entity_types=[t.value for t in doc.entity_types],
        residency_statuses=[r.value for r in doc.residency_statuses],
        metadata_={
            "supersedes": list(doc.supersedes),
            "coverage_notes": list(doc.coverage_notes),
            "notes": doc.notes,
            "chunking_profile": doc.effective_chunking_profile.value,
            "ocr_reliable_min_confidence": doc.ocr_reliable_min_confidence,
            "embed_uncertain_ocr": doc.embed_uncertain_ocr,
        },
        **ingestion,
    )


def chunk_row(meta: ChunkMetadata, extras: dict[str, Any] | None = None) -> Chunk:
    """`extras` = Postgres-only data: `raw_text` (column) and e.g. `excluded_lines` (JSON)."""
    payload = meta.to_qdrant_payload()
    json_fields = {k: payload[k] for k in sorted(CHUNK_JSON_PAYLOAD_FIELDS)}
    extra_json = {k: v for k, v in (extras or {}).items() if k != RAW_TEXT_EXTRA}
    clash = (json_fields.keys() | set(payload)) & extra_json.keys()
    if clash:
        raise ValueError(f"extras may not overwrite payload fields: {sorted(clash)}")
    return Chunk(
        id=uuid.UUID(meta.chunk_id),
        document_id=uuid.UUID(meta.document_id),
        chunk_index=meta.chunk_index,
        document_key=meta.document_key,
        bank=meta.bank.value,
        document_type=meta.document_type.value,
        source_status=meta.source_status.value,
        evidence_eligibility=meta.evidence_eligibility.value,
        page_number=meta.page_number,
        page_end=meta.page_end,
        section=meta.section,
        entity_types=[t.value for t in meta.entity_types],
        residency_statuses=[r.value for r in meta.residency_statuses],
        extraction_method=meta.extraction_method.value,
        content=meta.content,
        content_sha256=hashlib.sha256(meta.content.encode("utf-8")).hexdigest(),
        raw_text=(extras or {}).get(RAW_TEXT_EXTRA),
        parent_chunk_id=uuid.UUID(meta.parent_chunk_id) if meta.parent_chunk_id else None,
        chunk_role=meta.chunk_role.value,
        text_reliability=meta.text_reliability.value,
        embedding_eligible=meta.embedding_eligible,
        can_support_mandatory=meta.can_support_mandatory,
        metadata_={**json_fields, **extra_json},
    )


def chunk_metadata_from_rows(chunk: Chunk, document: Document) -> ChunkMetadata:
    """Inverse of `chunk_row` (+ the document-level payload fields from `document_row`)."""
    if chunk.document_id != document.id:
        raise ValueError(f"chunk {chunk.id} does not belong to document {document.id}")
    data: dict[str, Any] = {
        key: getattr(document, col) for key, col in DOCUMENT_LEVEL_PAYLOAD_FIELDS.items()
    }
    for key in ChunkMetadata.model_fields:
        if key in DOCUMENT_LEVEL_PAYLOAD_FIELDS:
            continue
        if key in CHUNK_JSON_PAYLOAD_FIELDS:
            if key in chunk.metadata_:
                data[key] = chunk.metadata_[key]
            continue
        value = getattr(chunk, CHUNK_PAYLOAD_RENAMES.get(key, key))
        data[key] = str(value) if isinstance(value, uuid.UUID) else value
    return ChunkMetadata.model_validate(data)


def chunk_record_from_rows(chunk: Chunk, document: Document) -> ChunkRecord:
    extras = {k: v for k, v in chunk.metadata_.items() if k not in CHUNK_JSON_PAYLOAD_FIELDS}
    if chunk.raw_text is not None:
        extras[RAW_TEXT_EXTRA] = chunk.raw_text
    return ChunkRecord(metadata=chunk_metadata_from_rows(chunk, document), extras=extras)


def page_metadata(page: ProcessedPage) -> dict[str, Any]:
    """`ProcessedPage.metadata()` without per-run flags (e.g. whether OCR hit the cache)."""
    meta = page.metadata()
    ocr = meta.get("ocr")
    if ocr:
        ocr["regions"] = [
            {k: v for k, v in region.items() if k not in _VOLATILE_PAGE_KEYS}
            for region in ocr["regions"]
        ]
    return meta


def page_row(document_id: uuid.UUID, page: ProcessedPage) -> DocumentPage:
    metrics = page.structured.content.metrics
    return DocumentPage(
        document_id=document_id,
        page_number=page.page_number,
        extraction_method=page.extraction_method.value if page.extraction_method else None,
        status=page.status.value,
        char_count=metrics.char_count,  # text layer; OCR word counts are in metadata
        alpha_ratio=metrics.alpha_ratio,
        image_coverage=metrics.image_coverage,
        ocr_confidence=page.ocr_confidence,
        error=page.error,
        metadata_=page_metadata(page),
    )


def exclusion_rows(document_id: uuid.UUID, exclusions: list[Exclusion]) -> list[DocumentExclusion]:
    return [
        DocumentExclusion(
            document_id=document_id,
            seq=i,
            page_number=e.page_number,
            region=e.region,
            reason=e.reason,
            text=e.text,
        )
        for i, e in enumerate(exclusions)
    ]
