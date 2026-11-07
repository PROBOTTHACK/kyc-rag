"""Build ORM rows from the shared metadata schemas, so Postgres mirrors manifest and Qdrant."""

import hashlib
import uuid
from typing import Any

from app.db.models import Chunk, Document
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
        },
        **ingestion,
    )


def chunk_row(meta: ChunkMetadata) -> Chunk:
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
        parent_chunk_id=uuid.UUID(meta.parent_chunk_id) if meta.parent_chunk_id else None,
    )
