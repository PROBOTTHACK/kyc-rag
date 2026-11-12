"""M3 S6: the workflow API end to end (real app, real Postgres checkpointer and tables).

Includes the mandatory resume-security checks: a PII-bearing resume is rejected before
anything is stored, and a scan of every checkpoint table, the workflow tables, Redis and
the logs then finds no trace of it.
"""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import redis
import sqlalchemy as sa
from fastapi.testclient import TestClient

from alembic import command
from app.config import get_settings
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.workflow_harness import (
    LLP_QUESTION,
    PAN,
    SchemaLLM,
    all_db_bytes,
    api,
    enable_app_logging,
    new_session,
    wait_for,
)
from tests.unit.test_screen import aadhaar

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(temp_db: str) -> Iterator[sa.Engine]:
    command.upgrade(alembic_cfg(), "head")
    enable_app_logging()  # Alembic's fileConfig disabled the app loggers
    eng = sync_engine(temp_db)
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: sa.Engine) -> Iterator[TestClient]:
    with api(SchemaLLM()) as c:
        yield c


def post(client: TestClient, path: str, body: Any, key: str | None = "k1") -> Any:
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/v1{path}", content=json.dumps(body), headers=headers)


def paused(client: TestClient) -> tuple[str, dict[str, Any]]:
    sid = new_session(client)
    r = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, key="m1")
    assert r.status_code == 202, r.text
    view = wait_for(client, sid, "awaiting_clarification")
    return sid, view["workflow"]["clarification"]


# --- the happy path ----------------------------------------------------------------------------


def test_message_pause_resume_report(client: TestClient) -> None:
    sid, clar = paused(client)
    assert [q["field"] for q in clar["questions"]] == ["account_type"]
    assert clar["round"] == 1 and clar["checkpoint_id"]
    assert LLP_QUESTION not in json.dumps(client.get(f"/api/v1/sessions/{sid}").json())

    body = {"checkpoint_id": clar["checkpoint_id"], "answers": {"account_type": "CURRENT_ACCOUNT"}}
    r = post(client, f"/sessions/{sid}/resume", body, key="r1")
    assert r.status_code == 202 and r.json()["status"] == "running"
    wf = wait_for(client, sid, "completed")["workflow"]
    # M4 S5: NLI disabled, so the claims are not verified and the run is DEGRADED
    assert wf["outcome"] == "DEGRADED" and wf["report_id"]
    seqs = [e["seq"] for e in wf["node_events"]]
    assert seqs == list(range(1, len(seqs) + 1)) and wf["node_events"][-1]["node"] == "finalize"

    report = client.get(f"/api/v1/reports/{wf['report_id']}").json()
    assert report["human_review_required"] is True
    assert [i["applicability"] for i in report["checklist"]] == ["CONDITIONAL"]  # group scope
    assert client.get("/api/v1/reports/00000000-0000-0000-0000-000000000000").status_code == 404


# --- resume security (mandatory) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "answers_or_body",
    [
        {"answers": {"account_type": PAN}},
        {"answers": {PAN: "CURRENT_ACCOUNT"}},
        {"answers": {"account_type": "CURRENT_ACCOUNT"}, "note": f"my PAN is {PAN}"},
        {"answers": {"account_type": int(aadhaar())}},
    ],
)
def test_pii_resume_is_rejected_and_leaves_no_trace(
    client: TestClient,
    engine: sa.Engine,
    caplog: pytest.LogCaptureFixture,
    answers_or_body: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG)
    sid, clar = paused(client)
    before = all_db_bytes(engine)
    body = {"checkpoint_id": clar["checkpoint_id"], **answers_or_body}
    r = post(client, f"/sessions/{sid}/resume", body, key="pii")
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "SENSITIVE_DATA_DETECTED" and error["categories"]
    secrets = [PAN.encode(), aadhaar().encode()]
    assert all(s not in r.content for s in secrets)
    after = all_db_bytes(engine)
    assert after == before and all(s not in after for s in secrets)  # nothing written at all
    rd = redis.Redis.from_url(get_settings().REDIS_URL.get_secret_value())
    for key in rd.scan_iter("*"):
        value = rd.dump(key) or b""
        assert all(s not in key and s not in value for s in secrets)
    assert PAN not in caplog.text and aadhaar() not in caplog.text
    still = client.get(f"/api/v1/sessions/{sid}").json()["workflow"]
    assert still["status"] == "awaiting_clarification"
    assert still["clarification"]["checkpoint_id"] == clar["checkpoint_id"]


