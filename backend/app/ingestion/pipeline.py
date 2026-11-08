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
   document `failed` (its Postgres rows stay valid). Re-running, `retry`, or
   `reconcile --repair` recovers, because indexing is idempotent.
5. The document's indexed point ids are checked against its eligible Postgres chunks.
6. Only then is the document `completed` (or `partial` when pages failed).

Modes (M1 Stage F) reuse the same stage methods:
- `full`: steps 1-6 (`run` / `ingest`);
- `persist`: steps 1-3, then the document is `pending` (stored, not yet indexed);
- `index`: steps 1, 4-6 from rows already in Postgres (no PDF is read).
`dry_run_document` runs Stages A-C and reports what would change, writing nothing.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Document, DocumentPage
from app.db.persistence import (
    PersistReport,
    completion_status,
    page_failures,
    persist_document,
    set_document_status,
    validate_prepared,
)
from app.db.repository import load_chunking_result
from app.db.runs import RunLog, latest_run
from app.ingestion.ocr import TesseractSetup
from app.ingestion.pages import OcrCache
from app.ingestion.prepare import PreparedDocument, StageFailure, prepare
from app.retrieval.embeddings import Embedder
from app.retrieval.indexing import IndexPlan, IndexReport, index_document, plan_index
from app.retrieval.qdrant_store import ChunkStore
from app.schemas.corpus import DocumentMetadata
from app.schemas.taxonomy import IngestionStage, IngestionStatus, PageStatus, StageStatus

log = logging.getLogger(__name__)

PREPARE_STAGES = (IngestionStage.EXTRACTION, IngestionStage.OCR, IngestionStage.CHUNKING)
INDEX_STAGES = (IngestionStage.EMBEDDING, IngestionStage.INDEXING)


class Mode(StrEnum):
    FULL = "full"
    PERSIST = "persist"
    INDEX = "index"


class IndexCheckError(RuntimeError):
    """After indexing, Qdrant does not hold exactly the document's eligible chunks."""


class NotPersistedError(RuntimeError):
    """`index` mode needs the document's rows in Postgres first."""


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
    mode: Mode = Mode.FULL
    run_id: str | None = None
    failed_stage: IngestionStage | None = None
    error: str | None = None
    persist: PersistReport | None = None
    index: IndexReport | None = None
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)  # e.g. run log not written
    elapsed_s: float = 0.0

    @property
    def failed(self) -> bool:
        return self.status is IngestionStatus.FAILED


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class DocumentIngestion:
    """One document through the stages of `mode`, recording each outcome."""

    def __init__(
        self,
        entry: DocumentMetadata,
        settings: Settings,
        *,
        sessions: sessionmaker[Session],
        embedder: Embedder | None,
        store: ChunkStore | None,
        setup: TesseractSetup | None = None,
        setup_error: str | None = None,
        cache: OcrCache | None = None,
        force: bool = False,
        prepared: PreparedDocument | None = None,
        mode: Mode = Mode.FULL,
        command: str = "ingest",
    ) -> None:
        if mode is not Mode.PERSIST and (embedder is None or store is None):
            raise ValueError(f"mode {mode} needs an embedder and a store")
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
        self.mode = mode
        self.run = RunLog(sessions, entry, command=command)
        self.outcome = IngestOutcome(entry.document_key, IngestionStatus.PROCESSING, mode=mode)

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

    def _finish(self, status: IngestionStatus) -> IngestOutcome:
        try:
            with self.sessions() as s, s.begin():
                set_document_status(s, [self.entry.row_id], status)
        except Exception as exc:
            return self._fail(IngestionStage.PERSISTENCE, exc)
        self._safely("record the finished run", self.run.finish, status)
        self.outcome.status = status
        self.outcome.stages = dict(self.run.stages)
        return self.outcome

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
        if self.embedder is None or self.store is None:  # guarded in __init__
            raise ValueError("indexing needs an embedder and a store")
        with self.sessions() as s:
            doc = s.get(Document, self.entry.row_id)
            if doc is None:
                raise NotPersistedError(
                    f"{self.entry.document_key} has no Postgres rows; run `ingest` first"
                )
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

    def _stored_completion_status(self) -> IngestionStatus:
        """`partial` if any stored page of the document failed, else `completed`."""
        with self.sessions() as s:
            failed = s.scalar(
                select(
                    exists().where(
                        DocumentPage.document_id == self.entry.row_id,
                        DocumentPage.status == PageStatus.FAILED.value,
                    )
                )
            )
        return IngestionStatus.PARTIAL if failed else IngestionStatus.COMPLETED

    # --- driver ---------------------------------------------------------------------------

    def run_all(self) -> IngestOutcome:
        started = time.perf_counter()
        try:
            return self._run_stages()
        finally:
            self.outcome.elapsed_s = round(time.perf_counter() - started, 2)

    def _run_stages(self) -> IngestOutcome:
        try:
            self.run.start()
            self.outcome.run_id = str(self.run.id)
        except Exception as exc:  # e.g. Postgres unreachable: reported, not hidden
            self.outcome.warnings.append(f"could not record the run: {_describe(exc)}")

        if self.mode is Mode.INDEX:
            note = "not run: indexed from committed Postgres rows"
            for stage in (*PREPARE_STAGES, IngestionStage.PERSISTENCE):
                self.run.stages[stage.value] = {"status": StageStatus.SKIPPED.value, "note": note}
            return self._index_and_finish(self._stored_completion_status)

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

        if self.mode is Mode.PERSIST:
            for stage in INDEX_STAGES:
                self.run.stages[stage.value] = {
                    "status": StageStatus.SKIPPED.value,
                    "note": "persist only; run `index --from-db` or `retry` to index",
                }
            return self._finish(IngestionStatus.PENDING)  # stored, not fully ingested
        return self._index_and_finish(lambda: completion_status(prepared))

    def _index_and_finish(self, status: Any) -> IngestOutcome:
        try:
            self.outcome.index = self._index()
        except StageFailure as exc:
            return self._fail(exc.stage, exc.error)
        except Exception as exc:
            return self._fail(IngestionStage.INDEXING, exc)
        try:
            final = status()
        except Exception as exc:
            return self._fail(IngestionStage.PERSISTENCE, exc)
        return self._finish(final)


