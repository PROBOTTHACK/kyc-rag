"""Stage E: write one prepared document (Stages A-C output) to Postgres, idempotently.

`persist_document` brings the database in line with a `PreparedDocument` inside the
caller's transaction:

- `documents`: inserted, or updated in place when a stored value differs (identity is
  the content-derived id; a different file under an existing `document_key` is refused
  with `PersistError`, matching the manifest's one-version-per-key rule);
- `document_pages`: every physical page, failed ones included;
- `chunks`: every chunk (single, parent, child) by its deterministic id; rows whose
  values differ are updated, identical rows are left untouched, and rows that are no
  longer produced are deleted (parents and children together; the deferred parent FK
  rejects any delete that would orphan a child);
- `document_exclusions`: replaced only when the list changed.

The document is left `processing`; the pipeline sets the final status once indexing has
also succeeded (`set_document_status`). Nothing here touches Qdrant.
"""

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.corpus import chunk_row, document_row, exclusion_rows, page_row
from app.db.models import Base, Chunk, Document, DocumentExclusion, DocumentPage
from app.ingestion.pages import ProcessedPage
from app.ingestion.prepare import PreparedDocument
from app.schemas.taxonomy import (
    ChunkRole,
    DocumentQuality,
    ExtractionMethod,
    IngestionStatus,
    PageStatus,
)

# Columns that record when/how a row was written, not what it says.
_BOOKKEEPING = frozenset({"created_at", "updated_at", "ingestion_status"})


class PersistError(RuntimeError):
    """The prepared document cannot be stored consistently (nothing is written)."""


@dataclass
class PersistReport:
    document_key: str
    document_id: uuid.UUID
    document: str = "unchanged"  # inserted | updated | unchanged
    pages: int = 0
    pages_written: int = 0
    chunks: int = 0
    parents: int = 0
    children: int = 0
    eligible: int = 0
    chunks_inserted: int = 0
    chunks_updated: int = 0
    chunks_unchanged: int = 0
    chunks_deleted: int = 0
    exclusions: int = 0
    exclusions_replaced: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.document != "unchanged"
            or self.pages_written
            or self.chunks_inserted
            or self.chunks_updated
            or self.chunks_deleted
            or self.exclusions_replaced
        )


# --- derived document fields ---------------------------------------------------------


def derive_quality(pages: Sequence[ProcessedPage]) -> DocumentQuality | None:
    """`text` if every page was read natively, `scanned` if every read page needed OCR,
    `mixed` otherwise; None when no page could be read at all."""
    methods = {p.extraction_method for p in pages if p.extraction_method is not None}
    if not methods:
        return None
    if methods == {ExtractionMethod.TEXT}:
        return DocumentQuality.TEXT
    if methods == {ExtractionMethod.OCR}:
        return DocumentQuality.SCANNED
    return DocumentQuality.MIXED


def completion_status(prepared: PreparedDocument) -> IngestionStatus:
    """Status once every stage succeeded: `partial` when some pages could not be read
    (recorded with their error, never dropped), else `completed`."""
    return IngestionStatus.PARTIAL if prepared.failed_pages else IngestionStatus.COMPLETED


def ingestion_summary(prepared: PreparedDocument) -> dict[str, Any]:
    chunks = prepared.result.chunks
    roles = Counter(c.metadata.chunk_role.value for c in chunks)
    return {
        "chunks": len(chunks),
        "chunk_roles": dict(sorted(roles.items())),
        "embedding_eligible": sum(c.embeddable for c in chunks),
        "exclusions_by_reason": dict(
            sorted(Counter(e.reason for e in prepared.result.exclusions).items())
        ),
        "page_statuses": dict(sorted(Counter(p.status.value for p in prepared.pages).items())),
        "failed_pages": [
            {"page_number": p.page_number, "error": p.error} for p in prepared.failed_pages
        ],
        "ocr_policy": {
            "reliable_min_confidence": prepared.result.policy.reliable_min_confidence,
            "embed_uncertain": prepared.result.policy.embed_uncertain,
        },
    }


# --- validation --------------------------------------------------------------------


def validate_prepared(prepared: PreparedDocument) -> None:
    """Refuse a chunk set that would violate the parent/child and page invariants."""
    entry, chunks = prepared.entry, prepared.result.chunks
    doc_id = str(entry.row_id)
    pages = {p.page_number for p in prepared.pages}
    by_id = {c.metadata.chunk_id: c for c in chunks}
    problems: list[str] = []
    if len(by_id) != len(chunks):
        problems.append("duplicate chunk ids")
    if len({c.metadata.chunk_index for c in chunks}) != len(chunks):
        problems.append("duplicate chunk indexes")
    children: Counter[str] = Counter()
    for c in chunks:
        m = c.metadata
        if m.document_id != doc_id or m.document_key != entry.document_key:
            problems.append(f"chunk {m.chunk_index} belongs to another document")
        if m.page_number not in pages or (m.page_end is not None and m.page_end not in pages):
            problems.append(f"chunk {m.chunk_index} cites a page that was not processed")
        if m.chunk_role is ChunkRole.CHILD:
            parent = by_id.get(m.parent_chunk_id or "")
            if parent is None or parent.metadata.chunk_role is not ChunkRole.PARENT:
                problems.append(f"child {m.chunk_index} has no parent chunk in this document")
            else:
                children[parent.metadata.chunk_id] += 1
        elif m.parent_chunk_id is not None:
            problems.append(f"{m.chunk_role.value} chunk {m.chunk_index} has a parent")
        if m.chunk_role is ChunkRole.PARENT and m.embedding_eligible:
            problems.append(f"parent {m.chunk_index} is marked embedding-eligible")
    for c in chunks:
        if c.metadata.chunk_role is ChunkRole.PARENT and not children[c.metadata.chunk_id]:
            problems.append(f"parent {c.metadata.chunk_index} has no children")
    if problems:
        raise PersistError(f"{entry.document_key}: " + "; ".join(problems[:10]))


