"""Postgres <-> Qdrant reconciliation (M1 Stage E).

Postgres is the source of truth for chunks; Qdrant is a derived index. The two cannot
share a transaction, so the pipeline writes Postgres first, then indexes from the
committed rows, and this module checks the result in both directions:

- eligible Postgres chunks with no Qdrant point (`missing`);
- Qdrant points with no Postgres chunk (`no_postgres_chunk`);
- Qdrant points whose chunk is no longer embedding-eligible (`not_eligible`);
- points whose stored payload, embedding input, model or revision differs from what
  the Postgres row would produce now (`outdated`).

`reconcile` is diagnostic and never writes. `repair` re-indexes the affected documents
from Postgres (which, as in Stage D, deletes that document's stale points only after
its upserts succeed). Points whose document is not in Postgres at all are deleted only
with `delete_unknown=True`.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.corpus import chunk_record_from_rows
from app.db.models import Chunk, Document
from app.db.repository import load_chunking_result
from app.retrieval.embeddings import Embedder
from app.retrieval.indexing import IndexReport, index_document, point_payload
from app.retrieval.qdrant_store import INDEX_KEY, ChunkStore


@dataclass(frozen=True)
class PointIssue:
    point_id: str
    document_key: str | None
    reason: str = ""


@dataclass
class ReconcileReport:
    postgres_documents: int = 0
    postgres_chunks: int = 0
    postgres_eligible: int = 0
    qdrant_points: int = 0
    missing: list[PointIssue] = field(default_factory=list)
    no_postgres_chunk: list[PointIssue] = field(default_factory=list)
    not_eligible: list[PointIssue] = field(default_factory=list)
    outdated: list[PointIssue] = field(default_factory=list)
    unfinished_documents: dict[str, str] = field(default_factory=dict)  # key -> status

    @property
    def consistent(self) -> bool:
        """Qdrant holds exactly the eligible Postgres chunks, all current."""
        return not (self.missing or self.no_postgres_chunk or self.not_eligible or self.outdated)

    @property
    def ok(self) -> bool:
        return self.consistent and not self.unfinished_documents

    def documents_to_reindex(self, known: set[str]) -> set[str]:
        issues = self.missing + self.no_postgres_chunk + self.not_eligible + self.outdated
        return {i.document_key for i in issues if i.document_key in known}

    def unknown_documents(self, known: set[str]) -> dict[str | None, list[str]]:
        out: dict[str | None, list[str]] = defaultdict(list)
        for i in self.no_postgres_chunk:
            if i.document_key not in known:
                out[i.document_key].append(i.point_id)
        return dict(out)


def reconcile(session: Session, store: ChunkStore, embedder: Embedder) -> ReconcileReport:
    """Compare every Postgres chunk with every Qdrant point. Read-only.

    `embedder` supplies only the expected model name and revision; no model is loaded.
    """
    report = ReconcileReport()
    documents = {d.id: d for d in session.scalars(select(Document))}
    report.postgres_documents = len(documents)
    report.unfinished_documents = {
        d.document_key: d.ingestion_status
        for d in documents.values()
        if d.ingestion_status not in ("completed", "partial")
    }
    chunks = {c.id: c for c in session.scalars(select(Chunk))}
    report.postgres_chunks = len(chunks)
    eligible = {cid for cid, c in chunks.items() if c.embedding_eligible}
    report.postgres_eligible = len(eligible)

    points = store.all_point_states()
    report.qdrant_points = len(points)
    point_ids = {uuid.UUID(pid): pid for pid in points}

    for cid in sorted(eligible - point_ids.keys()):
        report.missing.append(PointIssue(str(cid), chunks[cid].document_key))

    for uid, pid in sorted(point_ids.items()):
        state = points[pid]
        row = chunks.get(uid)
        if row is None:
            report.no_postgres_chunk.append(PointIssue(pid, state.document_key))
            continue
        if not row.embedding_eligible:
            report.not_eligible.append(PointIssue(pid, row.document_key, row.chunk_role))
            continue
        expected = point_payload(chunk_record_from_rows(row, documents[row.document_id]), embedder)[
            INDEX_KEY
        ]
        reasons = []
        if state.document_key != row.document_key:
            reasons.append("document_key")
        if state.input_sha256 != expected["input_sha256"]:
            reasons.append("embedding_input")
        if state.model != embedder.model_name or state.revision != embedder.revision:
            reasons.append("model")
        if state.payload_sha256 != expected["payload_sha256"]:
            reasons.append("payload")
        if reasons:
            report.outdated.append(PointIssue(pid, row.document_key, ",".join(reasons)))
    return report


@dataclass
class RepairReport:
    reindexed: dict[str, IndexReport] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    unknown_deleted: int = 0
    unknown_kept: int = 0


def repair(
    sessions: sessionmaker[Session],
    store: ChunkStore,
    embedder: Embedder,
    report: ReconcileReport,
    *,
    delete_unknown: bool = False,
) -> RepairReport:
    """Re-index affected documents from Postgres; delete unknown points only on request."""
    out = RepairReport()
    with sessions() as s:
        known = {d.document_key: d for d in s.scalars(select(Document))}
        for key in sorted(report.documents_to_reindex(set(known))):
            try:
                result = load_chunking_result(s, known[key])
                out.reindexed[key] = index_document(result, embedder, store)
            except Exception as exc:  # keep going: one document's failure is reported
                out.failed[key] = f"{type(exc).__name__}: {exc}"
    unknown = report.unknown_documents(set(known))
    ids = [pid for pids in unknown.values() for pid in pids]
    if delete_unknown:
        out.unknown_deleted = store.delete(ids)
    else:
        out.unknown_kept = len(ids)
    return out
