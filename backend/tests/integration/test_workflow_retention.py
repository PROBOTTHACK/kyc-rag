"""M3 S8: the retention purge against the real app, checkpointer and workflow tables.

Covers the approved S8 acceptance and the owner's safety corrections:
- phase 1 deletes the thread's data from EVERY checkpoint table and the idempotency
  rows, abandons the run and makes the session non-resumable (410), while reads, the
  report and the SSE replay keep working;
- phase 2 deletes the checkpoint data first and only then the session (cascade), and a
  failure in the checkpoint step retains everything for another attempt;
- orphan detection covers checkpoints, checkpoint_blobs and checkpoint_writes;
- a dry run is byte-identical; a second purge has nothing to do;
- the live lease is rechecked under row locks at execution time: a valid lease, a held
  lock or a state change since planning skips the session safely with a reason;
- residue written after phase 1 (a stale writer) is reported and removed;
- the idempotency TTL prunes only old rows.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from alembic import command
from app.config import get_settings
from app.db.session import create_sync_sessionmaker
from app.workflow import cli, retention
from app.workflow.retention import EXPECTED_TABLES, integrity, purge
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.workflow_harness import (
    LLP_QUESTION,
    SchemaLLM,
    all_db_bytes,
    api,
    enable_app_logging,
    new_session,
    wait_for,
    wait_for_node,
)

pytestmark = pytest.mark.integration
ANSWER = {"account_type": "CURRENT_ACCOUNT"}
TABLES = sorted(EXPECTED_TABLES)
COUNT_SQL = "SELECT count(*) FROM langgraph.{t} WHERE thread_id = ANY(:th)"


@pytest.fixture
def engine(temp_db: str) -> Iterator[sa.Engine]:
    command.upgrade(alembic_cfg(), "head")
    enable_app_logging()
    eng = sync_engine(temp_db)
    yield eng
    eng.dispose()


def run_purge(engine: sa.Engine, *, dry_run: bool = False) -> retention.PurgeReport:
    return purge(create_sync_sessionmaker(engine), get_settings(), dry_run=dry_run)


def post(client: TestClient, path: str, body: Any, key: str) -> Any:
    return client.post(f"/api/v1{path}", content=json.dumps(body), headers={"Idempotency-Key": key})


def paused(client: TestClient) -> str:
    sid = new_session(client)
    post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
    wait_for(client, sid, "awaiting_clarification")
    return sid


def completed(client: TestClient) -> tuple[str, str]:
    sid = paused(client)
    clar = client.get(f"/api/v1/sessions/{sid}").json()["workflow"]["clarification"]
    post(
        client,
        f"/sessions/{sid}/resume",
        {"checkpoint_id": clar["checkpoint_id"], "answers": ANSWER},
        "r",
    )
    wf = wait_for(client, sid, "completed")["workflow"]
    return sid, str(wf["report_id"])


def age(engine: sa.Engine, sid: str, *, expired_days: float) -> None:
    """Move the session's expiry `expired_days` into the past (created_at before it)."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE kyc_sessions SET created_at = now() - make_interval(secs => :c), "
                "expires_at = now() - make_interval(secs => :e) WHERE id = :id"
            ),
            {"id": sid, "c": (expired_days + 1) * 86400, "e": expired_days * 86400},
        )


def threads(engine: sa.Engine, sid: str) -> list[str]:
    with engine.connect() as conn:
        return [
            str(t)
            for t in conn.execute(
                sa.text("SELECT DISTINCT thread_id FROM graph_runs WHERE session_id = :id"),
                {"id": sid},
            ).scalars()
        ]


def checkpoint_rows(engine: sa.Engine, thread_ids: list[str]) -> dict[str, int]:
    # table names come from the fixed TABLES constant
    with engine.connect() as conn:
        return {
            t: int(
                conn.execute(
                    sa.text(COUNT_SQL.format(t=t)),
                    {"th": thread_ids},
                ).scalar_one()
            )
            for t in TABLES
        }


