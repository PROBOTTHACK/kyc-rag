"""M4 S1: report v2 needs no migration, and stored M3 (v1) reports still read back.

- A v1 report stored exactly as M3 wrote it (no `report_version` key) reads back through
  `PostgresReportStore.get` as report v1, with every stored value unchanged.
- A v2 report passes the existing `reports` CHECKs (outcome, JSON identity, human review)
  and the insert-only trigger, and reads back equal: no schema change is needed in S1.
- Every report already stored in the developer database (the M3 demo and live runs) still
  validates. Read-only; skipped when that database holds none.
"""

import json
import pathlib
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.db.workflow import PostgresReportStore, create_session
from app.graph.report import report_id
from app.graph.state import ComplianceReport
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.unit.test_m4_models import v2_report

pytestmark = pytest.mark.integration
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "m3_reports_v1"


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


def as_stored_by_m3(path: pathlib.Path, sid: str, run: str) -> dict[str, Any]:
    stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return stored | {"session_id": sid, "run_id": run, "report_id": report_id(sid, run)}


@pytest.mark.parametrize(
    "name", ["checklist_mandatory", "checklist_partial_llp", "blocked", "degraded_synthesis_failed"]
)
def test_a_report_stored_by_m3_reads_back_as_v1_unchanged(
    db: tuple[sa.Engine, sessionmaker[Session], str], name: str
) -> None:
    engine, sessions, sid = db
    stored = as_stored_by_m3(FIXTURES / f"{name}.json", sid, "run-v1")
    with engine.begin() as conn:  # the raw row M3's store wrote: no report_version key
        conn.execute(
            sa.text(
                "INSERT INTO reports (id, session_id, run_id, outcome, evidence_status, "
                "report_json) VALUES (:id, :sid, :run, :outcome, :es, CAST(:j AS jsonb))"
            ),
            {"id": stored["report_id"], "sid": sid, "run": "run-v1",
             "outcome": stored["outcome"], "es": stored["evidence_status"],
             "j": json.dumps(stored)},
        )  # fmt: skip
    report = PostgresReportStore(sessions).get(stored["report_id"])
    assert report is not None and report.report_version == 1
    assert {k: report.model_dump(mode="json")[k] for k in stored} == stored


def test_a_v2_report_is_stored_by_the_m3_schema_and_reads_back_equal(
    db: tuple[sa.Engine, sessionmaker[Session], str],
) -> None:
    engine, sessions, sid = db
    r = v2_report(report_id=report_id(sid, "run-v2"), session_id=sid, run_id="run-v2")
    store = PostgresReportStore(sessions)
    store.save(r)
    store.save(r)  # idempotent, as in M3
    assert store.get(r.report_id) == r
    with pytest.raises(sa.exc.DBAPIError, match="insert-only"), engine.begin() as conn:
        conn.execute(sa.text("UPDATE reports SET evidence_status = 'found'"))


def test_every_report_in_the_developer_database_still_validates() -> None:
    url = get_settings().DATABASE_URL.get_secret_value()
    engine = sync_engine(url)
    try:
        with engine.connect() as conn:
            exists = conn.execute(sa.text("SELECT to_regclass('public.reports')")).scalar()
            rows = (
                conn.execute(sa.text("SELECT report_json FROM reports")).scalars().all()
                if exists
                else []
            )
    finally:
        engine.dispose()
    if not rows:
        pytest.skip("the developer database holds no stored reports")
    for stored in rows:
        report = ComplianceReport.model_validate(stored)
        assert report.report_version == 1  # everything stored so far was written by M3
        assert {k: report.model_dump(mode="json")[k] for k in stored} == stored
