"""M3 S6 unit tests: the error envelope, input never echoed, and the source-level guarantee
that only the workflow service submits input to the graph and only the gate builds a
`Command` (no public path can resume with a raw value)."""

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.workflow.service import WorkflowError, _screen_raw, _sha

APP = Path(__file__).parents[2] / "app"


class FakeService:
    async def create_session(self, body: Any) -> dict[str, Any]:
        raise WorkflowError(409, "RUN_IN_PROGRESS", "busy", retry_after=7)

    async def get_report(self, report_id: str) -> dict[str, Any]:
        raise RuntimeError(f"boom with secret {report_id}")


async def client_for(service: Any | None) -> httpx.AsyncClient:
    app = create_app(Settings(_env_file=None, DATABASE_URL="x", REDIS_URL="y"))  # type: ignore[call-arg]
    if service is not None:
        app.state.workflow = service
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_errors_use_the_envelope_with_retry_after() -> None:
    async with await client_for(FakeService()) as c:
        r = await c.post("/api/v1/sessions", content=b"{}")
    assert r.status_code == 409 and r.headers["Retry-After"] == "7"
    assert r.json() == {"error": {"code": "RUN_IN_PROGRESS", "message": "busy", "retry_after": 7}}


async def test_unexpected_errors_hide_details() -> None:
    async with await client_for(FakeService()) as c:
        r = await c.get("/api/v1/reports/SECRET-ID")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert b"SECRET-ID" not in r.content and b"boom" not in r.content


async def test_invalid_json_is_not_echoed_and_no_runtime_means_503() -> None:
    async with await client_for(FakeService()) as c:
        r = await c.post(
            "/api/v1/sessions/abc/messages",
            content=b"{SECRET-WORD",
            headers={"Idempotency-Key": "k"},
        )
    assert r.status_code == 422 and b"SECRET-WORD" not in r.content
    async with await client_for(None) as c:
        r = await c.get("/api/v1/sessions/abc")
    assert (r.status_code, r.json()["error"]["code"]) == (503, "WORKFLOW_UNAVAILABLE")


@pytest.mark.parametrize(
    "body",
    [{"answers": {"x": "ABCPE1234F"}}, {"ABCPE1234F": 1}, ["ABCPE1234F"], {"n": 4111111111111111}],
)
def test_raw_bodies_are_screened_everywhere(body: Any) -> None:
    with pytest.raises(WorkflowError) as err:
        _screen_raw(body, 2000)
    assert err.value.code == "SENSITIVE_DATA_DETECTED" and err.value.extra["categories"]
    assert "ABCPE1234F" not in str(err.value.extra)


def test_request_hashes_are_canonical() -> None:
    assert _sha("resume", {"a": 1, "b": 2}) == _sha("resume", {"b": 2, "a": 1})
    assert _sha("resume", {"a": 1}) != _sha("retry", {"a": 1})


def sources() -> dict[str, str]:
    return {
        str(p.relative_to(APP)).replace("\\", "/"): p.read_text("utf-8") for p in APP.rglob("*.py")
    }


def calls(src: str) -> list[str]:
    """Names of everything called in the source (code only: docstrings don't count)."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            func = node.func
            out.append(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return out


def test_only_the_gate_constructs_a_command() -> None:
    builders = [f for f, src in sources().items() if "Command" in calls(src)]
    assert builders == ["graph/inputs.py"]


def test_only_the_workflow_service_submits_input_to_the_graph() -> None:
    submitters = {"astream", "ainvoke", "invoke", "stream"}
    callers = [f for f, src in sources().items() if submitters & set(calls(src))]
    assert callers == ["workflow/service.py"]
    service = sources()["workflow/service.py"]
    assert calls(service).count("astream") == 1  # the single submission point
    assert "def _execute" in service and "SUBMITTABLE" in service
