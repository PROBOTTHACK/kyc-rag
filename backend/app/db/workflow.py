"""Workflow persistence (M3 S5; migration 0006): sessions and insert-only reports.

`PostgresReportStore` is the `ReportStore` the S6 API injects into the graph. Reports are
written once (`INSERT ... ON CONFLICT DO NOTHING` on the deterministic report id), never
updated (a database trigger rejects UPDATE), and every one requires human review (a CHECK
rejects anything else). `graph_runs` and `api_idempotency` are used from S6.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import KycSession, ReportRow
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
