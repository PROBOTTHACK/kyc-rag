import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.core.health import HealthCheck
from app.main import create_app

ClientFactory = Callable[[dict[str, HealthCheck]], httpx.AsyncClient]

SECRET_URL = "postgresql+psycopg://u:synthetic_pw@db.internal:5432/d"


async def _ok() -> None:
    return None


async def _hang() -> None:
    await asyncio.sleep(10)


async def _leaky_error() -> None:
    raise ConnectionError(f"could not connect to {SECRET_URL}")


def _app(checks: dict[str, HealthCheck]) -> FastAPI:
    settings = Settings(_env_file=None, HEALTH_CHECK_TIMEOUT_S=0.1)  # type: ignore[call-arg]
    app = create_app(settings)
    app.state.health_checks = checks
    return app


@pytest.fixture
async def make_client() -> AsyncIterator[ClientFactory]:
    clients: list[httpx.AsyncClient] = []

    def factory(checks: dict[str, HealthCheck]) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=_app(checks))
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client

    yield factory
    for c in clients:
        await c.aclose()


async def test_all_ok_returns_200(make_client: ClientFactory) -> None:
    client = make_client({"postgres": _ok, "redis": _ok, "qdrant": _ok})
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"postgres", "redis", "qdrant"}
    assert all(c["ok"] and c["error"] is None for c in body["checks"].values())


async def test_timeout_returns_503(make_client: ClientFactory) -> None:
    client = make_client({"postgres": _ok, "redis": _hang, "qdrant": _ok})
    r = await client.get("/api/v1/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == {
        "ok": False,
        "latency_ms": body["checks"]["redis"]["latency_ms"],
        "error": "TimeoutError",
    }
    assert body["checks"]["postgres"]["ok"] is True


async def test_errors_do_not_leak_connection_details(make_client: ClientFactory) -> None:
    client = make_client({"postgres": _leaky_error, "redis": _ok, "qdrant": _ok})
    r = await client.get("/api/v1/health")
    assert r.status_code == 503
    assert r.json()["checks"]["postgres"]["error"] == "ConnectionError"
    assert "synthetic_pw" not in r.text
    assert "db.internal" not in r.text
