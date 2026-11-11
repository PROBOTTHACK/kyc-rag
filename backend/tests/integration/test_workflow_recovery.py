"""M3 S6: durability and explicit recovery across app instances (M3_PLAN §9).

- A paused workflow resumes from the Postgres checkpoint in a NEW app instance.
- A crash mid-run (the app stops while synthesis is in flight): /retry is refused while
  the lease is valid, then resumes from the last checkpoint after expiry; the old run is
  abandoned and exactly one report exists.
- A crash inside finalize (after the report insert): the run fails, /retry completes it,
  and still exactly one report exists.
- Startup only logs stale leases; nothing is re-executed automatically.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from alembic import command
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.workflow_harness import (
    LLP_QUESTION,
    FailOnceAfterSave,
    SchemaLLM,
    api,
    enable_app_logging,
    new_session,
    wait_for,
    wait_for_node,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(temp_db: str) -> Iterator[sa.Engine]:
    command.upgrade(alembic_cfg(), "head")
    enable_app_logging()  # Alembic's fileConfig disabled the app loggers
    eng = sync_engine(temp_db)
    yield eng
    eng.dispose()


def post(client: Any, path: str, body: Any, key: str) -> Any:
    return client.post(f"/api/v1{path}", content=json.dumps(body), headers={"Idempotency-Key": key})


def runs(engine: sa.Engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        return [
            (str(r[0]), r[1])
            for r in conn.execute(
                sa.text("SELECT thread_id, status FROM graph_runs ORDER BY started_at")
            )
        ]


def report_count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.text("SELECT count(*) FROM reports")).scalar() or 0)


def test_a_paused_workflow_resumes_in_a_new_app_instance(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as first:
        sid = new_session(first)
        post(first, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        clar = wait_for(first, sid, "awaiting_clarification")["workflow"]["clarification"]
    with api(SchemaLLM()) as second:  # a fresh process: new pool, new graph, new owner
        view = second.get(f"/api/v1/sessions/{sid}").json()
        assert view["workflow"]["clarification"]["checkpoint_id"] == clar["checkpoint_id"]
        body = {
            "checkpoint_id": clar["checkpoint_id"],
            "answers": {"account_type": "CURRENT_ACCOUNT"},
        }
        assert post(second, f"/sessions/{sid}/resume", body, "r").status_code == 202
        assert wait_for(second, sid, "completed")["workflow"]["outcome"]
    assert report_count(engine) == 1


def test_crash_mid_run_then_retry_after_the_lease_expires(engine: sa.Engine) -> None:
    with api(SchemaLLM(block_synthesis=True), lease_s=2.0) as first:
        sid = new_session(first)
        post(first, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        clar = wait_for(first, sid, "awaiting_clarification")["workflow"]["clarification"]
        body = {
            "checkpoint_id": clar["checkpoint_id"],
            "answers": {"account_type": "CURRENT_ACCOUNT"},
        }
        post(first, f"/sessions/{sid}/resume", body, "r")
        wait_for_node(first, sid, "evidence_association")  # synthesis is now in flight
    # the app stopped mid-run: the row is still `running` with an unexpired lease
    assert runs(engine)[-1][1] == "running"

    with api(SchemaLLM(), lease_s=2.0) as second:
        with engine.begin() as conn:  # push the lease into the future, as if just renewed
            conn.execute(
                sa.text(
                    "UPDATE graph_runs SET lease_expires_at = now() + "
                    "interval '1 hour' WHERE status = 'running'"
                )
            )
        early = post(second, f"/sessions/{sid}/retry", {}, "t1")
        assert (early.status_code, early.json()["error"]["code"]) == (409, "RUN_IN_PROGRESS")
        assert early.json()["error"]["retry_after"] > 0 and early.headers["Retry-After"]
        with engine.begin() as conn:  # the lease expires
            conn.execute(
                sa.text(
                    "UPDATE graph_runs SET lease_expires_at = now() - "
                    "interval '1 second' WHERE status = 'running'"
                )
            )
        ok = post(second, f"/sessions/{sid}/retry", {}, "t2")
        assert ok.status_code == 202
        assert post(second, f"/sessions/{sid}/retry", {}, "t2").json() == ok.json()  # replay
        wf = wait_for(second, sid, "completed")["workflow"]
        assert wf["outcome"] == "CHECKLIST_REVIEW_REQUIRED"
        node_names = [e["node"] for e in wf["node_events"]]
        assert node_names[0] == "compliance_synthesis"  # resumed from the last checkpoint
    statuses = [s for _, s in runs(engine)]
    assert statuses == ["abandoned", "completed"]
    assert len({t for t, _ in runs(engine)}) == 1  # the same thread throughout
    assert report_count(engine) == 1


def test_crash_inside_finalize_still_yields_one_report(engine: sa.Engine) -> None:
    with api(SchemaLLM(), store_wrapper=FailOnceAfterSave) as client:
        sid = new_session(client)
        post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        clar = wait_for(client, sid, "awaiting_clarification")["workflow"]["clarification"]
        body = {
            "checkpoint_id": clar["checkpoint_id"],
            "answers": {"account_type": "CURRENT_ACCOUNT"},
        }
        post(client, f"/sessions/{sid}/resume", body, "r")
        failed = wait_for(client, sid, "failed")["workflow"]
        assert failed["node_events"][-1] == {
            **failed["node_events"][-1],
            "node": "error:RuntimeError",
            "status": "failed",
        }
        assert report_count(engine) == 1  # inserted before the crash
        assert post(client, f"/sessions/{sid}/retry", {}, "t").status_code == 202
        assert wait_for(client, sid, "completed")["workflow"]["report_id"]
    assert report_count(engine) == 1  # ON CONFLICT DO NOTHING on the same report id


def test_startup_only_logs_stale_leases(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        sid = uuid.uuid4()
        conn.execute(
            sa.text(
                "INSERT INTO kyc_sessions (id, created_at, expires_at) VALUES "
                "(:id, now(), now() + interval '1 hour')"
            ),
            {"id": sid},
        )
        conn.execute(
            sa.text(
                "INSERT INTO graph_runs (id, session_id, thread_id, status, "
                "lease_expires_at) VALUES (:id, :sid, 't', 'running', "
                "now() - interval '1 minute')"
            ),
            {"id": uuid.uuid4(), "sid": sid},
        )
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture(level=logging.WARNING)
    logging.getLogger("app.workflow.service").addHandler(handler)
    try:
        with api(SchemaLLM()) as client:  # startup runs the stale-lease check
            # the same check, called directly: it reports and changes nothing
            assert client.portal.call(client.app.state.workflow.log_stale_leases) == 1
    finally:
        logging.getLogger("app.workflow.service").removeHandler(handler)
    assert any("1 workflow run(s) have expired leases" in m for m in records)
    assert runs(engine) == [("t", "running")]  # untouched: never re-executed
