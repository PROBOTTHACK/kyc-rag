"""Workflow progress events: the model and the best-effort Redis publisher (M3 S7).

Events are NOTIFICATIONS, never state (M3_PLAN §9). PostgreSQL holds the durable record
(`graph_runs.node_events`, `graph_runs.status`); the SSE stream always reads what it sends
from PostgreSQL and uses Redis only to wake up early (decision S7-D1).

- `ProgressEvent` carries only kind, run id, sequence, node, status and timestamp
  (decision S7-D2): never question text, answers, evidence, report content or user data.
  `extra="forbid"` makes any other field a programming error.
- `kind="node"`: a node completion from `node_events` (durable, replayable).
- `kind="run"`: the run's CURRENT status from `graph_runs.status` (decision S7-D3). It is
  a current-status notification, not a durable history: a client that reconnects after
  several transitions receives only the latest status.
- `RedisEventPublisher.publish` never raises and never blocks the workflow for long: one
  PUBLISH with a short timeout; failures are counted and logged as the class name only.
  Channel `chan:session:{session_id}` (docs/08) stores nothing.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class ProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["node", "run"]
    run_id: str
    seq: int | None = None
    node: str | None = None
    status: str
    at: str


def channel(session_id: str) -> str:
    return f"chan:session:{session_id}"


class Publisher(Protocol):
    async def publish(self, session_id: str, event: ProgressEvent) -> None: ...


class NullPublisher:
    async def publish(self, session_id: str, event: ProgressEvent) -> None:
        return None


@dataclass
class PublisherStats:
    published: int = 0
    dropped: int = 0


@dataclass
class RedisEventPublisher:
    client: Any  # redis.asyncio.Redis (short timeouts) or a test double
    timeout_s: float = 0.25
    stats: PublisherStats = field(default_factory=PublisherStats)

    async def publish(self, session_id: str, event: ProgressEvent) -> None:
        try:
            await asyncio.wait_for(
                self.client.publish(channel(session_id), event.model_dump_json()),
                timeout=self.timeout_s,
            )
            self.stats.published += 1
        except (RedisError, OSError, TimeoutError) as exc:
            self.stats.dropped += 1
            log.warning("progress event not published: %s", type(exc).__name__)
