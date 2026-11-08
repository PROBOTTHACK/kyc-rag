"""Durable per-document ingestion attempts (`ingestion_runs`).

Each write is its own short transaction, separate from the document's persistence
transaction. A run row has no FK to `documents`, so a failed first ingestion, whose
document row is rolled back, still leaves a record of which stage failed and why.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import IngestionRun
from app.schemas.corpus import DocumentMetadata
from app.schemas.taxonomy import IngestionStage, IngestionStatus, StageStatus

MAX_ERROR_CHARS = 2000  # keep run rows readable; full tracebacks belong in logs


def _clip(text: str | None) -> str | None:
    if text is None or len(text) <= MAX_ERROR_CHARS:
        return text
    return text[: MAX_ERROR_CHARS - 1] + "…"


class RunLog:
    """One ingestion attempt. `stage()` updates the in-memory stage map; `save()` and
    `finish()` write it."""

    def __init__(
        self, sessions: sessionmaker[Session], entry: DocumentMetadata, command: str
    ) -> None:
        self.sessions = sessions
        self.id = uuid.uuid4()
        self.entry = entry
        self.command = command
        self.stages: dict[str, dict[str, Any]] = {}

    def start(self) -> "RunLog":
        with self.sessions() as s, s.begin():
            s.add(
                IngestionRun(
                    id=self.id,
                    document_key=self.entry.document_key,
                    sha256=self.entry.sha256,
                    document_id=self.entry.row_id,
                    command=self.command,
                    status=IngestionStatus.PROCESSING.value,
                    stages={},
                )
            )
        return self

    def stage(
        self,
        stage: IngestionStage,
        status: StageStatus,
        *,
        error: str | None = None,
        **detail: Any,
    ) -> None:
        record: dict[str, Any] = {"status": status.value, **detail}
        if error is not None:
            record["error"] = _clip(error)
        self.stages[stage.value] = record

    def skip_remaining(self, after: IngestionStage) -> None:
        order = list(IngestionStage)
        for stage in order[order.index(after) + 1 :]:
            self.stages.setdefault(stage.value, {"status": StageStatus.SKIPPED.value})

    def save(self) -> None:
        with self.sessions() as s, s.begin():
            run = s.get(IngestionRun, self.id)
            if run is not None:
                run.stages = dict(self.stages)

    def finish(
        self,
        status: IngestionStatus,
        *,
        failed_stage: IngestionStage | None = None,
        error: str | None = None,
    ) -> None:
        with self.sessions() as s, s.begin():
            run = s.get(IngestionRun, self.id)
            if run is None:
                return
            run.stages = dict(self.stages)
            run.status = status.value
            run.failed_stage = failed_stage.value if failed_stage else None
            run.error = _clip(error)
            run.finished_at = func.clock_timestamp()


def recent_runs(
    session: Session,
    *,
    document_key: str | None = None,
    failed_only: bool = False,
    limit: int = 20,
) -> list[IngestionRun]:
    stmt = select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(limit)
    if document_key:
        stmt = stmt.where(IngestionRun.document_key == document_key)
    if failed_only:
        stmt = stmt.where(IngestionRun.status == IngestionStatus.FAILED.value)
    return list(session.scalars(stmt))


def latest_run(session: Session, document_key: str) -> IngestionRun | None:
    runs = recent_runs(session, document_key=document_key, limit=1)
    return runs[0] if runs else None
