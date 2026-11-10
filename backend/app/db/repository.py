"""Read access to persisted documents and chunks (Postgres = source of truth).

Reusable by the ingestion pipeline, reconciliation and, later, the API and the
LangGraph retrieval node (sync `Session`; async callers use `AsyncSession.run_sync`).

- `load_chunking_result`: rebuild a document's chunk set from Postgres, so Qdrant is
  indexed from committed rows (every point then references a stored chunk).
- `evidence_context`: expand retrieved chunk ids to the text synthesis should see: a
  child expands to its complete parent paragraph, anything else to itself.
- `family_integrity`: parent/child checks over stored rows.
- `eligible_chunk_payloads` / `chunk_corpus_fingerprint`: the keyword (BM25) index corpus
  and the fingerprint that tells when it must be rebuilt (M2 A1).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.corpus import DOCUMENT_LEVEL_PAYLOAD_FIELDS, chunk_record_from_rows
from app.db.models import Chunk, Document
from app.ingestion.chunking import ChunkingResult, ChunkRecord
from app.schemas.taxonomy import ChunkingProfile, ChunkRole


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


def get_document(session: Session, document_key: str) -> Document | None:
    return session.scalar(select(Document).where(Document.document_key == document_key))


def load_chunking_result(session: Session, document: Document) -> ChunkingResult:
    """All stored chunks of `document` in `chunk_index` order, as Stage C records."""
    rows = session.scalars(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.chunk_index)
    )
    return ChunkingResult(
        document_key=document.document_key,
        profile=ChunkingProfile(document.metadata_["chunking_profile"]),
        chunks=[chunk_record_from_rows(row, document) for row in rows],
    )


def get_chunks(session: Session, chunk_ids: Iterable[str | uuid.UUID]) -> dict[str, ChunkRecord]:
    ids = {_uuid(i) for i in chunk_ids}
    if not ids:
        return {}
    rows = session.execute(
        select(Chunk, Document)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(ids))
    ).all()
    return {str(c.id): chunk_record_from_rows(c, d) for c, d in rows}


@dataclass(frozen=True)
class EvidenceContext:
    """What synthesis and citation use for one retrieved chunk."""

    retrieved: ChunkRecord  # the chunk that matched (single or child)
    context: ChunkRecord  # its parent paragraph for a child, else the chunk itself

    @property
    def expanded(self) -> bool:
        return self.context is not self.retrieved


def evidence_context(
    session: Session, chunk_ids: Iterable[str | uuid.UUID]
) -> dict[str, EvidenceContext]:
    """Map each retrieved chunk id to its evidence context; unknown ids are omitted."""
    retrieved = get_chunks(session, chunk_ids)
    parent_ids = {
        r.metadata.parent_chunk_id for r in retrieved.values() if r.metadata.parent_chunk_id
    }
    parents = get_chunks(session, {p for p in parent_ids if p is not None})
    out: dict[str, EvidenceContext] = {}
    for cid, rec in retrieved.items():
        pid = rec.metadata.parent_chunk_id
        if pid is not None and pid not in parents:  # unreachable: enforced by the FK
            raise LookupError(f"parent {pid} of chunk {cid} is missing")
        out[cid] = EvidenceContext(retrieved=rec, context=parents[pid] if pid else rec)
    return out


def children_of(session: Session, parent_id: str | uuid.UUID) -> list[ChunkRecord]:
    pid = _uuid(parent_id)
    rows = session.execute(
        select(Chunk, Document)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.parent_chunk_id == pid)
        .order_by(Chunk.chunk_index)
    ).all()
    return [chunk_record_from_rows(c, d) for c, d in rows]


def eligible_chunk_payloads(session: Session) -> list[dict[str, Any]]:
    """Payloads (= Qdrant payload without `index`) of every embedding-eligible chunk.

    The same rows, and the same row -> payload mapping, as the Qdrant index: single and
    child chunks of every eligibility value; parents are never embedding-eligible.
    """
    rows = session.execute(
        select(Chunk, Document)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.embedding_eligible.is_(True))
        .order_by(Chunk.document_key, Chunk.chunk_index)
    ).all()
    return [chunk_record_from_rows(c, d).metadata.to_qdrant_payload() for c, d in rows]


# Document columns that reach a chunk payload (citations: title, file, version, ...).
_FINGERPRINT_DOCUMENT_COLUMNS = ("id", *sorted(set(DOCUMENT_LEVEL_PAYLOAD_FIELDS.values())))


def chunk_corpus_fingerprint(session: Session) -> str:
    """SHA-256 over every stored chunk row and the payload-relevant document columns.

    Deliberately over-inclusive, so no field that affects the keyword index can change
    unnoticed: indexed text (`content`, `metadata.context`), payloads and citations
    (pages, section, paragraph, region, document title / file / version, ...),
    filters (eligibility, bank, document, entity types, mandatory flag) and parent/child
    links. All chunks are covered, parents included. Only the row timestamps are left
    out. Over-inclusion merely costs an occasional, cheap rebuild.
    """
    return str(session.execute(_FINGERPRINT_SQL).scalar_one())


# The document column list is written out literally (no string interpolation in SQL); a
# unit test keeps it equal to `_FINGERPRINT_DOCUMENT_COLUMNS`.
_FINGERPRINT_SQL = text(
    "SELECT encode(sha256(convert_to(coalesce(string_agg(r.line, E'\\n' ORDER BY r.id), ''),"
    " 'UTF8')), 'hex') FROM ("
    "  SELECT c.id, ((to_jsonb(c) - 'created_at' - 'updated_at')::text || '|' ||"
    "   to_jsonb(ROW(d.id, d.effective_from, d.effective_until, d.file, d.source_authority,"
    "   d.title, d.version))::text) AS line"
    "  FROM chunks c JOIN documents d ON d.id = c.document_id"
    ") r"
)


@dataclass
class FamilyReport:
    parents: int = 0
    children: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _squash(text: str) -> str:
    return "".join(text.split())


def family_integrity(
    session: Session, document_ids: Iterable[uuid.UUID] | None = None
) -> FamilyReport:
    """Check every stored parent/child family.

    A family is intact when the parent exists in the same document with role `parent`,
    is not embedded, has at least two children, and its children reassemble it: their
    raw text joined by newlines equals the parent's raw text, and their normalised
    content equals the parent's content up to whitespace (the Stage C invariant).
    """
    stmt = select(Chunk).order_by(Chunk.document_id, Chunk.chunk_index)
    if document_ids is not None:
        stmt = stmt.where(Chunk.document_id.in_(list(document_ids)))
    rows = list(session.scalars(stmt))
    by_id = {r.id: r for r in rows}
    kids: dict[uuid.UUID, list[Chunk]] = defaultdict(list)
    report = FamilyReport()
    for r in rows:
        if r.chunk_role == ChunkRole.PARENT.value:
            report.parents += 1
        if r.parent_chunk_id is None:
            continue
        report.children += 1
        parent = by_id.get(r.parent_chunk_id)
        if parent is None:
            report.problems.append(f"orphan child {r.id}")
        elif parent.document_id != r.document_id:
            report.problems.append(f"child {r.id} points into another document")
        elif parent.chunk_role != ChunkRole.PARENT.value:
            report.problems.append(f"child {r.id} points at a {parent.chunk_role} chunk")
        else:
            kids[parent.id].append(r)
    for r in rows:
        if r.chunk_role != ChunkRole.PARENT.value:
            continue
        family = sorted(kids.get(r.id, []), key=lambda c: c.chunk_index)
        label = f"parent {r.document_key}#{r.chunk_index}"
        if r.embedding_eligible:
            report.problems.append(f"{label} is embedding-eligible")
        if len(family) < 2:
            report.problems.append(f"{label} has {len(family)} children")
            continue
        if _squash(r.content) != "".join(_squash(c.content) for c in family):
            report.problems.append(f"{label}: children do not reassemble its content")
        if r.raw_text != "\n".join(c.raw_text or "" for c in family):
            report.problems.append(f"{label}: children do not reassemble its raw text")
    return report