def scalar(engine: sa.Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


def insert_checkpoint_data(engine: sa.Engine, thread: str, tables: list[str]) -> None:
    """Fabricate per-thread rows in the given checkpoint tables (orphans or residue)."""
    with engine.begin() as conn:
        if "checkpoints" in tables:
            conn.execute(
                sa.text(
                    "INSERT INTO langgraph.checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                    "type, checkpoint, metadata) VALUES (:t, '', :c, 'json', '{}', '{}')"
                ),
                {"t": thread, "c": str(uuid.uuid4())},
            )
        if "checkpoint_blobs" in tables:
            conn.execute(
                sa.text(
                    "INSERT INTO langgraph.checkpoint_blobs (thread_id, checkpoint_ns, channel, "
                    "version, type, blob) VALUES (:t, '', 'x', '1', 'empty', NULL)"
                ),
                {"t": thread},
            )
        if "checkpoint_writes" in tables:
            conn.execute(
                sa.text(
                    "INSERT INTO langgraph.checkpoint_writes (thread_id, checkpoint_ns, "
                    "checkpoint_id, task_id, idx, channel, type, blob, task_path) VALUES "
                    "(:t, '', :c, 'task', 0, 'x', 'empty', '\\x00', '')"
                ),
                {"t": thread, "c": str(uuid.uuid4())},
            )


def check(engine: sa.Engine) -> retention.IntegrityReport:
    with create_sync_sessionmaker(engine)() as db, db.begin():
        return integrity(db, get_settings())


# --- schema --------------------------------------------------------------------------------


def test_the_purge_covers_every_per_thread_checkpoint_table(engine: sa.Engine) -> None:
    with create_sync_sessionmaker(engine)() as db:
        assert retention.checkpoint_tables(db) == TABLES  # discovered from the live schema


# --- phase 1 -------------------------------------------------------------------------------


