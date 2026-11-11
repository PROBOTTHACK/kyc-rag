"""Server-sent progress events for a session (M3 S7; M3_PLAN §9, §10).

What is sent always comes from PostgreSQL (decision S7-D1): the durable, gap-free
`graph_runs.node_events` of every run in the session, plus each run's CURRENT status from
`graph_runs.status` (S7-D3: a current-status notification, not a history; a client that
reconnects after several transitions receives only the latest status). Redis pub/sub is
only a wake-up: with Redis down or slow the stream polls PostgreSQL every
`EVENTS_POLL_S`, so delivery never depends on Redis.

Event ids (`Last-Event-ID`) are unambiguous across the runs of a session (one run per
question, S6-D1): `<run_id>.<seq>` for a node event and `<run_id>.<seq>.<status>` for a
run-status event. A malformed id, or one naming a run that is not in this session,
restarts from the beginning of the session. Runs are ordered by start time, then id.

Resources: the stream subscribes BEFORE its first read (nothing published in between is
lost), opens a short-lived database session per read (none is held while waiting), drains
at most `MAX_DRAIN` Redis messages per wake-up and discards their content (the
subscriber's buffer never grows with a slow client, and the workflow never waits on a
subscriber: PUBLISH does not block on readers). The `finally` block unsubscribes and
closes the pub/sub on completion, disconnect, idle timeout, cancellation or error.

Also here: `completed_nodes` / `missing_completions`, which let a retry recover node
completions that were checkpointed but never written to `node_events` (S7-D5).
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.events import ProgressEvent, channel

log = logging.getLogger(__name__)
TERMINAL = frozenset({"completed", "failed", "abandoned"})
MAX_DRAIN = 256
SUBSCRIBE_TIMEOUT_S = 1.0


# --- cursor ----------------------------------------------------------------------------------


@dataclass
class Cursor:
    run_id: str | None = None
    seq: int = 0
    statuses: dict[str, str] = field(default_factory=dict)  # last status sent per run


def parse_last_event_id(value: str | None) -> Cursor:
    """`<run_id>.<seq>` or `<run_id>.<seq>.<status>`; anything else -> from the start."""
    if not value:
        return Cursor()
    parts = value.strip().split(".")
    if len(parts) not in (2, 3):
        return Cursor()
    try:
        run_id = str(uuid.UUID(parts[0]))
        seq = int(parts[1])
    except ValueError:
        return Cursor()
    if seq < 0:
        return Cursor()
    cursor = Cursor(run_id=run_id, seq=seq)
    if len(parts) == 3 and parts[2]:
        cursor.statuses[run_id] = parts[2]
    return cursor


@dataclass(frozen=True)
class RunView:
    run_id: str
    status: str
    node_events: list[dict[str, Any]]
    status_at: str


@dataclass(frozen=True)
class Outgoing:
    event_id: str
    event: ProgressEvent


def diff(runs: Sequence[RunView], cursor: Cursor) -> tuple[list[Outgoing], Cursor]:
    """Everything after the cursor, in order: per run, a `running` status first, then its
    node events with seq above the cursor, then any other status change."""
    ids = [r.run_id for r in runs]
    if cursor.run_id is not None and cursor.run_id not in ids:
        cursor = Cursor()  # an unknown run: restart from the beginning of the session
    start = ids.index(cursor.run_id) if cursor.run_id is not None else 0
    out: list[Outgoing] = []
    cur = Cursor(cursor.run_id, cursor.seq, dict(cursor.statuses))
    for run in runs[start:]:
        after = cur.seq if run.run_id == cur.run_id else 0
        changed = cur.statuses.get(run.run_id) != run.status

        def status_event(seq: int, run: RunView = run) -> None:
            ev = ProgressEvent(kind="run", run_id=run.run_id, seq=None, node=None,
                               status=run.status, at=run.status_at)  # fmt: skip
            out.append(Outgoing(f"{run.run_id}.{seq}.{run.status}", ev))
            cur.run_id, cur.seq = run.run_id, seq
            cur.statuses[run.run_id] = run.status

        if changed and run.status == "running":
            status_event(after)
        last = after
        for e in sorted(run.node_events, key=lambda e: int(e["seq"])):
            seq = int(e["seq"])
            if seq <= after:
                continue
            ev = ProgressEvent(kind="node", run_id=run.run_id, seq=seq, node=str(e["node"]),
                               status=str(e["status"]), at=str(e["at"]))  # fmt: skip
            out.append(Outgoing(f"{run.run_id}.{seq}", ev))
            cur.run_id, cur.seq, last = run.run_id, seq, seq
        if changed and run.status != "running":
            status_event(last)
        elif run.run_id != cur.run_id:
            cur.run_id, cur.seq = run.run_id, last
    return out, cur


def caught_up_and_terminal(runs: Sequence[RunView], cursor: Cursor) -> bool:
    if not runs:
        return False
    last = runs[-1]
    max_seq = max((int(e["seq"]) for e in last.node_events), default=0)
    return (
        last.status in TERMINAL
        and cursor.run_id == last.run_id
        and cursor.seq >= max_seq
        and cursor.statuses.get(last.run_id) == last.status
    )


def sse(item: Outgoing) -> str:
    data = json.dumps(item.event.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {item.event_id}\nevent: {item.event.kind}\ndata: {data}\n\n"


# --- checkpoint-history reconciliation (S7-D5) ------------------------------------------------


def completed_nodes(history: Sequence[tuple[Sequence[str], str | None]]) -> list[str]:
    """Node completions in execution order from chronological checkpoint snapshots
    `(next, source)`: a node named in a snapshot's `next` completed when the following
    checkpoint was written by the graph loop (`source == "loop"`). Manual state updates
    and the input checkpoint's `__start__` are not node executions."""
    out: list[str] = []
    for (nxt, _), (_, later_source) in zip(history, history[1:], strict=False):
        if later_source != "loop":
            continue
        out += [n for n in nxt if not n.startswith("__")]
    return out


