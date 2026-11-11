"""M3 S1: the shared rate limiter and the LLM cache against the real Redis (`make up`).

Isolated by a unique provider name / key set that is removed afterwards. An unreachable
Redis is a client pointed at a closed port: results must be identical, only slower or
less shared. No LLM is called: the provider is the scripted fake.
"""

import asyncio
import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.core.ratelimit import LocalRateLimiter, RedisRateLimiter
from app.llm.cache import CachedLLMProvider, SyntheticAllowlist, synthetic_input_digest
from app.llm.fake import scripted_llm
from app.llm.provider import LLMRequest

pytestmark = pytest.mark.integration
DEMO = "Synthetic demo: KYC documents for an LLP current account"
CLOSED_PORT = "redis://127.0.0.1:1/0"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


def client(url: str | None = None) -> Any:
    return aioredis.Redis.from_url(
        url or get_settings().REDIS_URL.get_secret_value(),
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        retry_on_timeout=False,
    )


class FakeTime:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def limiter(c: Any, provider: str, t: FakeTime, rpm: int = 2) -> RedisRateLimiter:
    fb = LocalRateLimiter(rpm, 1000, clock=t.clock, sleep=t.sleep)
    return RedisRateLimiter(client=c, provider=provider, rpm=rpm, tpm=1000, fallback=fb,
                            op_timeout_s=0.5, clock=t.clock, sleep=t.sleep)  # fmt: skip


def test_two_limiters_share_one_budget_through_redis() -> None:
    provider = f"test-{uuid.uuid4().hex}"

    async def scenario() -> tuple[list[float], dict[Any, Any], int]:
        c = client()
        try:
            t = FakeTime(now=60.0 * 1000)
            a, b = limiter(c, provider, t), limiter(c, provider, t)
            await a.acquire(100)
            await b.acquire(100)
            await a.acquire(100)  # third in the window: waits for the next one
            stored = await c.hgetall(f"rl:{provider}:1000")
            ttl = await c.ttl(f"rl:{provider}:1001")
            return t.sleeps, stored, ttl
        finally:
            keys = [k async for k in c.scan_iter(f"rl:{provider}:*")]
            if keys:
                await c.delete(*keys)
            await c.aclose()

    sleeps, stored, ttl = asyncio.run(scenario())
    assert sleeps == [60.0]
    assert {k.decode(): int(v) for k, v in stored.items()} == {"req": 2, "tok": 200}
    assert 0 < ttl <= 120


def test_unreachable_redis_falls_back_to_the_local_window() -> None:
    async def scenario() -> RedisRateLimiter:
        c = client(CLOSED_PORT)
        try:
            lim = limiter(c, "test-down", FakeTime(now=0.0), rpm=5)
            for _ in range(3):
                await lim.acquire(10)
            return lim
        finally:
            await c.aclose()

    lim = asyncio.run(scenario())
    assert lim.redis_failures == 3 and lim.fallback_acquisitions == 3


def test_cache_round_trip_and_outage_against_real_redis() -> None:
    allow = SyntheticAllowlist(frozenset({synthetic_input_digest(DEMO)}))
    req = LLMRequest(
        prompt_id=f"it-{uuid.uuid4().hex}",
        prompt_version="1",
        system="sys",
        user=DEMO,
        synthetic_input_digest=synthetic_input_digest(DEMO),
    )
    valid = Answer(items=["LLP agreement"])

    async def scenario() -> tuple[Any, Any, Any, int, int]:
        c = client()
        inner = scripted_llm(valid)
        p = CachedLLMProvider(inner=inner, client=c, allowlist=allow, ttl_s=60, op_timeout_s=0.5)
        key = p.key(req, Answer)
        try:
            first = await p.generate_structured(req, Answer)
            second = await p.generate_structured(req, Answer)
            ttl = await c.ttl(key)
            down = aioredis.Redis.from_url(CLOSED_PORT, socket_connect_timeout=0.5)
            outage = CachedLLMProvider(inner=scripted_llm(valid), client=down, allowlist=allow,
                                       op_timeout_s=0.5)  # fmt: skip
            third = await outage.generate_structured(req, Answer)
            await down.aclose()
            return first, second, third, ttl, len(inner.requests)
        finally:
            await c.delete(key)
            await c.aclose()

    first, second, third, ttl, calls = asyncio.run(scenario())
    assert calls == 1 and second.cache_hit and second.parsed == first.parsed == valid
    assert third.parsed == valid and not third.cache_hit  # Redis down: same result
    assert 0 < ttl <= 60
