"""M3 S1: request + token rate limiting, local and Redis-shared (Redis faked here; the
real Redis is exercised in tests/integration/test_llm_redis.py)."""

import asyncio
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.ratelimit import (
    LocalRateLimiter,
    RedisRateLimiter,
    estimate_tokens,
)
from app.llm.common import ProviderRateLimited, ProviderUnavailable


class FakeTime:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class FakePipe:
    def __init__(self, store: dict[str, dict[str, int]], fail: bool) -> None:
        self.store, self.fail, self.ops = store, fail, []

    async def __aenter__(self) -> "FakePipe":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def hincrby(self, key: str, field: str, n: int) -> None:
        self.ops.append(("hincrby", key, field, n))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", key, ttl))

    async def execute(self) -> list[Any]:
        if self.fail:
            raise RedisConnectionError("down")
        out: list[Any] = []
        for op in self.ops:
            if op[0] == "hincrby":
                h = self.store.setdefault(op[1], {})
                h[op[2]] = h.get(op[2], 0) + op[3]
                out.append(h[op[2]])
            else:
                out.append(True)
        return out


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, dict[str, int]] = {}
        self.fail = fail

    def pipeline(self, transaction: bool = True) -> FakePipe:
        return FakePipe(self.store, self.fail)


def local(
    rpm: int = 2, tpm: int = 1000, max_wait_s: float = 120
) -> tuple[LocalRateLimiter, FakeTime]:
    t = FakeTime()
    return LocalRateLimiter(rpm, tpm, max_wait_s=max_wait_s, clock=t.clock, sleep=t.sleep), t


def shared(
    client: FakeRedis, rpm: int = 2, tpm: int = 1000, max_wait_s: float = 120
) -> tuple[RedisRateLimiter, FakeTime, LocalRateLimiter]:
    t = FakeTime(now=600.0)  # window 10
    fb = LocalRateLimiter(rpm, tpm, max_wait_s=max_wait_s, clock=t.clock, sleep=t.sleep)
    lim = RedisRateLimiter(client=client, provider="groq", rpm=rpm, tpm=tpm, fallback=fb,
                           max_wait_s=max_wait_s, clock=t.clock, sleep=t.sleep)  # fmt: skip
    return lim, t, fb


def test_token_estimate_is_conservative() -> None:
    assert estimate_tokens("a" * 300, "b" * 300, max_output_tokens=100) == 300


def test_local_requests_per_minute() -> None:
    lim, t = local(rpm=2)
    for _ in range(3):
        asyncio.run(lim.acquire(10))
    assert t.sleeps == [60.0]


def test_local_tokens_per_minute() -> None:
    lim, t = local(rpm=100, tpm=1000)
    asyncio.run(lim.acquire(600))
    asyncio.run(lim.acquire(600))  # 1200 > 1000: waits for the first to leave the window
    assert t.sleeps == [60.0]


def test_a_request_larger_than_the_budget_fails_at_once_and_is_not_transient() -> None:
    lim, t = local(tpm=1000)
    with pytest.raises(ProviderUnavailable, match="per-minute budget") as err:
        asyncio.run(lim.acquire(1001))
    assert err.value.transient is False and t.sleeps == []


def test_waiting_is_bounded_and_transient() -> None:
    lim, _ = local(rpm=1, max_wait_s=30)
    asyncio.run(lim.acquire(1))
    with pytest.raises(ProviderRateLimited) as err:
        asyncio.run(lim.acquire(1))
    assert err.value.transient is True


def test_shared_window_grants_counts_and_rolls_back_a_denied_reservation() -> None:
    client = FakeRedis()
    lim, t, fb = shared(client, rpm=2, tpm=1000)
    asyncio.run(lim.acquire(400))
    asyncio.run(lim.acquire(400))
    assert client.store["rl:groq:10"] == {"req": 2, "tok": 800}
    asyncio.run(lim.acquire(400))  # denied in window 10, granted in window 11
    assert t.sleeps == [60.0]
    assert client.store["rl:groq:10"] == {"req": 2, "tok": 800}  # the denial was undone
    assert client.store["rl:groq:11"] == {"req": 1, "tok": 400}
    assert lim.redis_failures == 0 and len(fb._events) == 0  # Redis was used, not the fallback


def test_shared_window_waits_are_bounded() -> None:
    lim, _, _ = shared(FakeRedis(), rpm=1, max_wait_s=10)
    asyncio.run(lim.acquire(1))
    with pytest.raises(ProviderRateLimited) as err:
        asyncio.run(lim.acquire(1))
    assert err.value.transient is True


def test_redis_outage_falls_back_to_the_local_window_with_the_same_limits() -> None:
    lim, t, fb = shared(FakeRedis(fail=True), rpm=2)
    for _ in range(3):
        asyncio.run(lim.acquire(10))
    assert lim.redis_failures == 3 and lim.fallback_acquisitions == 3
    assert t.sleeps == [60.0]  # the local window enforced the same rpm


def test_keys_carry_only_the_provider_and_window() -> None:
    lim, _, _ = shared(FakeRedis())
    assert lim.key(29_000_000) == "rl:groq:29000000"
