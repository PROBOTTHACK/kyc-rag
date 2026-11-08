"""Per-document ingestion: Stages A-C -> Postgres (Stage E) -> Qdrant (Stage D).

Order and failure handling (Postgres and Qdrant never share a transaction):

1. An `ingestion_runs` row is written first, in its own transaction.
2. Extraction, OCR and chunking run in memory. A raising stage stops the document;
   pages that fail OCR do not raise and are recorded (the document ends `partial`).
3. One Postgres transaction writes the document, pages, chunks and exclusions and
   leaves the document `processing`. On failure it rolls back; an existing document is
   then marked `failed`, and a first ingestion (no row survives) is still recorded in
   `ingestion_runs`.
4. Qdrant is indexed from the committed Postgres rows, so every point references a
   stored chunk. Stage D's ordering holds: upserts first, stale deletes after, so a
   failed embedding or upsert never removes existing vectors. A failure marks the
   document `failed` (its Postgres rows stay valid). Re-running, or
   `reconcile --repair`, recovers, because indexing is idempotent.
5. The document's indexed point ids are checked against its eligible Postgres chunks.
6. Only then is the document `completed` (or `partial` when pages failed).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Document
from app.db.persistence import (
    PersistReport,
    completion_status,
    page_failures,
    persist_document,
    set_document_status,
)
from app.db.repository import load_chunking_result
from app.db.runs import RunLog
from app.ingestion.ocr import TesseractSetup
from app.ingestion.pages import OcrCache
from app.ingestion.prepare import PreparedDocument, StageFailure, prepare
from app.retrieval.embeddings import Embedder
from app.retrieval.indexing import IndexReport, index_document
from app.retrieval.qdrant_store import ChunkStore
from app.schemas.corpus import DocumentMetadata
from app.schemas.taxonomy import IngestionStage, IngestionStatus, StageStatus

log = logging.getLogger(__name__)


class IndexCheckError(RuntimeError):
    """After indexing, Qdrant does not hold exactly the document's eligible chunks."""