def ingest_document(
    entry: DocumentMetadata,
    settings: Settings,
    *,
    sessions: sessionmaker[Session],
    embedder: Embedder | None,
    store: ChunkStore | None,
    setup: TesseractSetup | None = None,
    setup_error: str | None = None,
    cache: OcrCache | None = None,
    force: bool = False,
    prepared: PreparedDocument | None = None,
    mode: Mode = Mode.FULL,
    command: str = "ingest",
) -> IngestOutcome:
    """Run one manifest entry through `mode`; never raises for a stage failure.

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
        mode=mode,
        command=command,
    ).run_all()


# --- dry run ----------------------------------------------------------------------------------


@dataclass
class DryRunReport:
    document_key: str
    pages: int = 0
    failed_pages: int = 0
    chunks: int = 0
    parents: int = 0
    eligible: int = 0
    exclusions: int = 0
    error: str | None = None  # a stage or validation failure
    persist: PersistReport | None = None  # what the Postgres write would do
    index: IndexPlan | None = None  # what indexing would do (from the same chunks)
    notes: list[str] = field(default_factory=list)


def dry_run_document(
    entry: DocumentMetadata,
    settings: Settings,
    *,
    sessions: sessionmaker[Session] | None,
    store: ChunkStore | None,
    embedder: Embedder | None,
    setup: TesseractSetup | None = None,
    setup_error: str | None = None,
) -> DryRunReport:
    """Stages A-C plus validation; then the Postgres write inside a transaction that is
    rolled back, and the Qdrant comparison read-only. Nothing is written, nothing is
    embedded, and no run is recorded."""
    report = DryRunReport(entry.document_key)
    try:
        prepared = prepare(entry, settings, setup=setup, setup_error=setup_error)
        validate_prepared(prepared)
    except Exception as exc:
        report.error = _describe(exc)
        return report
    chunks = prepared.result.chunks
    report.pages = len(prepared.pages)
    report.failed_pages = len(prepared.failed_pages)
    report.chunks = len(chunks)
    report.parents = sum(c.metadata.chunk_role.value == "parent" for c in chunks)
    report.eligible = sum(c.embeddable for c in chunks)
    report.exclusions = len(prepared.result.exclusions)

    if sessions is not None:
        try:
            with sessions() as s:
                s.begin()
                try:
                    report.persist = persist_document(s, prepared)
                finally:
                    s.rollback()
        except Exception as exc:
            report.notes.append(f"Postgres comparison skipped: {_describe(exc)}")
    if store is not None and embedder is not None:
        try:
            report.index = plan_index(prepared.result, embedder, store)
        except Exception as exc:
            report.notes.append(f"Qdrant comparison skipped: {_describe(exc)}")
    return report


# --- retry --------------------------------------------------------------------------------------


class RetryAction(StrEnum):
    NONE = "none"  # completed / partial: nothing to retry
    INDEX = "index"  # Postgres rows are current; only embedding/indexing must be redone
    FULL = "full"  # run every stage again


def retry_action(session: Session, entry: DocumentMetadata) -> tuple[RetryAction, str]:
    """Decide how to recover one document from its stored status and latest run."""
    doc = session.get(Document, entry.row_id)
    run = latest_run(session, entry.document_key)
    if doc is None:
        if run is not None and run.status == IngestionStatus.FAILED.value:
            return RetryAction.FULL, f"last run failed at {run.failed_stage}; not in Postgres"
        return RetryAction.FULL, "never ingested"
    status = doc.ingestion_status
    if status in (IngestionStatus.COMPLETED.value, IngestionStatus.PARTIAL.value):
        return RetryAction.NONE, status
    if status == IngestionStatus.PENDING.value:
        return RetryAction.INDEX, "persisted but not indexed"
    if (
        status == IngestionStatus.FAILED.value
        and run is not None
        and run.failed_stage in {s.value for s in INDEX_STAGES}
    ):
        return RetryAction.INDEX, f"last run failed at {run.failed_stage}"
    return RetryAction.FULL, f"status {status}" + (
        f", last run failed at {run.failed_stage}" if run and run.failed_stage else ""
    )
