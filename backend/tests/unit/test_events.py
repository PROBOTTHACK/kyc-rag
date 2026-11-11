"""M3 S7: progress events, the SSE stream logic and the recovered-node reconciliation.

Pure logic with fakes (no Redis, no Postgres): the event model's key set, the
never-raising publisher, `Last-Event-ID` parsing (malformed and unknown cursors), the
PostgreSQL-snapshot diff (order, unambiguous ids across runs, current-status collapse),
checkpoint-history reconciliation, generator cleanup on every exit path, bounded
draining, and the checkpoint-id screening regression found during S7.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.events import ProgressEvent, RedisEventPublisher, channel
from app.workflow.service import WorkflowError, _screen_raw, _without_checkpoint_id
from app.workflow.stream import (
    MAX_DRAIN,
    Cursor,
    RunView,
    caught_up_and_terminal,
    completed_nodes,
    diff,
    missing_completions,
    parse_last_event_id,
    session_event_stream,
    sse,
)

R1 = "11111111-1111-4111-8111-111111111111"
R2 = "22222222-2222-4222-8222-222222222222"
AT = "2026-11-12T00:00:00+00:00"
KEYS = {"kind", "run_id", "seq", "node", "status", "at"}


def ev(seq: int, node: str, status: str = "completed") -> dict[str, Any]:
    return {"seq": seq, "node": node, "status": status, "at": AT}


def run(rid: str, status: str, *nodes: str) -> RunView:
    return RunView(rid, status, [ev(i + 1, n) for i, n in enumerate(nodes)], AT)


def ids(items: Any) -> list[str]:
    return [i.event_id for i in items]


# --- the event model and the publisher -------------------------------------------------------


def test_progress_event_has_exactly_the_approved_keys() -> None:
    e = ProgressEvent(kind="node", run_id=R1, seq=1, node="input_security", status="ok", at=AT)
    assert set(json.loads(e.model_dump_json())) == KEYS
    with pytest.raises(ValidationError):
        ProgressEvent(kind="node", run_id=R1, status="ok", at=AT, question="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ProgressEvent(kind="report", run_id=R1, status="ok", at=AT)  # type: ignore[arg-type]
    assert channel("abc") == "chan:session:abc"


class Client:
    def __init__(self, exc: BaseException | None = None, delay: float = 0.0) -> None:
        self.exc, self.delay, self.sent = exc, delay, []  # type: list[tuple[str, str]]

    async def publish(self, chan: str, payload: str) -> int:
        await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        self.sent.append((chan, payload))
        return 1


@pytest.mark.parametrize(
    "client",
    [Client(RedisConnectionError("down")), Client(OSError("refused")), Client(delay=5.0)],
    ids=["redis-error", "os-error", "timeout"],
)
def test_the_publisher_never_raises(client: Client) -> None:
    pub = RedisEventPublisher(client, timeout_s=0.05)
    e = ProgressEvent(kind="run", run_id=R1, status="running", at=AT)
    asyncio.run(pub.publish("s", e))
    assert (pub.stats.published, pub.stats.dropped) == (0, 1)


def test_the_publisher_sends_the_event_json_on_the_session_channel() -> None:
    client = Client()
    pub = RedisEventPublisher(client)
    e = ProgressEvent(kind="run", run_id=R1, status="running", at=AT)
    asyncio.run(pub.publish("s1", e))
    assert client.sent == [("chan:session:s1", e.model_dump_json())]
    assert pub.stats.published == 1


# --- Last-Event-ID ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "1",
        f"{R1}",
        f"{R1}.x",
        f"{R1}.-1",
        f"{R1}.1.a.b",
        "not-a-uuid.3",
        f"{R1}.1.2.3",
        " . ",
    ],
)
def test_malformed_cursors_restart_from_the_beginning(value: str | None) -> None:
    assert parse_last_event_id(value) == Cursor()


def test_cursor_parsing() -> None:
    assert parse_last_event_id(f"{R1}.3") == Cursor(R1, 3, {})
    assert parse_last_event_id(f"{R1}.3.completed") == Cursor(R1, 3, {R1: "completed"})
    upper = R1.upper()
    assert parse_last_event_id(f"{upper}.0").run_id == R1  # normalized


# --- the snapshot diff ---------------------------------------------------------------------------


def test_diff_orders_status_then_nodes_then_final_status() -> None:
    items, cur = diff([run(R1, "completed", "a", "b")], Cursor())
    assert ids(items) == [f"{R1}.1", f"{R1}.2", f"{R1}.2.completed"]
    assert [i.event.kind for i in items] == ["node", "node", "run"]
    assert caught_up_and_terminal([run(R1, "completed", "a", "b")], cur)


def test_a_running_status_comes_before_the_nodes() -> None:
    items, cur = diff([run(R1, "running", "a")], Cursor())
    assert ids(items) == [f"{R1}.0.running", f"{R1}.1"]
    assert diff([run(R1, "running", "a")], cur)[0] == []  # nothing new: nothing sent
    items, cur = diff([run(R1, "running", "a", "b")], cur)
    assert ids(items) == [f"{R1}.2"]
    items, _ = diff([run(R1, "awaiting_clarification", "a", "b")], cur)
    assert ids(items) == [f"{R1}.2.awaiting_clarification"]


def test_ids_are_unique_across_runs_and_resume_mid_session() -> None:
    runs = [run(R1, "awaiting_clarification", "a", "b"), run(R2, "completed", "a", "c")]
    items, _ = diff(runs, Cursor())
    assert len(set(ids(items))) == len(items)
    assert ids(items) == [
        f"{R1}.1",
        f"{R1}.2",
        f"{R1}.2.awaiting_clarification",
        f"{R2}.1",
        f"{R2}.2",
        f"{R2}.2.completed",
    ]
    # reconnecting after every id yields exactly the remainder: no gaps, no duplicates
    for n, item in enumerate(items):
        rest, _ = diff(runs, parse_last_event_id(item.event_id))
        assert ids(rest) == ids(items)[n + 1 :], item.event_id


def test_an_unknown_run_restarts_from_the_beginning() -> None:
    runs = [run(R1, "completed", "a")]
    unknown = str(uuid.uuid4())
    assert ids(diff(runs, parse_last_event_id(f"{unknown}.5"))[0]) == ids(diff(runs, Cursor())[0])


def test_run_status_is_current_status_only() -> None:
    """S7-D3: a client reconnecting after several transitions sees only the latest."""
    cur = parse_last_event_id(f"{R1}.1")  # it saw node 1 while the run was running
    items, _ = diff([run(R1, "completed", "a", "b")], cur)
    assert ids(items) == [f"{R1}.2", f"{R1}.2.completed"]  # no replayed `running`


def test_terminal_requires_the_latest_run_fully_sent() -> None:
    runs = [run(R1, "completed", "a")]
    assert not caught_up_and_terminal(runs, Cursor(R1, 1, {}))
    assert not caught_up_and_terminal(
        [run(R1, "awaiting_clarification", "a")], Cursor(R1, 1, {R1: "awaiting_clarification"})
    )
    assert not caught_up_and_terminal([], Cursor())
    for status in ("completed", "failed", "abandoned"):
        assert caught_up_and_terminal([run(R1, status, "a")], Cursor(R1, 1, {R1: status}))


def test_sse_wire_format() -> None:
    (item,) = diff([run(R1, "running")], Cursor())[0]
    text = sse(item)
    assert text.startswith(f"id: {R1}.0.running\nevent: run\ndata: ") and text.endswith("\n\n")
    assert set(json.loads(text.split("data: ", 1)[1])) == KEYS


# --- checkpoint-history reconciliation (S7-D5) -------------------------------------------------


def test_completed_nodes_from_history() -> None:
    history = [
        (("__start__",), "input"),
        (("input_security",), "loop"),
        (("entity_intake",), "loop"),
        (("clarify",), "loop"),  # interrupted: no later loop checkpoint for clarify
    ]
    assert completed_nodes(history) == ["input_security", "entity_intake"]
    assert completed_nodes([(("a",), "loop"), (("b",), "update")]) == []  # a manual update
    assert completed_nodes([]) == []


def test_a_checkpointed_but_unrecorded_node_is_missing() -> None:
    assert missing_completions(["a", "b", "c"], ["a", "b"]) == ["c"]


def test_already_recorded_nodes_are_never_duplicated() -> None:
    assert missing_completions(["a", "b", "c"], ["a", "b", "c"]) == []
    assert missing_completions([], []) == []


def test_multiple_previous_runs_and_repeated_nodes() -> None:
    """Recorded completions span earlier runs of the thread (first question, then the
    resumed run); repeated nodes after clarification count separately and in order."""
    checkpointed = [
        "input_security",
        "entity_intake",
        "clarify",
        "input_security",
        "entity_intake",
        "policy_retrieval",
        "form_analysis",
    ]
    run1 = ["input_security", "entity_intake"]  # clarify interrupted, not recorded
    run2 = ["clarify", "input_security", "entity_intake", "policy_retrieval"]
    assert missing_completions(checkpointed, run1 + run2) == ["form_analysis"]
    assert missing_completions(checkpointed, run1) == [
        "clarify",
        "input_security",
        "entity_intake",
        "policy_retrieval",
        "form_analysis",
    ]


# --- the stream generator --------------------------------------------------------------------


class FakePubSub:
    def __init__(self, messages: int = 0, fail_on_get: bool = False) -> None:
        self.messages, self.fail_on_get = messages, fail_on_get
        self.subscribed = self.closed = False
        self.gets = 0

    async def subscribe(self, chan: str) -> None:
        self.subscribed = True

    async def unsubscribe(self) -> None:
        self.subscribed = False

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(
        self, ignore_subscribe_messages: bool, timeout: float  # noqa: ASYNC109
    ) -> Any:
        self.gets += 1
        if self.fail_on_get:
            raise RedisConnectionError("lost")
        if self.messages:
            self.messages -= 1
            return {"type": "message"}
        await asyncio.sleep(min(timeout, 0.01))
        return None


class FakeRedis:
    def __init__(self, ps: FakePubSub) -> None:
        self.ps = ps

    def pubsub(self) -> FakePubSub:
        return self.ps


def stream(load: Any, ps: FakePubSub | None, *, disconnected: Any = None, **kw: Any) -> Any:
    async def never() -> bool:
        return False

    args: dict[str, Any] = dict(poll_s=0.01, keepalive_s=100.0, idle_timeout_s=100.0)
    args.update(kw)
    return session_event_stream(
        session_id="s",
        load=load,
        last_event_id=args.pop("last_event_id", None),
        is_disconnected=disconnected or never,
        subscriber=(lambda: FakeRedis(ps)) if ps is not None else None,
        **args,
    )


async def collect(gen: Any) -> list[str]:
    return [chunk async for chunk in gen]


def test_the_stream_subscribes_first_and_closes_on_the_terminal_event() -> None:
    ps = FakePubSub()
    loads: list[bool] = []

    async def load() -> list[RunView]:
        loads.append(ps.subscribed)  # subscribed before the first read
        return [run(R1, "completed", "a")]

    out = asyncio.run(collect(stream(load, ps)))
    assert loads == [True] and len(out) == 2 and ps.closed and not ps.subscribed


def test_the_stream_stays_open_before_the_first_run_and_through_clarification() -> None:
    ps = FakePubSub()
    states = [
        [],
        [run(R1, "awaiting_clarification", "a")],
        [run(R1, "awaiting_clarification", "a")],
        [run(R1, "awaiting_clarification", "a"), run(R2, "completed", "b")],
    ]

    async def load() -> list[RunView]:
        return states.pop(0) if len(states) > 1 else states[0]

    out = asyncio.run(collect(stream(load, ps)))
    assert [line.split("\n")[0] for line in out] == [
        f"id: {R1}.1",
        f"id: {R1}.1.awaiting_clarification",
        f"id: {R2}.1",
        f"id: {R2}.1.completed",
    ]
    assert ps.closed


@pytest.mark.parametrize("exit_path", ["disconnect", "idle", "exception", "cancel"])
def test_every_exit_path_releases_the_subscription(exit_path: str) -> None:
    ps = FakePubSub()

    async def load() -> list[RunView]:
        if exit_path == "exception":
            raise RuntimeError("db gone")
        return [run(R1, "running")]

    async def disconnected() -> bool:
        return exit_path == "disconnect"

    async def scenario() -> None:
        gen = stream(load, ps, disconnected=disconnected, idle_timeout_s=0.05)
        if exit_path == "exception":
            with pytest.raises(RuntimeError):
                await collect(gen)
        elif exit_path == "cancel":
            task = asyncio.create_task(collect(gen))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(collect(gen), 5.0)

    asyncio.run(scenario())
    assert ps.closed and not ps.subscribed


def test_an_early_client_close_releases_the_subscription() -> None:
    ps = FakePubSub()

    async def load() -> list[RunView]:
        return [run(R1, "running", "a")]

    async def scenario() -> None:
        gen = stream(load, ps)
        await gen.__anext__()
        await gen.aclose()  # what the server does when the response is abandoned

    asyncio.run(scenario())
    assert ps.closed and not ps.subscribed


def test_keep_alive_comments() -> None:
    n = 0

    async def load() -> list[RunView]:
        nonlocal n
        n += 1
        return [run(R1, "running")] if n < 12 else [run(R1, "completed")]

    out = asyncio.run(collect(stream(load, None, keepalive_s=0.0)))
    assert ": keep-alive\n\n" in out and out[-1].startswith(f"id: {R1}.0.completed")


def test_redis_errors_fall_back_to_polling() -> None:
    ps = FakePubSub(fail_on_get=True)
    n = 0

    async def load() -> list[RunView]:
        nonlocal n
        n += 1
        return [run(R1, "running")] if n < 3 else [run(R1, "completed", "a")]

    out = asyncio.run(collect(stream(load, ps)))
    assert out[-1].startswith(f"id: {R1}.1.completed") and ps.closed and ps.gets == 1


def test_an_unavailable_subscriber_polls() -> None:
    def broken() -> Any:
        raise OSError("refused")

    async def load() -> list[RunView]:
        return [run(R1, "completed", "a")]

    async def never() -> bool:
        return False

    gen = session_event_stream(
        session_id="s",
        load=load,
        last_event_id=None,
        is_disconnected=never,
        subscriber=broken,
        poll_s=0.01,
        keepalive_s=100.0,
        idle_timeout_s=100.0,
    )
    assert len(asyncio.run(collect(gen))) == 2


def test_draining_is_bounded() -> None:
    ps = FakePubSub(messages=10 * MAX_DRAIN)
    n = 0

    async def load() -> list[RunView]:
        nonlocal n
        n += 1
        return [run(R1, "running")] if n < 2 else [run(R1, "completed")]

    asyncio.run(collect(stream(load, ps)))
    assert ps.gets == MAX_DRAIN + 1  # one wait drains at most MAX_DRAIN, then reads again
    assert ps.messages == 10 * MAX_DRAIN - (MAX_DRAIN + 1)


def test_reconnect_with_last_event_id_sends_only_the_rest() -> None:
    async def load() -> list[RunView]:
        return [run(R1, "completed", "a", "b", "c")]

    out = asyncio.run(collect(stream(load, None, last_event_id=f"{R1}.2")))
    assert [o.split("\n")[0] for o in out] == [f"id: {R1}.3", f"id: {R1}.3.completed"]


# --- the checkpoint-id screening regression (found in S7) ------------------------------------


LUHN_UUID = "1f1de055-f966-6080-8002-022e095547bf"  # a real LangGraph-style id; Luhn-valid digits


def test_a_well_formed_checkpoint_id_is_exempt_from_pii_screening() -> None:
    body = {"checkpoint_id": LUHN_UUID, "answers": {"account_type": "CURRENT_ACCOUNT"}}
    with pytest.raises(WorkflowError):
        _screen_raw(body, 2000)  # the false positive that S7 surfaced
    _screen_raw(_without_checkpoint_id(body), 2000)  # no error


@pytest.mark.parametrize(
    "value", ["4111 1111 1111 1111", LUHN_UUID.upper(), LUHN_UUID + "x", 7, None, ""]
)
def test_a_malformed_checkpoint_id_is_rejected_with_codes_only(value: Any) -> None:
    with pytest.raises(WorkflowError) as err:
        _without_checkpoint_id({"checkpoint_id": value, "answers": {}})
    assert (err.value.status, err.value.code) == (422, "VALIDATION_ERROR")
    assert err.value.message == "The request body is not valid."


def test_other_fields_are_still_screened() -> None:
    body = {"checkpoint_id": LUHN_UUID, "answers": {"account_type": "4111 1111 1111 1111"}}
    with pytest.raises(WorkflowError):
        _screen_raw(_without_checkpoint_id(body), 2000)
