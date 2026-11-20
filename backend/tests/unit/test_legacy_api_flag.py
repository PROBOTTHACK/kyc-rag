"""LEGACY_API_ENABLED (owner decision, 2026-11-16): a small deployment can leave the legacy
workflow API unmounted and unbuilt. The default (true) keeps every legacy route, so local
development, the published API contract and the legacy tests are unchanged."""

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.main import create_app

URL = "postgresql+psycopg://u:p@localhost:1/d"  # never connected in these tests


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, DATABASE_URL=URL, REDIS_URL="redis://localhost:1/0", **kw)  # type: ignore[call-arg]


def paths(app: Any) -> set[str]:
    # The published routes (FastAPI nests included routers, so app.routes is not flat).
    return set(app.openapi()["paths"])


def test_the_default_mounts_the_legacy_api() -> None:
    p = paths(create_app(settings()))
    assert "/api/v1/health" in p and "/api/v1/sessions" in p
    assert "/api/v2/assistant/sessions" in p


def test_disabled_keeps_health_and_the_assistant_but_no_legacy_route() -> None:
    p = paths(create_app(settings(LEGACY_API_ENABLED=False)))
    assert "/api/v1/health" in p
    assert "/api/v2/assistant/sessions" in p
    assert not [r for r in p if r.startswith("/api/v1/") and r != "/api/v1/health"]


async def test_disabled_never_builds_the_workflow_runtime() -> None:
    async def explode(*_: Any) -> Any:
        raise AssertionError("the legacy runtime must not be built")

    app = create_app(settings(LEGACY_API_ENABLED=False), runtime_factory=explode)
    app.state.assistant = object()  # a preset service: the lifespan builds nothing else
    app.state.health_checks = {}
    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, "workflow")


async def test_disabled_legacy_routes_answer_404() -> None:
    app = create_app(settings(LEGACY_API_ENABLED=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/sessions", json={"query": "synthetic"})
    assert r.status_code == 404


@pytest.mark.parametrize("enabled", [True, False])
async def test_health_answers_either_way(enabled: bool) -> None:
    async def ok() -> None:
        return None

    app = create_app(settings(LEGACY_API_ENABLED=enabled))
    app.state.health_checks = {"postgres": ok, "redis": ok, "qdrant": ok}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
