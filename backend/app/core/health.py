"""Dependency health checks for Postgres, Redis and Qdrant."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.schemas.health import CheckResult, HealthResponse

HealthCheck = Callable[[], Awaitable[None]]


def postgres_check(engine: AsyncEngine) -> HealthCheck:
    async def check() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return check


def redis_check(client: Redis) -> HealthCheck:
    async def check() -> None:
        if not await client.ping():
            raise ConnectionError("redis ping returned false")

    return check


def qdrant_check(client: AsyncQdrantClient) -> HealthCheck:
    async def check() -> None:
        await client.get_collections()

    return check


async def _run_one(check: HealthCheck, timeout_s: float) -> CheckResult:
    start = time.perf_counter()
    error: str | None = None
    try:
        await asyncio.wait_for(check(), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        error = type(exc).__name__
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    return CheckResult(ok=error is None, latency_ms=latency_ms, error=error)


async def run_health_checks(checks: Mapping[str, HealthCheck], timeout_s: float) -> HealthResponse:
    names = list(checks)
    results = await asyncio.gather(*(_run_one(checks[n], timeout_s) for n in names))
    by_name = dict(zip(names, results, strict=True))
    status = "ok" if all(r.ok for r in results) else "degraded"
    return HealthResponse(status=status, checks=by_name)
