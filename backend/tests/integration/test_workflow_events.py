"""M3 S7: progress events over SSE against the real app, Postgres and Redis (`make up`).

What is sent always comes from PostgreSQL (S7-D1); Redis only wakes the stream up. These
tests cover the approved S7 acceptance: event order and gap-free seq, payload discipline,
Redis down (the run and the stream are unaffected; polling delivers everything),
reconnect with `Last-Event-ID` (no duplicates; malformed and unknown cursors restart),
a crash after the `node_events` write and before the publish, recovered completions on
retry (S7-D5), subscription release on disconnect, idle timeout and keep-alive, a slow
subscriber, the stream opened before the first message, and an unknown session.

`TestClient` returns a streamed body only once the stream ends, so a full-flow stream runs
in a thread while the main thread drives the workflow; the open-subscription checks call
the service's stream generator directly through the app's event-loop portal.
"""

import json
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import redis
import redis.asyncio as aioredis
import sqlalchemy as sa
from fastapi.testclient import TestClient

from alembic import command
from app.config import get_settings
from app.core.events import ProgressEvent, PublisherStats, RedisEventPublisher, channel
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.workflow_harness import (
    LLP_QUESTION,
    SchemaLLM,
    api,
    enable_app_logging,
    new_session,
    wait_for,
    wait_for_node,
)

pytestmark = pytest.mark.integration
CLOSED_PORT = "redis://127.0.0.1:1/0"
KEYS = {"kind", "run_id", "seq", "node", "status", "at"}
ANSWER = {"account_type": "CURRENT_ACCOUNT"}


@pytest.fixture
def engine(temp_db: str) -> Iterator[sa.Engine]:
    command.upgrade(alembic_cfg(), "head")
    enable_app_logging()
    eng = sync_engine(temp_db)
    yield eng
    eng.dispose()


def redis_url() -> str:
    return get_settings().REDIS_URL.get_secret_value()


def redis_wiring(url: str | None = None) -> tuple[RedisEventPublisher, Any]:
    """The production wiring (runtime.build_runtime) for a given Redis URL."""
    url = url or redis_url()
    pub = aioredis.Redis.from_url(
        url, socket_timeout=0.25, socket_connect_timeout=0.25, retry_on_timeout=False
    )
    sub = aioredis.Redis.from_url(url, socket_connect_timeout=0.25)
    return RedisEventPublisher(pub, timeout_s=0.25), (lambda: sub)


def post(client: TestClient, path: str, body: Any, key: str) -> Any:
    return client.post(f"/api/v1{path}", content=json.dumps(body), headers={"Idempotency-Key": key})