def test_unasked_fields_and_stale_checkpoints_are_rejected_without_writes(
    client: TestClient, engine: sa.Engine
) -> None:
    sid, clar = paused(client)
    before = all_db_bytes(engine)
    unasked = {
        "checkpoint_id": clar["checkpoint_id"],
        "answers": {"account_type": "CURRENT_ACCOUNT", "residency": "NRI"},
    }
    r = post(client, f"/sessions/{sid}/resume", unasked, key="a")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_ANSWER")
    not_current = "00000000-0000-0000-0000-000000000000"  # well-formed, not the current one
    stale = {"checkpoint_id": not_current, "answers": {"account_type": "CURRENT_ACCOUNT"}}
    r = post(client, f"/sessions/{sid}/resume", stale, key="b")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "STALE_CHECKPOINT")
    bad_value = {"checkpoint_id": clar["checkpoint_id"], "answers": {"account_type": "GOLD"}}
    r = post(client, f"/sessions/{sid}/resume", bad_value, key="c")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_ANSWER")
    assert all_db_bytes(engine) == before


def test_validation_errors_never_echo_input(client: TestClient) -> None:
    sid, _ = paused(client)
    r = post(client, f"/sessions/{sid}/resume", {"checkpoint_id": 5, "answers": "SECRET-WORD"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert b"SECRET-WORD" not in r.content
    r = client.post(
        f"/api/v1/sessions/{sid}/resume",
        content=b"{not json SECRET-WORD",
        headers={"Idempotency-Key": "x"},
    )
    assert r.status_code == 422 and b"SECRET-WORD" not in r.content


def test_pii_in_a_new_message_is_rejected_and_nothing_starts(
    client: TestClient, engine: sa.Engine
) -> None:
    sid = new_session(client)
    before = all_db_bytes(engine)
    r = post(client, f"/sessions/{sid}/messages", {"message": f"LLP with PAN {PAN}"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "SENSITIVE_DATA_DETECTED"
    assert all_db_bytes(engine) == before
    assert client.get(f"/api/v1/sessions/{sid}").json()["workflow"] is None


# --- idempotency, concurrency, lifecycle ----------------------------------------------------------


def test_idempotency(client: TestClient, engine: sa.Engine) -> None:
    sid = new_session(client)
    first = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, key="same")
    again = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, key="same")
    assert first.status_code == again.status_code == 202 and first.json() == again.json()
    other = post(client, f"/sessions/{sid}/messages", {"message": "Another question?"}, key="same")
    assert (other.status_code, other.json()["error"]["code"]) == (409, "IDEMPOTENCY_MISMATCH")
    missing = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, key=None)
    assert (missing.status_code, missing.json()["error"]["code"]) == (
        400,
        "IDEMPOTENCY_KEY_REQUIRED",
    )
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM graph_runs")).scalar() == 1
    busy = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, key="new")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "RUN_IN_PROGRESS")


def test_two_concurrent_resumes_exactly_one_wins(client: TestClient) -> None:
    sid, clar = paused(client)
    body = {"checkpoint_id": clar["checkpoint_id"], "answers": {"account_type": "CURRENT_ACCOUNT"}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda k: post(client, f"/sessions/{sid}/resume", body, key=k), ["c1", "c2"])
        )
    assert sorted(r.status_code for r in results) == [202, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"]["code"] in (
        "RUN_IN_PROGRESS",
        "NOT_AWAITING_CLARIFICATION",
        "STALE_CHECKPOINT",
    )
    wait_for(client, sid, "completed")


def test_expired_sessions_reject_every_write(client: TestClient, engine: sa.Engine) -> None:
    sid = new_session(client)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE kyc_sessions SET created_at = now() - interval '2 hours', "
                "expires_at = now() - interval '1 hour'"
            )
        )
    for path, body in (
        (f"/sessions/{sid}/messages", {"message": LLP_QUESTION}),
        (f"/sessions/{sid}/resume", {"checkpoint_id": "x", "answers": {}}),
        (f"/sessions/{sid}/retry", {}),
    ):
        r = post(client, path, body)
        assert (r.status_code, r.json()["error"]["code"]) == (410, "SESSION_EXPIRED")
    assert client.get(f"/api/v1/sessions/{sid}").json()["status"] == "expired"


def test_a_second_question_gets_a_fresh_thread(client: TestClient) -> None:
    sid, clar = paused(client)
    body = {"checkpoint_id": clar["checkpoint_id"], "answers": {"account_type": "CURRENT_ACCOUNT"}}
    post(client, f"/sessions/{sid}/resume", body, key="r1")
    first = wait_for(client, sid, "completed")["workflow"]
    r = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, key="m2")
    assert r.status_code == 202
    second = wait_for(client, sid, "awaiting_clarification")["workflow"]
    assert second["thread_id"] != first["thread_id"]
    assert second["node_events"][0]["seq"] == 1  # a new run, nothing carried over
    assert second["clarification"]["round"] == 1  # the first question's rounds did not carry


def test_unknown_session_and_retry_with_nothing_to_retry(client: TestClient) -> None:
    assert client.get("/api/v1/sessions/not-a-uuid").status_code == 404
    sid = new_session(client)
    r = post(client, f"/sessions/{sid}/retry", {})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "NOTHING_TO_RETRY")
