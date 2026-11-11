"""Workflow persistence (M3 S5; migration 0006): sessions and insert-only reports.

`PostgresReportStore` is the `ReportStore` the S6 API injects into the graph. Reports are
written once (`INSERT ... ON CONFLICT DO NOTHING` on the deterministic report id), never
updated (a database trigger rejects UPDATE), and every one requires human review (a CHECK
rejects anything else). S6 adds the async run, lease, node-event and idempotency
functions used by the workflow API.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ApiIdempotency, GraphRun, KycSession, ReportRow
from app.graph.state import ComplianceReport, DataClass


def create_session(
    session: Session,
    *,
    ttl: timedelta,
    data_class: DataClass | str = DataClass.UNSPECIFIED,
    session_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> KycSession:
    """A new active session (the caller commits)."""
    created = now or datetime.now(UTC)
    row = KycSession(
        id=session_id or uuid.uuid4(),
        status="active",
        data_class=DataClass(data_class).value,
        created_at=created,
        expires_at=created + ttl,
    )
    session.add(row)
    session.flush()
    return row


@dataclass
class PostgresReportStore:
    sessions: sessionmaker[Session]

    def save(self, report: ComplianceReport) -> None:
        if report.session_id is None or report.run_id is None:
            raise ValueError("a stored report needs its session and run")
        payload = report.model_dump(mode="json")
        stmt = (
            insert(ReportRow)
            .values(
                id=uuid.UUID(report.report_id),
                session_id=uuid.UUID(report.session_id),
                run_id=report.run_id,
                outcome=report.outcome.value,
                human_review_required=True,
                evidence_status=report.evidence_status,
                report_json=payload,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        with self.sessions() as session, session.begin():
            session.execute(stmt)

    def get(self, report_id: str) -> ComplianceReport | None:
        with self.sessions() as session:
            row = session.get(ReportRow, uuid.UUID(report_id))
            return None if row is None else ComplianceReport.model_validate(row.report_json)


# --- M3 S6: runs, leases, node events, idempotency (async; M3_PLAN §9) ----------------------
# Postgres is the only authority: the partial unique index `uq_graph_runs_active` allows
# one running/awaiting run per session, and every state change is a conditional UPDATE
# (compare-and-set) so concurrent requests cannot both win.

ACTIVE = ("running", "awaiting_clarification")


async def get_session_row(db: AsyncSession, session_id: uuid.UUID) -> KycSession | None:
    return await db.get(KycSession, session_id)


async def latest_run(db: AsyncSession, session_id: uuid.UUID) -> GraphRun | None:
    return await db.scalar(
        select(GraphRun)
        .where(GraphRun.session_id == session_id)
        .order_by(GraphRun.started_at.desc(), GraphRun.id)
        .limit(1)
    )


async def insert_running(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    thread_id: str,
    owner_id: str,
    lease_s: float,
) -> None:
    """A new `running` row; IntegrityError on `uq_graph_runs_active` if one is active."""
    await db.execute(
        text(
            "INSERT INTO graph_runs (id, session_id, thread_id, status, owner_id, heartbeat_at, "
            "lease_expires_at) VALUES (:id, :sid, :thread, 'running', :owner, now(), "
            "now() + make_interval(secs => :lease))"
        ),
        {"id": run_id, "sid": session_id, "thread": thread_id, "owner": owner_id, "lease": lease_s},
    )


async def claim_awaiting(
    db: AsyncSession, run_id: uuid.UUID, owner_id: str, lease_s: float
) -> bool:
    """awaiting_clarification -> running, for exactly one caller."""
    result = await db.execute(
        text(
            "UPDATE graph_runs SET status = 'running', owner_id = :owner, heartbeat_at = now(), "
            "lease_expires_at = now() + make_interval(secs => :lease) "
            "WHERE id = :id AND status = 'awaiting_clarification'"
        ),
        {"id": run_id, "owner": owner_id, "lease": lease_s},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def abandon(db: AsyncSession, run_id: uuid.UUID, *, require_expired: bool) -> bool:
    """running (with an expired lease) or failed -> abandoned, for exactly one caller."""
    condition = (
        "status = 'running' AND lease_expires_at < now()"
        if require_expired
        else "status = 'failed'"
    )
    result = await db.execute(
        text(
            f"UPDATE graph_runs SET status = 'abandoned', finished_at = now() "  # noqa: S608
            f"WHERE id = :id AND {condition}"
        ),
        {"id": run_id},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def renew_lease(db: AsyncSession, run_id: uuid.UUID, owner_id: str, lease_s: float) -> bool:
    result = await db.execute(
        text(
            "UPDATE graph_runs SET heartbeat_at = now(), "
            "lease_expires_at = now() + make_interval(secs => :lease) "
            "WHERE id = :id AND owner_id = :owner AND status = 'running'"
        ),
        {"id": run_id, "owner": owner_id, "lease": lease_s},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def append_node_event(
    db: AsyncSession, run_id: uuid.UUID, owner_id: str, node: str, status: str = "completed"
) -> None:
    """Append `{seq, node, status, at}`; seq is gap-free per run (computed in the UPDATE)."""
    await db.execute(
        text(
            "UPDATE graph_runs SET node_events = node_events || jsonb_build_array("
            "jsonb_build_object('seq', jsonb_array_length(node_events) + 1, 'node', "
            "CAST(:node AS text), 'status', CAST(:status AS text), 'at', now())) "
            "WHERE id = :id AND owner_id = :owner"
        ),
        {"id": run_id, "owner": owner_id, "node": node, "status": status},
    )


async def finish_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    owner_id: str,
    *,
    status: str,
    latency_ms: int | None = None,
    llm_calls: list[Any] | None = None,
) -> None:
    """running -> awaiting_clarification | completed | failed (only by its owner)."""
    await db.execute(
        text(
            "UPDATE graph_runs SET status = :status, lease_expires_at = NULL, "
            "finished_at = CASE WHEN :status = 'awaiting_clarification' THEN NULL ELSE now() END, "
            "latency_ms = :latency, llm_calls = CAST(:calls AS jsonb) "
            "WHERE id = :id AND owner_id = :owner AND status = 'running'"
        ),
        {
            "id": run_id,
            "owner": owner_id,
            "status": status,
            "latency": latency_ms,
            "calls": json.dumps(llm_calls or []),
        },
    )


async def count_stale_leases(db: AsyncSession) -> int:
    value = await db.scalar(
        text(
            "SELECT count(*) FROM graph_runs WHERE status = 'running' AND lease_expires_at < now()"
        )
    )
    return int(value or 0)


async def reserve_idempotency(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    key: str,
    endpoint: str,
    request_sha256: str,
    response: dict[str, Any],
) -> ApiIdempotency | None:
    """Insert the key; on conflict return the existing row (the caller compares hashes).
    Concurrent duplicates wait on the key until the first transaction ends."""
    inserted = await db.scalar(
        pg_insert(ApiIdempotency)
        .values(
            session_id=session_id,
            key=key,
            endpoint=endpoint,
            request_sha256=request_sha256,
            response_json=response,
        )
        .on_conflict_do_nothing(index_elements=["session_id", "key"])
        .returning(ApiIdempotency.key)
    )
    if inserted is not None:
        return None
    return await db.get(ApiIdempotency, (session_id, key))


async def get_idempotency(
    db: AsyncSession, session_id: uuid.UUID, key: str
) -> ApiIdempotency | None:
    return await db.get(ApiIdempotency, (session_id, key))