def read_stream(client: TestClient, sid: str, last_event_id: str | None = None) -> str:
    headers = {"Last-Event-ID": last_event_id} if last_event_id is not None else {}
    r = client.get(f"/api/v1/sessions/{sid}/events", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    return r.text


def parse(text: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(id, event, data) for every event; comments (keep-alives) are skipped."""
    out = []
    for block in text.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((fields["id"], fields["event"], json.loads(fields["data"])))
    return out


def drive_to_completion(client: TestClient, sid: str) -> dict[str, Any]:
    post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
    clar = wait_for(client, sid, "awaiting_clarification")["workflow"]["clarification"]
    body = {"checkpoint_id": clar["checkpoint_id"], "answers": ANSWER}
    assert post(client, f"/sessions/{sid}/resume", body, "r").status_code == 202
    return dict(wait_for(client, sid, "completed")["workflow"])


def stream_while_driving(client: TestClient) -> tuple[str, dict[str, Any], float]:
    """Open the stream BEFORE the first message, then run the whole workflow."""
    sid = new_session(client)
    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(read_stream, client, sid)
        time.sleep(0.3)  # the stream is open and subscribed; no run exists yet
        started = time.monotonic()
        wf = drive_to_completion(client, sid)
        text = fut.result(timeout=60)
    return text, wf, time.monotonic() - started


def assert_matches_postgres(events: list[Any], wf: dict[str, Any]) -> None:
    nodes = [(d["seq"], d["node"], d["status"]) for _, kind, d in events if kind == "node"]
    durable = [(e["seq"], e["node"], e["status"]) for e in wf["node_events"]]
    assert nodes == durable  # order = node order, every seq once, gap-free
    assert [s for s, _, _ in nodes] == list(range(1, len(nodes) + 1))
    assert len({i for i, _, _ in events}) == len(events)  # ids unambiguous
    statuses = [d["status"] for _, kind, d in events if kind == "run"]
    assert statuses[-1] == "completed"
    assert all(a != b for a, b in zip(statuses, statuses[1:], strict=False))
    assert set(statuses) <= {"running", "awaiting_clarification", "completed"}


# --- order, payload, stream before the first message -------------------------------------------


def test_full_flow_streamed_from_before_the_first_message(engine: sa.Engine) -> None:
    publisher, subscriber = redis_wiring()
    with api(SchemaLLM(), publisher=publisher, subscriber=subscriber, poll_s=2.0) as client:
        text, wf, _ = stream_while_driving(client)
    events = parse(text)
    assert_matches_postgres(events, wf)
    # Redis woke the stream (poll_s=2.0 alone would not be this complete): every
    # transition was seen, including the pause for clarification
    statuses = [d["status"] for _, kind, d in events if kind == "run"]
    assert statuses == ["running", "awaiting_clarification", "running", "completed"]
    assert publisher.stats.published > 0 and publisher.stats.dropped == 0
    # payload discipline: only the approved keys; no question, answer or report content
    for _, _, data in events:
        assert set(data) == KEYS
        ProgressEvent.model_validate(data)
    for secret in (LLP_QUESTION, "CURRENT_ACCOUNT", "Partnership deed", wf["report_id"]):
        assert secret not in text


# --- Redis is optional ------------------------------------------------------------------------


def test_redis_down_changes_nothing_but_speed(engine: sa.Engine) -> None:
    publisher, subscriber = redis_wiring(CLOSED_PORT)
    with api(SchemaLLM(), publisher=publisher, subscriber=subscriber) as client:
        text, wf, _ = stream_while_driving(client)
        # M4 S5: NLI disabled -> DEGRADED by claim verification only; Redis adds nothing
        assert wf["outcome"] == "DEGRADED" and wf["report_id"]
        report = client.get(f"/api/v1/reports/{wf['report_id']}").json()
        assert [d["component"] for d in report["degradations"]] == ["verification"]
    assert_matches_postgres(parse(text), wf)  # every event, by PostgreSQL polling
    assert publisher.stats.dropped > 0 and publisher.stats.published == 0


# --- reconnect --------------------------------------------------------------------------------


def test_reconnect_with_last_event_id_has_no_gaps_or_duplicates(engine: sa.Engine) -> None:
    dropping = RedisEventPublisher(
        aioredis.Redis.from_url(CLOSED_PORT, socket_connect_timeout=0.1),
        timeout_s=0.1,
        stats=PublisherStats(),
    )
    with api(SchemaLLM(), publisher=dropping) as client:
        sid = new_session(client)
        drive_to_completion(client, sid)
        full = parse(read_stream(client, sid))
        assert full
        for n, (event_id, _, _) in enumerate(full):
            rest = parse(read_stream(client, sid, event_id))
            assert [i for i, _, _ in rest] == [i for i, _, _ in full[n + 1 :]], event_id
        # malformed and unknown cursors restart from the beginning of the session
        for bad in ("garbage", "1.2.3.4", f"{uuid.uuid4()}.3", f"{full[0][0]}x", ""):
            assert parse(read_stream(client, sid, bad)) == full, bad
    assert dropping.stats.dropped > 0  # every publish was dropped; the snapshot filled in


def test_a_second_question_streams_after_the_first_run(engine: sa.Engine) -> None:
    with api(SchemaLLM()) as client:
        sid = new_session(client)
        drive_to_completion(client, sid)
        first = parse(read_stream(client, sid))
        last_id = first[-1][0]
        post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m2")
        wait_for(client, sid, "awaiting_clarification")
        body = client.get(f"/api/v1/sessions/{sid}").json()["workflow"]["clarification"]
        resume = {"checkpoint_id": body["checkpoint_id"], "answers": ANSWER}
        post(client, f"/sessions/{sid}/resume", resume, "r2")
        wait_for(client, sid, "completed")
        rest = parse(read_stream(client, sid, last_id))
        everything = parse(read_stream(client, sid))
    assert [i for i, _, _ in everything] == [i for i, _, _ in first + rest]
    assert {d["run_id"] for _, _, d in rest}.isdisjoint({d["run_id"] for _, _, d in first})
    assert len({i for i, _, _ in everything}) == len(everything)


# --- a crash between the durable write and the publish -----------------------------------------


class StuckPublisher:
    """Blocks forever when asked to publish one node: the process 'dies' right after the
    `node_events` row is committed and before the notification goes out."""

    def __init__(self, node: str) -> None:
        self.node = node

    async def publish(self, session_id: str, event: ProgressEvent) -> None:
        if event.node == self.node:
            import asyncio

            await asyncio.Event().wait()


def test_a_crash_before_the_publish_still_shows_the_node(engine: sa.Engine) -> None:
    with api(SchemaLLM(), publisher=StuckPublisher("entity_intake"), idle_s=1.0) as client:
        sid = new_session(client)
        post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        wait_for_node(client, sid, "entity_intake")
        events = parse(read_stream(client, sid))  # closes by the idle timeout
    nodes = [d["node"] for _, kind, d in events if kind == "node"]
    assert nodes == ["input_security", "entity_intake"]
    assert [d["status"] for _, kind, d in events if kind == "run"] == ["running"]


# --- recovered completions on retry (S7-D5) ---------------------------------------------------


def crash_mid_synthesis(engine: sa.Engine) -> str:
    """A run that was paused, resumed, then lost while synthesis was in flight."""
    with api(SchemaLLM(block_synthesis=True), lease_s=2.0) as first:
        sid = new_session(first)
        post(first, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        clar = wait_for(first, sid, "awaiting_clarification")["workflow"]["clarification"]
        body = {"checkpoint_id": clar["checkpoint_id"], "answers": ANSWER}
        post(first, f"/sessions/{sid}/resume", body, "r")
        wait_for_node(first, sid, "evidence_association")
    return sid


def drop_last_node_events(engine: sa.Engine, n: int) -> list[str]:
    """Simulate a crash after the checkpoint and before the `node_events` write."""
    with engine.begin() as conn:
        dropped = []
        for _ in range(n):
            node = conn.execute(
                sa.text(
                    "SELECT node_events -> -1 ->> 'node' FROM graph_runs "
                    "WHERE status = 'running'"
                )
            ).scalar_one()
            conn.execute(
                sa.text(
                    "UPDATE graph_runs SET node_events = node_events - -1 "
                    "WHERE status = 'running'"
                )
            )
            dropped.insert(0, str(node))
        conn.execute(
            sa.text(
                "UPDATE graph_runs SET lease_expires_at = now() - interval '1 second' "
                "WHERE status = 'running'"
            )
        )
    return dropped


def node_events(engine: sa.Engine) -> list[list[tuple[str, str]]]:
    with engine.connect() as conn:
        return [
            [(e["node"], e["status"]) for e in r[0]]
            for r in conn.execute(sa.text("SELECT node_events FROM graph_runs ORDER BY started_at"))
        ]


@pytest.mark.parametrize("lost", [0, 1, 2])
def test_retry_recovers_checkpointed_but_unrecorded_nodes(engine: sa.Engine, lost: int) -> None:
    sid = crash_mid_synthesis(engine)
    dropped = drop_last_node_events(engine, lost) if lost else []
    if not lost:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE graph_runs SET lease_expires_at = now() - interval '1 second' "
                    "WHERE status = 'running'"
                )
            )
    before = node_events(engine)[0]
    with api(SchemaLLM(), lease_s=2.0) as client:
        assert post(client, f"/sessions/{sid}/retry", {}, "t").status_code == 202
        wait_for(client, sid, "completed")
        events = parse(read_stream(client, sid))
    old, new = node_events(engine)
    assert old == before  # the abandoned run's record is never rewritten
    recovered = [(n, "recovered") for n in dropped]
    assert new[: len(recovered)] == recovered  # recovered first, in execution order
    assert new[len(recovered)][0] == "compliance_synthesis"  # then the resumed nodes
    assert [s for _, s in new[len(recovered) :]] == ["completed"] * (len(new) - len(recovered))
    # nothing already recorded is duplicated: across both runs every completion appears once
    every = [n for n, _ in old + new]
    assert every.count("evidence_association") == 1 and every.count("form_analysis") == 1
    streamed = [(d["node"], d["status"]) for _, kind, d in events if kind == "node"]
    assert streamed == old + new


# --- subscriptions: release on disconnect, slow subscribers ------------------------------------


def numsub(rd: redis.Redis, sid: str) -> int:
    return int(dict(rd.pubsub_numsub(channel(sid)))[channel(sid).encode()])  # type: ignore[arg-type]


def test_disconnect_releases_the_subscription(engine: sa.Engine) -> None:
    publisher, subscriber = redis_wiring()
    rd = redis.Redis.from_url(redis_url())
    with api(SchemaLLM(), publisher=publisher, subscriber=subscriber) as client:
        sid = new_session(client)
        post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        wait_for(client, sid, "awaiting_clarification")
        service = client.app.state.workflow  # type: ignore[attr-defined]
        gone = {"value": False}

        async def disconnected() -> bool:
            return gone["value"]

        async def open_and_read() -> Any:
            gen = await service.open_event_stream(sid, None, disconnected)
            first = await gen.__anext__()
            return gen, first

        gen, first = client.portal.call(open_and_read)
        assert first.startswith("id: ")
        assert numsub(rd, sid) == 1  # subscribed while open (a waiting clarification)
        gone["value"] = True

        async def drain() -> list[str]:
            return [chunk async for chunk in gen]

        client.portal.call(drain)  # the stream notices the disconnect and ends
        assert numsub(rd, sid) == 0

        # an abandoned response (the server closes the generator) also releases it
        gone["value"] = False
        gen, _ = client.portal.call(open_and_read)
        assert numsub(rd, sid) == 1
        client.portal.call(gen.aclose)
        assert numsub(rd, sid) == 0
    rd.close()


def test_a_slow_subscriber_never_blocks_the_workflow(engine: sa.Engine) -> None:
    publisher, subscriber = redis_wiring()
    rd = redis.Redis.from_url(redis_url())
    with api(SchemaLLM(), publisher=publisher, subscriber=subscriber) as client:
        sid = new_session(client)
        stalled = rd.pubsub()
        stalled.subscribe(channel(sid))  # subscribed and never reads
        service = client.app.state.workflow  # type: ignore[attr-defined]

        async def never() -> bool:
            return False

        async def open_unread() -> Any:
            gen = await service.open_event_stream(sid, None, never)
            await gen.__anext__()  # stays open, then is never read again
            return gen

        post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m")
        wait_for(client, sid, "awaiting_clarification")
        unread = client.portal.call(open_unread)
        started = time.monotonic()
        clar = client.get(f"/api/v1/sessions/{sid}").json()["workflow"]["clarification"]
        body = {"checkpoint_id": clar["checkpoint_id"], "answers": ANSWER}
        post(client, f"/sessions/{sid}/resume", body, "r")
        wf = wait_for(client, sid, "completed")["workflow"]
        assert time.monotonic() - started < 15 and wf["report_id"]
        assert publisher.stats.dropped == 0
        client.portal.call(unread.aclose)
        stalled.close()
    rd.close()


# --- idle timeout, keep-alive, unknown session -------------------------------------------------


def test_idle_timeout_and_keep_alive(engine: sa.Engine) -> None:
    with api(SchemaLLM(), keepalive_s=0.1, idle_s=0.6) as client:
        sid = new_session(client)
        started = time.monotonic()
        text = read_stream(client, sid)  # no message yet: stays open until idle
        elapsed = time.monotonic() - started
    assert 0.5 <= elapsed < 10
    assert parse(text) == [] and text.count(": keep-alive\n\n") >= 2


@pytest.mark.parametrize("sid", [str(uuid.uuid4()), "not-a-uuid"])
def test_unknown_session_is_404_with_codes_only(engine: sa.Engine, sid: str) -> None:
    with api(SchemaLLM()) as client:
        r = client.get(f"/api/v1/sessions/{sid}/events")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert sid not in r.text
