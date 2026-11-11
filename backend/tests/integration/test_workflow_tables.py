"""M3 S5: migration 0006 (workflow tables) and the insert-only report store.

Throwaway databases (`temp_db`). Checks: additive and reversible; ORM == migration;
every report requires human review; no outcome outside the five; the JSON matches the
columns; UPDATE is rejected by the trigger; one active run per session; the store is
idempotent per report id and reads back exactly what it wrote.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.workflow import PostgresReportStore, create_session
from app.graph.report import DISCLAIMER, report_id
from app.graph.state import ComplianceReport, Outcome
from tests.integration.conftest import alembic_cfg, sync_engine

pytestmark = pytest.mark.integration
WORKFLOW = {"kyc_sessions", "graph_runs", "reports", "api_idempotency"}


def tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def report(session_id: str, run_id: str = "run-1", **kw: Any) -> ComplianceReport:
    base: dict[str, Any] = {
        "report_id": report_id(session_id, run_id),
        "session_id": session_id,
        "run_id": run_id,
        "outcome": Outcome.INSUFFICIENT_EVIDENCE,
        "user_message": f"No verified information.\n\n{DISCLAIMER}",
        "evidence_status": "none",
        "gaps": ["no_eligible_evidence"],
    }
    return ComplianceReport(**(base | kw))


@pytest.fixture
def db(temp_db: str) -> tuple[sa.Engine, sessionmaker[Session], str]:
    command.upgrade(alembic_cfg(), "head")
    engine = sync_engine(temp_db)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as s:
        row = create_session(s, ttl=timedelta(hours=24), data_class="synthetic_demo")
        s.commit()
        sid = str(row.id)
    return engine, sessions, sid


def test_0006_is_additive_and_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    engine = sync_engine(temp_db)
    command.upgrade(cfg, "0005_m2_gemini_method")
    before = tables(engine)
    assert not WORKFLOW & before
    command.upgrade(cfg, "0006_m3_workflow")
    assert tables(engine) == before | WORKFLOW
    command.check(cfg)  # ORM and migration agree
    command.downgrade(cfg, "0005_m2_gemini_method")
    assert tables(engine) == before
    with engine.connect() as conn:  # the trigger function went with it
        assert not conn.execute(
            sa.text("SELECT 1 FROM pg_proc WHERE proname = 'reports_reject_update'")
        ).first()
    command.upgrade(cfg, "head")
    assert tables(engine) >= WORKFLOW


def test_the_store_is_idempotent_and_reads_back_exactly(
    db: tuple[sa.Engine, sessionmaker[Session], str],
) -> None:
    engine, sessions, sid = db
    store = PostgresReportStore(sessions)
    r = report(sid)
    store.save(r)
    store.save(r)  # finalize ran twice: still one row
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM reports")).scalar() == 1
    assert store.get(r.report_id) == r
    assert store.get(str(uuid.uuid4())) is None


def test_reports_are_insert_only(db: tuple[sa.Engine, sessionmaker[Session], str]) -> None:
    engine, sessions, sid = db
    PostgresReportStore(sessions).save(report(sid))
    with pytest.raises(sa.exc.DBAPIError, match="insert-only"), engine.begin() as conn:
        conn.execute(sa.text("UPDATE reports SET evidence_status = 'found'"))
    with engine.begin() as conn:  # the retention purge (S8) may still delete
        conn.execute(sa.text("DELETE FROM reports"))


def raw_insert(engine: sa.Engine, sid: str, **overrides: Any) -> None:
    rid = str(uuid.uuid4())
    values: dict[str, Any] = {
        "id": rid,
        "sid": sid,
        "run": f"run-{rid}",
        "outcome": "DEGRADED",
        "review": True,
        "status": None,
        "json": f'{{"outcome": "DEGRADED", "report_id": "{rid}", "human_review_required": true}}',
    }
    values |= overrides
    if "outcome" in overrides and "json" not in overrides:  # keep the JSON consistent
        values["json"] = values["json"].replace('"DEGRADED"', f'"{overrides["outcome"]}"')
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO reports (id, session_id, run_id, outcome, human_review_required, "
                "evidence_status, report_json) VALUES (:id, :sid, :run, :outcome, :review, "
                ":status, CAST(:json AS jsonb))"
            ),
            values,
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"review": False}, "ck_reports_human_review"),
        ({"outcome": "APPROVED"}, "ck_reports_outcome"),
        ({"status": "relevant"}, "ck_reports_evidence_status"),
        (
            {"json": '{"outcome": "BLOCKED", "human_review_required": true}'},
            "ck_reports_json_identity",
        ),
    ],
)
def test_database_guards(
    db: tuple[sa.Engine, sessionmaker[Session], str], overrides: dict[str, Any], constraint: str
) -> None:
    engine, _, sid = db
    raw_insert(engine, sid)  # the valid baseline is accepted
    with pytest.raises(sa.exc.IntegrityError, match=constraint):
        raw_insert(engine, sid, **overrides)


def test_one_report_per_run_and_cascade_on_session_delete(
    db: tuple[sa.Engine, sessionmaker[Session], str],
) -> None:
    engine, sessions, sid = db
    PostgresReportStore(sessions).save(report(sid))
    with pytest.raises(sa.exc.IntegrityError, match="uq_reports_session_run"):
        raw_insert(engine, sid, run="run-1")
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM kyc_sessions"))
        assert conn.execute(sa.text("SELECT count(*) FROM reports")).scalar() == 0


def test_one_active_run_per_session(db: tuple[sa.Engine, sessionmaker[Session], str]) -> None:
    engine, _, sid = db

    def run(status: str) -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO graph_runs (id, session_id, thread_id, status) "
                    "VALUES (:id, :sid, :thread, :status)"
                ),
                {"id": str(uuid.uuid4()), "sid": sid, "thread": sid, "status": status},
            )

    run("completed")
    run("running")
    with pytest.raises(sa.exc.IntegrityError, match="uq_graph_runs_active"):
        run("awaiting_clarification")
    run("failed")


def test_sessions_need_a_valid_class_and_expiry(
    db: tuple[sa.Engine, sessionmaker[Session], str],
) -> None:
    _, sessions, _ = db
    now = datetime.now(UTC)
    with sessions() as s, pytest.raises(sa.exc.IntegrityError, match="ck_kyc_sessions_expiry"):
        create_session(s, ttl=timedelta(0), now=now)
    with sessions() as s, pytest.raises(ValueError):
        create_session(s, ttl=timedelta(hours=1), data_class="real_customer")