class _TrackingEmbedder:
    """Delegates to an `Embedder`; remembers whether embedding (vs. Qdrant) raised."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.model_name = inner.model_name
        self.revision = inner.revision
        self.failed = False

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            return self.inner.embed_passages(texts)
        except Exception:
            self.failed = True
            raise

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(text)


@dataclass
class IngestOutcome:
    document_key: str
    status: IngestionStatus
    run_id: str | None = None
    failed_stage: IngestionStage | None = None
    error: str | None = None
    persist: PersistReport | None = None
    index: IndexReport | None = None
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)  # e.g. run log not written

    @property
    def failed(self) -> bool:
        return self.status is IngestionStatus.FAILED


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class DocumentIngestion:
    """One document through every stage, recording each outcome."""

    def __init__(
        self,
        entry: DocumentMetadata,
        settings: Settings,
        *,
        sessions: sessionmaker[Session],
        embedder: Embedder,
        store: ChunkStore,
        setup: TesseractSetup | None,
        setup_error: str | None = None,
        cache: OcrCache | None = None,
        force: bool = False,
        prepared: PreparedDocument | None = None,
    ) -> None:
        self.entry = entry
        self.settings = settings
        self.sessions = sessions
        self.embedder = embedder
        self.store = store
        self.setup = setup
        self.setup_error = setup_error
        self.cache = cache
        self.force = force
        self.prepared = prepared  # Stages A-C already run by the caller
        self.run = RunLog(sessions, entry, command="ingest")
        self.outcome = IngestOutcome(entry.document_key, IngestionStatus.PROCESSING)

    # --- bookkeeping that must not mask the real outcome ------------------------------

    def _safely(self, what: str, fn: Any, *args: Any, **kw: Any) -> None:
        try:
            fn(*args, **kw)
        except Exception as exc:
            msg = f"could not {what}: {_describe(exc)}"
            log.error("%s: %s", self.entry.document_key, msg)
            self.outcome.warnings.append(msg)

    def _mark_document_failed(self) -> None:
        """Mark an existing document `failed` (no-op if its row does not exist)."""

        def mark() -> None:
            with self.sessions() as s, s.begin():
                set_document_status(s, [self.entry.row_id], IngestionStatus.FAILED)

        self._safely("mark the document failed", mark)

    def _fail(self, stage: IngestionStage, exc: BaseException) -> IngestOutcome:
        error = _describe(exc)
        self.run.stage(stage, StageStatus.FAILED, error=error)
        self.run.skip_remaining(stage)
        self._mark_document_failed()
        self._safely(
            "record the failed run",
            self.run.finish,
            IngestionStatus.FAILED,
            failed_stage=stage,
            error=error,
        )
        o = self.outcome
        o.status, o.failed_stage, o.error, o.stages = (
            IngestionStatus.FAILED,
            stage,
            error,
            dict(self.run.stages),
        )
        return o

    # --- stages -------------------------------------------------------------------------

    def _prepare(self) -> PreparedDocument:
        prepared = self.prepared or prepare(
            self.entry,
            self.settings,
            setup=self.setup,
            setup_error=self.setup_error,
            cache=self.cache,
        )
        ocr_pages = [p for p in prepared.pages if p.ocr_triggered]
        failures = page_failures(prepared)
        self.run.stage(IngestionStage.EXTRACTION, StageStatus.OK, pages=len(prepared.pages))
        self.run.stage(
            IngestionStage.OCR,
            StageStatus.FAILED if failures else StageStatus.OK,
            ocr_pages=len(ocr_pages),
            failed_pages=failures,
            **({"error": f"{len(failures)} page(s) failed"} if failures else {}),
        )
        self.run.stage(
            IngestionStage.CHUNKING,
            StageStatus.OK,
            chunks=len(prepared.result.chunks),
            eligible=sum(c.embeddable for c in prepared.result.chunks),
            exclusions=len(prepared.result.exclusions),
        )
        return prepared

    def _persist(self, prepared: PreparedDocument) -> PersistReport:
        with self.sessions() as s, s.begin():
            report = persist_document(s, prepared)
        self.run.stage(
            IngestionStage.PERSISTENCE,
            StageStatus.OK,
            document=report.document,
            pages=report.pages,
            chunks=report.chunks,
            parents=report.parents,
            children=report.children,
            inserted=report.chunks_inserted,
            updated=report.chunks_updated,
            unchanged=report.chunks_unchanged,
            deleted=report.chunks_deleted,
            exclusions=report.exclusions,
        )
        return report

    def _index(self) -> IndexReport:
        with self.sessions() as s:
            doc = s.get(Document, self.entry.row_id)
            if doc is None:  # unreachable: persisted just before
                raise IndexCheckError("document row disappeared before indexing")
            result = load_chunking_result(s, doc)
        tracking = _TrackingEmbedder(self.embedder)
        try:
            report = index_document(result, tracking, self.store, force=self.force)
        except Exception as exc:
            stage = IngestionStage.EMBEDDING if tracking.failed else IngestionStage.INDEXING
            raise StageFailure(stage, exc) from exc
        self.run.stage(IngestionStage.EMBEDDING, StageStatus.OK, embedded=report.embedded)

        expected = {c.metadata.chunk_id for c in result.chunks if c.embeddable}
        indexed = set(self.store.point_states(self.entry.document_key))
        if indexed != expected:
            raise StageFailure(
                IngestionStage.INDEXING,
                IndexCheckError(
                    f"after indexing: {len(expected - indexed)} eligible chunk(s) missing, "
                    f"{len(indexed - expected)} unexpected point(s)"
                ),
            )
        self.run.stage(
            IngestionStage.INDEXING,
            StageStatus.OK,
            points=len(indexed),
            payload_updated=report.payload_updated,
            unchanged=report.unchanged,
            stale_deleted=report.stale_deleted,
        )
        return report

    # --- driver ---------------------------------------------------------------------------

    def run_all(self) -> IngestOutcome:
        try:
            self.run.start()
            self.outcome.run_id = str(self.run.id)
        except Exception as exc:  # e.g. Postgres unreachable: reported, not hidden
            self.outcome.warnings.append(f"could not record the run: {_describe(exc)}")

        try:
            prepared = self._prepare()
        except StageFailure as exc:
            return self._fail(exc.stage, exc.error)
        self._safely("save stage progress", self.run.save)

        try:
            self.outcome.persist = self._persist(prepared)
        except Exception as exc:
            return self._fail(IngestionStage.PERSISTENCE, exc)
        self._safely("save stage progress", self.run.save)

        try:
            self.outcome.index = self._index()
        except StageFailure as exc:
            return self._fail(exc.stage, exc.error)
        except Exception as exc:
            return self._fail(IngestionStage.INDEXING, exc)

        status = completion_status(prepared)
        try:
            with self.sessions() as s, s.begin():
                set_document_status(s, [self.entry.row_id], status)
        except Exception as exc:
            return self._fail(IngestionStage.PERSISTENCE, exc)
        self._safely("record the finished run", self.run.finish, status)
        self.outcome.status = status
        self.outcome.stages = dict(self.run.stages)
        return self.outcome


def ingest_document(
    entry: DocumentMetadata,
    settings: Settings,
    *,
    sessions: sessionmaker[Session],
    embedder: Embedder,
    store: ChunkStore,
    setup: TesseractSetup | None,
    setup_error: str | None = None,
    cache: OcrCache | None = None,
    force: bool = False,
    prepared: PreparedDocument | None = None,
) -> IngestOutcome:
    """Ingest one manifest entry end to end; never raises for a stage failure.

    `prepared` skips Stages A-C (e.g. an upload already extracted by the caller).
    """
    return DocumentIngestion(
        entry,
        settings,
        sessions=sessions,
        embedder=embedder,
        store=store,
        setup=setup,
        setup_error=setup_error,
        cache=cache,
        force=force,
        prepared=prepared,
    ).run_all()