def missing_completions(checkpointed: Sequence[str], recorded: Sequence[str]) -> list[str]:
    """Checkpointed completions that no previous run recorded, in execution order.
    Recording follows execution, so `recorded` is matched in order against
    `checkpointed`; repeated nodes (e.g. intake after clarification) count separately."""
    missing: list[str] = []
    j = 0
    for node in checkpointed:
        if j < len(recorded) and recorded[j] == node:
            j += 1
        else:
            missing.append(node)
    return missing


# --- the stream ----------------------------------------------------------------------------


Loader = Callable[[], Awaitable[list[RunView]]]


async def _subscribe(factory: Callable[[], Any] | None, session_id: str) -> Any | None:
    if factory is None:
        return None
    pubsub = None
    try:
        pubsub = factory().pubsub()
        await asyncio.wait_for(pubsub.subscribe(channel(session_id)), SUBSCRIBE_TIMEOUT_S)
        return pubsub
    except Exception as exc:  # noqa: BLE001 - Redis is optional: fall back to polling
        log.info("progress stream without Redis (%s); polling PostgreSQL", type(exc).__name__)
        await _close(pubsub)
        return None


async def _close(pubsub: Any | None) -> None:
    if pubsub is None:
        return
    with contextlib.suppress(Exception):  # best effort; the connection is closed next
        await asyncio.wait_for(pubsub.unsubscribe(), SUBSCRIBE_TIMEOUT_S)
    with contextlib.suppress(Exception):  # nothing more can be released
        await pubsub.aclose()


async def _wait(pubsub: Any | None, poll_s: float) -> Any | None:
    """Wake on a Redis message (drained, content ignored) or after `poll_s`. Returns the
    pub/sub to keep using, or None after a Redis error (polling from then on)."""
    if pubsub is None:
        await asyncio.sleep(poll_s)
        return None
    try:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_s)
        drained = 0
        while msg is not None and drained < MAX_DRAIN:
            drained += 1
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
        return pubsub
    except Exception as exc:  # noqa: BLE001 - Redis dropped: keep streaming from Postgres
        log.info("progress stream lost Redis (%s); polling PostgreSQL", type(exc).__name__)
        await _close(pubsub)
        return None


async def session_event_stream(
    *,
    session_id: str,
    load: Loader,
    last_event_id: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    subscriber: Callable[[], Any] | None,
    poll_s: float,
    keepalive_s: float,
    idle_timeout_s: float,
) -> AsyncIterator[str]:
    cursor = parse_last_event_id(last_event_id)
    pubsub = await _subscribe(subscriber, session_id)  # before the first read
    try:
        last_event = last_ping = time.monotonic()
        while True:
            runs = await load()
            items, cursor = diff(runs, cursor)
            for item in items:
                yield sse(item)
            now = time.monotonic()
            if items:
                last_event = last_ping = now
            if caught_up_and_terminal(runs, cursor):
                return
            if await is_disconnected() or now - last_event >= idle_timeout_s:
                return
            if now - last_ping >= keepalive_s:
                yield ": keep-alive\n\n"
                last_ping = now
            pubsub = await _wait(pubsub, poll_s)
    finally:
        await _close(pubsub)
