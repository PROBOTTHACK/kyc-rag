"""Hits the real docker services. Run with `make up` then `pytest -m integration`."""

import pytest
from fastapi.testclient import TestClient

from app.core.loop import selector_loop_factory
from app.main import create_app

pytestmark = pytest.mark.integration


def test_live_health_all_green() -> None:
    # Same loop as `make api`: psycopg async can't run on Windows' Proactor loop.
    app = create_app()
    with TestClient(app, backend_options={"loop_factory": selector_loop_factory}) as client:
        r = client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
