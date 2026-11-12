"""M4 S7 on PostgreSQL: migration 0008, the insert-only `review_cases` table, the atomic and
idempotent store, the read-only endpoint, retention and `check-workflow`.

Throwaway databases (`temp_db`); the real app with the synthetic S4 corpus and the
schema-aware fake LLM (workflow harness). NLI is disabled, as in production.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import app.db.workflow as workflow_db
from alembic import command
from app.config import get_settings
from app.db.workflow import PostgresReportStore, create_session
from app.graph.report import report_id
from app.graph.review import ReviewCase, review_cases
from app.graph.state import ComplianceReport
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.test_workflow_retention import (  # noqa: F401 (fixture)
    age,
    check,
    completed,
    engine,
    run_purge,
)
from tests.integration.test_workflow_tables import report as insufficient_report
from tests.integration.workflow_harness import SchemaLLM, api
from tests.unit.test_m4_graph_s5 import trust_req, trust_run

pytestmark = pytest.mark.integration


def scalar(eng: sa.Engine, sql: str, **params: Any) -> Any:
    with eng.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


@pytest.fixture
def db(temp_db: str) -> tuple[sa.Engine, sessionmaker[Session], str]:
    command.upgrade(alembic_cfg(), "head")
    eng = sync_engine(temp_db)
    sessions = sessionmaker(eng, expire_on_commit=False)
    with sessions() as s:
        sid = str(create_session(s, ttl=timedelta(hours=24), data_class="synthetic_demo").id)
        s.commit()
    return eng, sessions, sid


def v2_report(sid: str, run: str = "run-1") -> ComplianceReport:
    """A real NLI-disabled v2 report (two checklist items), re-keyed to a stored session."""
    raw = trust_run(trust_req(), trust_req("CONDITIONAL"))["report"]
    return ComplianceReport.model_validate(
        raw | {"session_id": sid, "run_id": run, "report_id": report_id(sid, run)}
    )


def stored_cases(eng: sa.Engine, rid: str) -> list[dict[str, Any]]:
    with eng.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT * FROM review_cases WHERE report_id = :r "
                    "ORDER BY requirement_id, reason"), {"r": rid}  # fmt: skip
        ).mappings()
        return [dict(r) for r in rows]


# --- migration -----------------------------------------------------------------------------------


def test_0008_is_additive_and_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    eng = sync_engine(temp_db)
    command.upgrade(cfg, "0007_m3_checkpointer")
    before = set(sa.inspect(eng).get_table_names())
    assert "review_cases" not in before
    command.upgrade(cfg, "0008_m4_review_cases")
    assert set(sa.inspect(eng).get_table_names()) == before | {"review_cases"}
    command.downgrade(cfg, "0007_m3_checkpointer")
    assert set(sa.inspect(eng).get_table_names()) == before
    assert not scalar(eng, "SELECT 1 FROM pg_proc WHERE proname = 'review_cases_reject_update'")
    command.upgrade(cfg, "head")
    command.check(cfg)  # ORM == migrations (make check-schema)


# --- the store: one transaction, idempotent ------------------------------------------------------


def test_save_writes_the_report_and_its_cases_together_and_idempotently(
    db: tuple[sa.Engine, sessionmaker[Session], str],
) -> None:
    eng, sessions, sid = db
    store = PostgresReportStore(sessions)
    r = v2_report(sid)
    expected = review_cases(r)
    assert len(expected) == 2 and {c.reason for c in expected} == {"not_verified"}
    store.save(r)
    rows = stored_cases(eng, r.report_id)
    assert [str(x["id"]) for x in rows] == [c.id for c in expected]
    for row in rows:
        assert (row["status"], row["claim_id"], row["reason"]) == ("open", None, "not_verified")
        assert (str(row["session_id"]), row["run_id"]) == (sid, "run-1")
    stored_json = scalar(eng, "SELECT report_json::text FROM reports WHERE id = :i",
                         i=r.report_id)  # fmt: skip
    store.save(r)  # again: nothing new, nothing changed
    assert stored_cases(eng, r.report_id) == rows
    assert scalar(eng, "SELECT count(*) FROM reports WHERE id = :i", i=r.report_id) == 1
    assert scalar(eng, "SELECT report_json::text FROM reports WHERE id = :i",
                  i=r.report_id) == stored_json  # fmt: skip
    assert store.get(r.report_id) == r


def test_a_failed_case_insert_rolls_back_the_report(
    db: tuple[sa.Engine, sessionmaker[Session], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    eng, sessions, sid = db
    r = v2_report(sid)

    def bad_cases(report: ComplianceReport) -> list[ReviewCase]:
        good = review_cases(report)[0]
        return [ReviewCase.model_construct(**(dict(good) | {"reason": "approved"}))]

    monkeypatch.setattr(workflow_db, "review_cases", bad_cases)
    with pytest.raises(IntegrityError):  # the CHECK rejects the reason
        PostgresReportStore(sessions).save(r)
    assert scalar(eng, "SELECT count(*) FROM reports WHERE id = :i", i=r.report_id) == 0
    assert scalar(eng, "SELECT count(*) FROM review_cases") == 0


def test_a_report_without_cases_stores_none(
    db: tuple[sa.Engine, sessionmaker[Session], str],
) -> None:
    eng, sessions, sid = db
    r = insufficient_report(sid)
    PostgresReportStore(sessions).save(r)
    assert stored_cases(eng, r.report_id) == []
    assert PostgresReportStore(sessions).review_cases_for(r.report_id) == []


# --- the table refuses anything but open, insert-only cases --------------------------------------


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("UPDATE review_cases SET reason = 'unsupported'", "insert-only"),
        ("UPDATE review_cases SET status = 'open'", "insert-only"),  # even a no-op update
        ("INSERT INTO review_cases (id, report_id, session_id, run_id, requirement_id, reason, "
         "status) SELECT gen_random_uuid(), report_id, session_id, run_id, 'R9', 'not_verified', "
         "'closed' FROM review_cases LIMIT 1", "ck_review_cases_open_only"),
        ("INSERT INTO review_cases (id, report_id, session_id, run_id, requirement_id, reason) "
         "SELECT gen_random_uuid(), report_id, session_id, run_id, 'R9', 'approved' "
         "FROM review_cases LIMIT 1", "ck_review_cases_reason"),
        ("INSERT INTO review_cases (id, report_id, session_id, run_id, requirement_id, reason) "
         "SELECT gen_random_uuid(), gen_random_uuid(), session_id, run_id, 'R9', 'not_verified' "
         "FROM review_cases LIMIT 1", "review_cases_report_id_fkey"),
        ("INSERT INTO review_cases (id, report_id, session_id, run_id, requirement_id, reason) "
         "SELECT gen_random_uuid(), report_id, session_id, run_id, requirement_id, reason "
         "FROM review_cases LIMIT 1", "uq_review_cases_report_req_reason"),
    ],
)  # fmt: skip
def test_the_table_is_insert_only_and_open_only(
    db: tuple[sa.Engine, sessionmaker[Session], str], sql: str, error: str
) -> None:
    eng, sessions, sid = db
    PostgresReportStore(sessions).save(v2_report(sid))
    with pytest.raises(DBAPIError, match=error), eng.begin() as conn:
        conn.execute(sa.text(sql))


# --- the read-only endpoint ----------------------------------------------------------------------


def test_the_endpoint_lists_the_open_cases_of_a_real_run(engine: sa.Engine) -> None:  # noqa: F811
    with api(SchemaLLM()) as client:
        sid, rid = completed(client)
        report = client.get(f"/api/v1/reports/{rid}").json()
        body = client.get(f"/api/v1/reports/{rid}/review-cases").json()
        n_items = len(report["checklist"])
        assert n_items >= 1 and body["report_id"] == rid
        cases = body["review_cases"]
        assert len(cases) == n_items  # one not_verified case per checklist item (NLI off)
        assert {c["reason"] for c in cases} == {"not_verified"}
        assert all(c["status"] == "open" and c["claim_id"] is None for c in cases)
        assert sorted(c["requirement_id"] for c in cases) == sorted(
            i["requirement_id"] for i in report["checklist"]
        )
        assert set(cases[0]) == {"id", "requirement_id", "claim_id", "reason", "status",
                                 "created_at"}  # fmt: skip
        # the report itself is unchanged by S7: no case data inside it
        assert "review_cases" not in report
        for bad in ("not-a-uuid", str(uuid.uuid4())):
            r = client.get(f"/api/v1/reports/{bad}/review-cases")
            assert (r.status_code, r.json()["error"]["code"]) == (404, "REPORT_NOT_FOUND")
        for method in ("post", "put", "patch", "delete"):
            r = getattr(client, method)(f"/api/v1/reports/{rid}/review-cases")
            assert r.status_code == 405, method
        for path in (f"/api/v1/review-cases/{cases[0]['id']}",
                     f"/api/v1/reports/{rid}/review-cases/{cases[0]['id']}/decision"):  # fmt: skip
            assert client.post(path, json={"decision": "approve"}).status_code in (404, 405)


def test_the_endpoint_returns_an_empty_list_for_a_report_without_cases(
    engine: sa.Engine,  # noqa: F811
) -> None:
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as s:
        sid = str(create_session(s, ttl=timedelta(hours=24), data_class="synthetic_demo").id)
        s.commit()
    r = insufficient_report(sid)
    PostgresReportStore(sessions).save(r)
    with api(SchemaLLM()) as client:
        body = client.get(f"/api/v1/reports/{r.report_id}/review-cases").json()
    assert body == {"report_id": r.report_id, "review_cases": []}


# --- retention and check-workflow ----------------------------------------------------------------


def test_phase1_keeps_cases_readable_and_phase2_deletes_them(
    engine: sa.Engine,  # noqa: F811
) -> None:
    with api(SchemaLLM()) as client:
        sid, rid = completed(client)
        before = client.get(f"/api/v1/reports/{rid}/review-cases").json()
        assert before["review_cases"]
        age(engine, sid, expired_days=1)
        assert run_purge(engine).phase1 == [sid]
        assert client.get(f"/api/v1/reports/{rid}/review-cases").json() == before
        age(engine, sid, expired_days=get_settings().REPORT_RETENTION_D + 1)
        assert run_purge(engine).phase2 == [sid]
        r = client.get(f"/api/v1/reports/{rid}/review-cases")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "REPORT_NOT_FOUND")
    assert scalar(engine, "SELECT count(*) FROM review_cases WHERE session_id = :i", i=sid) == 0


def test_check_workflow_validates_count_contents_and_status(
    engine: sa.Engine,  # noqa: F811
) -> None:
    with api(SchemaLLM()) as client:
        _, rid = completed(client)
    found = check(engine)
    n = scalar(engine, "SELECT count(*) FROM review_cases")
    assert n >= 1 and found.review_cases == n and not found.problems
    assert found.as_dict()["review_case_problems"] == []
    case_id = str(scalar(engine, "SELECT id FROM review_cases WHERE report_id = :r LIMIT 1",
                         r=rid))  # fmt: skip
    # a missing case (DELETE stays possible for the purge cascade) is reported
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM review_cases WHERE id = :i"), {"i": case_id})
    found = check(engine)
    assert found.problems and found.review_case_problems == [f"review_case_missing:{rid}:{case_id}"]
    # a case under the same id but with other contents is a mismatch
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO review_cases (id, report_id, session_id, run_id, requirement_id, reason) "
            "SELECT :i, id, session_id, run_id, 'R-extra', 'unsupported' FROM reports "
            "WHERE id = :r"), {"i": case_id, "r": rid})  # fmt: skip
    problems = check(engine).review_case_problems
    assert f"review_case_mismatch:{rid}:{case_id}" in problems
    # a case the report does not imply is unexpected
    extra = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO review_cases (id, report_id, session_id, run_id, requirement_id, reason) "
            "SELECT :i, id, session_id, run_id, 'R-other', 'contradicted' FROM reports "
            "WHERE id = :r"), {"i": extra, "r": rid})  # fmt: skip
    assert f"review_case_unexpected:{rid}:{extra}" in check(engine).review_case_problems
