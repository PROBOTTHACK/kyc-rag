"""Request + token rate limiting for LLM providers (M3 S1; docs/02, docs/08 `rl:*`).

Two limits per provider and minute: requests (RPM) and tokens (TPM). Groq's published
token budget is small (docs/02), so a call reserves its *estimated* tokens (prompt
estimate + the maximum output it may produce) before it is sent.

- `LocalRateLimiter`: a sliding 60 s window in this process.
- `RedisRateLimiter`: a fixed one-minute window shared by every process through Redis
  (`rl:{provider}:{window}`, a hash with `req` and `tok`, TTL 120 s). Redis is never a
  correctness dependency: on any Redis error or timeout it uses its in-process fallback
  window, counts the failure, and the call proceeds exactly as it otherwise would.

Both are bounded: a call that would wait longer than `max_wait_s` fails with a transient
`ProviderRateLimited` (so a fallback model may be tried), and a request whose own estimate
exceeds the per-minute token budget fails at once (non-transient: it can never fit).
Keys hold only the provider name and a window number: no prompt, no user data.
"""

import asyncio
import collections
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.llm.common import ProviderRateLimited, ProviderUnavailable

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
WINDOW_S = 60.0
KEY_TTL_S = 120
CHARS_PER_TOKEN = 3  # deliberately pessimistic (English text is ~4 chars per token)


class RateLimiter(Protocol):
    async def acquire(self, tokens: int) -> None: ...


def estimate_tokens(*texts: str, max_output_tokens: int) -> int:
    """A conservative token reservation: prompt characters / 3, plus the output cap."""
    chars = sum(len(t) for t in texts)
    return math.ceil(chars / CHARS_PER_TOKEN) + max_output_tokens


def _check_fits(tokens: int, tpm: int) -> None:
    if tokens <= 0:
        raise ValueError("tokens must be positive")
    if tokens > tpm:
        raise ProviderUnavailable(
            f"request needs ~{tokens} tokens, above the per-minute budget of {tpm}"
        )


def _too_long(waited: float, wait: float, max_wait_s: float) -> ProviderRateLimited:
    err = ProviderRateLimited(
        f"rate-limit capacity not available within {max_wait_s:.0f} s "
        f"(waited {waited:.0f} s, next slot in {wait:.0f} s)"
    )
    err.transient = True
    return err


@dataclass
class LocalRateLimiter:
    """Sliding 60-second window over (time, tokens) in this process."""

    rpm: int
    tpm: int
    max_wait_s: float = 120.0
    clock: Clock = time.monotonic
    sleep: Sleep = asyncio.sleep
    _events: collections.deque[tuple[float, int]] = field(default_factory=collections.deque)

    def __post_init__(self) -> None:
        if self.rpm <= 0 or self.tpm <= 0 or self.max_wait_s <= 0:
            raise ValueError("rpm, tpm and max_wait_s must be positive")

    async def acquire(self, tokens: int) -> None:
        _check_fits(tokens, self.tpm)
        waited = 0.0
        while True:
            now = self.clock()
            while self._events and now - self._events[0][0] >= WINDOW_S:
                self._events.popleft()
            used = sum(t for _, t in self._events)
            if len(self._events) < self.rpm and used + tokens <= self.tpm:
                self._events.append((now, tokens))
                return
            wait = max(0.001, WINDOW_S - (now - self._events[0][0]))
            if waited + wait > self.max_wait_s:
                raise _too_long(waited, wait, self.max_wait_s)
            await self.sleep(wait)
            waited += wait


@dataclass
class RedisRateLimiter:
    """Fixed one-minute window shared through Redis, with an in-process fallback."""

    client: Any  # redis.asyncio.Redis, or a test double with the same pipeline API
    provider: str
    rpm: int
    tpm: int
    fallback: LocalRateLimiter
    max_wait_s: float = 120.0
    op_timeout_s: float = 0.25
    clock: Clock = time.time  # wall clock: windows must agree across processes
    sleep: Sleep = asyncio.sleep
    redis_failures: int = 0
    fallback_acquisitions: int = 0

    def __post_init__(self) -> None:
        if self.rpm <= 0 or self.tpm <= 0 or self.max_wait_s <= 0:
            raise ValueError("rpm, tpm and max_wait_s must be positive")

    def key(self, window: int) -> str:
        return f"rl:{self.provider}:{window}"

    async def acquire(self, tokens: int) -> None:
        _check_fits(tokens, self.tpm)
        waited = 0.0
        while True:
            now = self.clock()
            window = int(now // WINDOW_S)
            try:
                granted = await asyncio.wait_for(
                    self._try_reserve(window, tokens), timeout=self.op_timeout_s
                )
            except (RedisError, OSError, TimeoutError) as exc:
                self.redis_failures += 1
                self.fallback_acquisitions += 1
                log.warning("rate limiter: Redis unavailable (%s); local window used",
                            type(exc).__name__)  # fmt: skip
                await self.fallback.acquire(tokens)
                return
            if granted:
                return
            wait = max(0.001, (window + 1) * WINDOW_S - now)
            if waited + wait > self.max_wait_s:
                raise _too_long(waited, wait, self.max_wait_s)
            await self.sleep(wait)
            waited += wait

    async def _try_reserve(self, window: int, tokens: int) -> bool:
        key = self.key(window)
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "req", 1)
            pipe.hincrby(key, "tok", tokens)
            pipe.expire(key, KEY_TTL_S)
            req, tok, _ = await pipe.execute()
        if int(req) <= self.rpm and int(tok) <= self.tpm:
            return True
        async with self.client.pipeline(transaction=True) as pipe:  # give it back
            pipe.hincrby(key, "req", -1)
            pipe.hincrby(key, "tok", -tokens)
            await pipe.execute()
        return False


def redis_rate_limiter(
    redis_url: str,
    *,
    provider: str,
    rpm: int,
    tpm: int,
    max_wait_s: float,
    op_timeout_s: float,
) -> RedisRateLimiter:
    """A shared limiter with short Redis timeouts; nothing connects until first use."""
    client = aioredis.Redis.from_url(
        redis_url,
        socket_timeout=op_timeout_s,
        socket_connect_timeout=op_timeout_s,
        retry_on_timeout=False,
    )
    return RedisRateLimiter(
        client=client,
        provider=provider,
        rpm=rpm,
        tpm=tpm,
        fallback=LocalRateLimiter(rpm=rpm, tpm=tpm, max_wait_s=max_wait_s),
        max_wait_s=max_wait_s,
        op_timeout_s=op_timeout_s,
    )