def test_phase1_purges_checkpoints_and_makes_the_session_read_only(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        sid = paused(client)
        th = threads(engine, sid)
        assert all(checkpoint_rows(engine, th).values())  # every table holds data
        age(engine, sid, expired_days=0.01)
        report = run_purge(engine)
        assert report.phase1 == [sid] and not report.skipped
        assert checkpoint_rows(engine, th) == {t: 0 for t in TABLES}
        assert (
            scalar(engine, "SELECT count(*) FROM api_idempotency WHERE session_id = :i", i=sid) == 0
        )
        assert (
            scalar(engine, "SELECT status FROM graph_runs WHERE session_id = :i", i=sid)
            == "abandoned"
        )
        assert scalar(engine, "SELECT status FROM kyc_sessions WHERE id = :i", i=sid) == "expired"
        assert scalar(engine, "SELECT checkpoints_purged_at FROM kyc_sessions WHERE id = :i", i=sid)
        # non-resumable: every write is 410
        for path, body in (
            (f"/sessions/{sid}/resume", {"checkpoint_id": str(uuid.uuid4()), "answers": ANSWER}),
            (f"/sessions/{sid}/messages", {"message": LLP_QUESTION}),
            (f"/sessions/{sid}/retry", {}),
        ):
            r = post(client, path, body, f"k-{path}")
            assert (r.status_code, r.json()["error"]["code"]) == (410, "SESSION_EXPIRED"), path
        # reads still work, and the SSE replay ends on the terminal `abandoned`
        view = client.get(f"/api/v1/sessions/{sid}").json()
        assert view["status"] == "expired" and view["workflow"]["status"] == "abandoned"
        stream = client.get(f"/api/v1/sessions/{sid}/events").text
        assert '"status":"abandoned"' in stream and '"node":"entity_intake"' in stream
    assert check(engine).problems is False
    assert run_purge(engine).phase1 == []  # idempotent: nothing left to do


def test_phase1_keeps_reports_readable(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        sid, report_id = completed(client)
        age(engine, sid, expired_days=1)
        assert run_purge(engine).phase1 == [sid]
        assert client.get(f"/api/v1/reports/{report_id}").status_code == 200
        assert (
            scalar(engine, "SELECT status FROM graph_runs WHERE session_id = :i", i=sid)
            == "completed"
        )


# --- phase 2 -------------------------------------------------------------------------------


def test_phase2_deletes_checkpoints_first_then_cascades(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        old, _ = completed(client)
        keep, keep_report = completed(client)
    th = threads(engine, old)
    age(engine, old, expired_days=get_settings().REPORT_RETENTION_D + 1)
    before_keep = checkpoint_rows(engine, threads(engine, keep))
    report = run_purge(engine)
    assert report.phase2 == [old] and not report.skipped
    assert checkpoint_rows(engine, th) == {t: 0 for t in TABLES}
    for table in ("kyc_sessions", "graph_runs", "reports", "api_idempotency"):
        col = "id" if table == "kyc_sessions" else "session_id"
        sql = f"SELECT count(*) FROM {table} WHERE {col} = :i"  # noqa: S608 - fixed names
        assert scalar(engine, sql, i=old) == 0
    # the other session is untouched
    assert checkpoint_rows(engine, threads(engine, keep)) == before_keep
    assert scalar(engine, "SELECT count(*) FROM reports WHERE id = :i", i=keep_report) == 1


@pytest.mark.parametrize("phase_days", [0.01, 31.0], ids=["phase1", "phase2"])
def test_a_failed_checkpoint_delete_retains_everything(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, phase_days: float
) -> None:
    with api(SchemaLLM()) as client:
        sid, report_id = completed(client)
    th = threads(engine, sid)
    rows = checkpoint_rows(engine, th)
    age(engine, sid, expired_days=phase_days)
    original = retention._delete_threads

    def half_delete(db: Any, tables: list[str], thread_ids: list[str]) -> dict[str, int]:
        db.execute(
            sa.text("DELETE FROM langgraph.checkpoints WHERE thread_id = ANY(:th)"),
            {"th": thread_ids},
        )
        raise retention.RetentionError("simulated failure mid-delete")

    monkeypatch.setattr(retention, "_delete_threads", half_delete)
    report = run_purge(engine)
    assert [(s.session_id, s.reason) for s in report.skipped] == [(sid, "error:RetentionError")]
    assert report.failed and cli.main(["purge"]) == 1  # reported by the CLI
    # rolled back: checkpoints, session, report and idempotency rows all retained
    assert checkpoint_rows(engine, th) == rows
    assert scalar(engine, "SELECT status FROM kyc_sessions WHERE id = :i", i=sid) == "active"
    assert scalar(engine, "SELECT count(*) FROM reports WHERE id = :i", i=report_id) == 1
    assert scalar(engine, "SELECT count(*) FROM api_idempotency WHERE session_id = :i", i=sid) > 0
    # the next purge retries and succeeds
    monkeypatch.setattr(retention, "_delete_threads", original)
    report = run_purge(engine)
    assert sid in report.phase1 + report.phase2 and not report.skipped
    assert checkpoint_rows(engine, th) == {t: 0 for t in TABLES}


# --- live writers, locks, state changes ------------------------------------------------------


def test_a_valid_lease_is_never_purged_then_purged_once_expired(engine: sa.Engine) -> None:
    with api(SchemaLLM(block_synthesis=True), lease_s=60.0) as client:
        sid = paused(client)
        clar = client.get(f"/api/v1/sessions/{sid}").json()["workflow"]["clarification"]
        post(
            client,
            f"/sessions/{sid}/resume",
            {"checkpoint_id": clar["checkpoint_id"], "answers": ANSWER},
            "r",
        )
        wait_for_node(client, sid, "evidence_association")  # a live writer, valid lease
        th = threads(engine, sid)
        rows = checkpoint_rows(engine, th)
        age(engine, sid, expired_days=0.01)
        report = run_purge(engine)
        assert [(s.session_id, s.reason) for s in report.skipped] == [(sid, "live_lease")]
        after = checkpoint_rows(engine, th)  # the live writer may add rows; none deleted
        assert all(after[t] >= rows[t] > 0 for t in TABLES)
        assert (
            scalar(engine, "SELECT status FROM graph_runs WHERE session_id = :i", i=sid)
            == "running"
        )
    with engine.begin() as conn:  # the process is gone; its lease runs out
        conn.execute(sa.text("UPDATE graph_runs SET lease_expires_at = now() - interval '1 s'"))
    assert run_purge(engine).phase1 == [sid]
    assert checkpoint_rows(engine, th) == {t: 0 for t in TABLES}
    assert scalar(engine, "SELECT owner_id FROM graph_runs WHERE session_id = :i", i=sid) is None


def test_a_held_lock_skips_the_session_without_waiting(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with api(SchemaLLM()) as client:
        sid = paused(client)
    age(engine, sid, expired_days=0.01)
    th = threads(engine, sid)
    rows = checkpoint_rows(engine, th)
    monkeypatch.setattr(retention, "LOCK_TIMEOUT", "200ms")
    with engine.connect() as holder:
        tx = holder.begin()  # e.g. a heartbeat renewing the lease right now
        holder.execute(
            sa.text("SELECT 1 FROM graph_runs WHERE session_id = :i FOR UPDATE"), {"i": sid}
        )
        report = run_purge(engine)
        tx.rollback()
    assert [(s.session_id, s.reason) for s in report.skipped] == [(sid, "locked")]
    assert checkpoint_rows(engine, th) == rows
    assert run_purge(engine).phase1 == [sid]  # retried once the lock is gone


def test_state_changed_since_planning_is_skipped_with_a_reason(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with api(SchemaLLM()) as client:
        active = paused(client)
    gone = str(uuid.uuid4())
    monkeypatch.setattr(retention, "_candidates", lambda db, tables, days: ([active, gone], []))
    report = run_purge(engine)
    assert sorted((s.session_id, s.reason) for s in report.skipped) == sorted(
        [(active, "not_expired"), (gone, "gone")]
    )
    assert all(checkpoint_rows(engine, threads(engine, active)).values())


def test_residue_from_a_stale_writer_is_reported_and_removed(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        sid = paused(client)
    age(engine, sid, expired_days=0.01)
    assert run_purge(engine).phase1 == [sid]
    th = threads(engine, sid)[0]
    insert_checkpoint_data(engine, th, TABLES)  # written after phase 1
    found = check(engine)
    assert found.problems and found.residue == {sid: 3}
    assert run_purge(engine).phase1 == [sid]
    assert not check(engine).problems


# --- orphans, dry run, idempotency TTL --------------------------------------------------------


def test_orphans_in_any_checkpoint_table_are_detected_and_removed(
    engine: sa.Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    full, writes_only, blobs_only = (str(uuid.uuid4()) for _ in range(3))
    insert_checkpoint_data(engine, full, TABLES)
    insert_checkpoint_data(engine, writes_only, ["checkpoint_writes"])
    insert_checkpoint_data(engine, blobs_only, ["checkpoint_blobs"])
    with api(SchemaLLM()) as client:
        sid = paused(client)  # a real thread: never an orphan
    before = checkpoint_rows(engine, threads(engine, sid))
    found = check(engine)
    assert found.orphans == {
        full: {t: 1 for t in TABLES},
        writes_only: {"checkpoint_writes": 1},
        blobs_only: {"checkpoint_blobs": 1},
    }
    assert cli.main(["check"]) == 1
    report = run_purge(engine)
    assert sorted(report.orphan_threads) == sorted([full, writes_only, blobs_only])
    assert checkpoint_rows(engine, [full, writes_only, blobs_only]) == {t: 0 for t in TABLES}
    assert checkpoint_rows(engine, threads(engine, sid)) == before
    capsys.readouterr()
    assert cli.main(["check"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_dry_run_writes_nothing(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        p1 = paused(client)
        p2, _ = completed(client)
    age(engine, p1, expired_days=0.01)
    age(engine, p2, expired_days=40)
    orphan = str(uuid.uuid4())
    insert_checkpoint_data(engine, orphan, TABLES)
    before = all_db_bytes(engine)
    report = run_purge(engine, dry_run=True)
    assert all_db_bytes(engine) == before
    assert (report.phase1, report.phase2, report.orphan_threads) == ([p1], [p2], [orphan])
    assert cli.main(["purge", "--dry-run"]) == 0 and all_db_bytes(engine) == before


def test_idempotency_ttl_prunes_only_old_rows(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        sid = paused(client)
        clar = client.get(f"/api/v1/sessions/{sid}").json()["workflow"]["clarification"]
        post(
            client,
            f"/sessions/{sid}/resume",
            {"checkpoint_id": clar["checkpoint_id"], "answers": ANSWER},
            "r",
        )
        wait_for(client, sid, "completed")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE api_idempotency SET created_at = now() - interval '25 hours' "
                "WHERE session_id = :i AND key = 'm'"
            ),
            {"i": sid},
        )
    report = run_purge(engine)
    assert report.idempotency_pruned == 1
    with engine.connect() as conn:
        left = sorted(
            conn.execute(
                sa.text("SELECT key FROM api_idempotency WHERE session_id = :i"), {"i": sid}
            ).scalars()
        )
    assert left == ["r"]