# --- row comparison ------------------------------------------------------------------


def _values(row: Base) -> dict[str, Any]:
    return {
        attr.key: getattr(row, attr.key)
        for attr in row.__mapper__.column_attrs
        if attr.key not in _BOOKKEEPING
    }


def _apply(target: Base, source: Base) -> bool:
    """Copy differing values from `source` onto the loaded `target`; True if any."""
    changed = False
    for key, value in _values(source).items():
        if getattr(target, key) != value:
            setattr(target, key, value)
            changed = True
    return changed


# --- persistence ----------------------------------------------------------------------


def persist_document(session: Session, prepared: PreparedDocument) -> PersistReport:
    """Write `prepared` in the caller's transaction (the caller commits or rolls back)."""
    validate_prepared(prepared)
    entry = prepared.entry
    doc_id = entry.row_id
    chunks = prepared.result.chunks
    report = PersistReport(
        document_key=entry.document_key,
        document_id=doc_id,
        pages=len(prepared.pages),
        chunks=len(chunks),
        parents=sum(c.metadata.chunk_role is ChunkRole.PARENT for c in chunks),
        children=sum(c.metadata.chunk_role is ChunkRole.CHILD for c in chunks),
        eligible=sum(c.embeddable for c in chunks),
        exclusions=len(prepared.result.exclusions),
    )

    # 1. documents --------------------------------------------------------------------
    other = session.scalar(
        select(Document).where(Document.document_key == entry.document_key, Document.id != doc_id)
    )
    if other is not None:
        raise PersistError(
            f"{entry.document_key}: Postgres already holds another version of this document "
            f"(sha256 {other.sha256[:12]}..., manifest {entry.sha256[:12]}...). M1 keeps one "
            "version per document_key; record the version relationship before re-ingesting."
        )
    quality = derive_quality(prepared.pages)
    new_doc = document_row(
        entry,
        ingestion_status=IngestionStatus.PROCESSING.value,
        quality=quality.value if quality else None,
        page_count=len(prepared.pages),
    )
    new_doc.metadata_ = {**new_doc.metadata_, "ingestion": ingestion_summary(prepared)}
    doc = session.get(Document, doc_id, with_for_update=True)
    if doc is None:
        session.add(new_doc)
        report.document = "inserted"
    else:
        if _apply(doc, new_doc):
            doc.updated_at = func.now()
            report.document = "updated"
        doc.ingestion_status = IngestionStatus.PROCESSING.value
    session.flush()

    # 2. pages ---------------------------------------------------------------------------
    stored_pages = {
        p.page_number: p
        for p in session.scalars(select(DocumentPage).where(DocumentPage.document_id == doc_id))
    }
    for page in prepared.pages:
        new_page = page_row(doc_id, page)
        have = stored_pages.pop(page.page_number, None)
        if have is None:
            session.add(new_page)
            report.pages_written += 1
        elif _apply(have, new_page):
            report.pages_written += 1
    session.flush()

    # 3. chunks (parents and children; the parent FK is checked at commit) ---------------
    stored = {c.id: c for c in session.scalars(select(Chunk).where(Chunk.document_id == doc_id))}
    current: set[uuid.UUID] = set()
    for rec in chunks:
        new_chunk = chunk_row(rec.metadata, rec.extras)
        current.add(new_chunk.id)
        have_chunk = stored.get(new_chunk.id)
        if have_chunk is None:
            session.add(new_chunk)
            report.chunks_inserted += 1
        elif _apply(have_chunk, new_chunk):
            have_chunk.updated_at = func.now()
            report.chunks_updated += 1
        else:
            report.chunks_unchanged += 1
    session.flush()
    for stale_id in sorted(set(stored) - current):
        session.delete(stored[stale_id])
        report.chunks_deleted += 1
    session.flush()

    # Pages no longer present (cannot happen for identical bytes; kept for safety).
    for leftover in stored_pages.values():
        session.delete(leftover)
        report.pages_written += 1

    # 4. exclusions -----------------------------------------------------------------------
    new_excl = exclusion_rows(doc_id, prepared.result.exclusions)
    stored_excl = list(
        session.scalars(
            select(DocumentExclusion)
            .where(DocumentExclusion.document_id == doc_id)
            .order_by(DocumentExclusion.seq)
        )
    )
    if [_values(e) for e in stored_excl] != [_values(e) for e in new_excl]:
        for e in stored_excl:
            session.delete(e)
        session.flush()
        session.add_all(new_excl)
        report.exclusions_replaced = True
    session.flush()
    return report


def set_document_status(
    session: Session, document_ids: Iterable[uuid.UUID], status: IngestionStatus
) -> int:
    """Set `ingestion_status` (and `updated_at`) on existing rows; returns rows changed."""
    ids = list(document_ids)
    if not ids:
        return 0
    result = session.execute(
        update(Document)
        .where(Document.id.in_(ids), Document.ingestion_status != status.value)
        .values(ingestion_status=status.value, updated_at=func.now())
    )
    return int(getattr(result, "rowcount", 0) or 0)


def page_failures(prepared: PreparedDocument) -> list[dict[str, Any]]:
    return [
        {"page_number": p.page_number, "status": p.status.value, "error": p.error}
        for p in prepared.pages
        if p.status is PageStatus.FAILED
    ]
