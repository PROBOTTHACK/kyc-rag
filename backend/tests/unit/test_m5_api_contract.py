"""M5 S1: the published API contract (decision M5-D2).

- The committed OpenAPI document equals the running app's.
- Routes still parse no body and re-serialize nothing: the bytes sent are exactly the
  service's dict.
- Every documented model accepts the real response shapes (v1 fixtures, v2 NLI-off and
  scripted reports, every session state) and rejects an undocumented key.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app.api.openapi import DEFAULT_PATH, render, spec
from app.api.v1.schemas import (
    ErrorResponse,
    MessageRequest,
    ResumeRequest,
    RetryRequest,
    ReviewCasesResponse,
    RunAccepted,
    SessionCreated,
    SessionView,
)
from app.api.v1.sessions import router as workflow_router
from app.config import Settings
from app.core.events import ProgressEvent
from app.graph.intake import questions_for
from app.graph.review import review_cases
from app.graph.state import ComplianceReport
from app.main import create_app
from app.verification.nli import FakeNLI
from app.workflow.service import MessageBody, ResumeBody, RetryBody, WorkflowError
from tests.unit.test_m4_graph_s5 import ENTAIL, FAKE_T, trust_run

V1_FIXTURES = sorted((Path(__file__).parents[1] / "fixtures" / "m3_reports_v1").glob("*.json"))
SID = "5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f"
RUN = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"
CKPT = "1f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
AT = "2026-11-13T10:00:00.123456+00:00"


# --- fixtures: real shapes -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def v2_reports() -> dict[str, dict[str, Any]]:
    return {
        "v2_nli_off": trust_run()["report"],
        "v2_scripted": trust_run(nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T)["report"],
    }


@pytest.fixture(scope="module")
def reports(v2_reports: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """As `get_report` returns them: the stored JSON through `ComplianceReport`."""
    raw = {p.stem: json.loads(p.read_text("utf-8")) for p in V1_FIXTURES} | v2_reports
    assert len(raw) == 10
    return {k: ComplianceReport.model_validate(v).model_dump(mode="json") for k, v in raw.items()}


def created() -> dict[str, Any]:
    return {"session_id": SID, "status": "active", "data_class": "synthetic_demo", "expires_at": AT}


def workflow(status: str, **extra: Any) -> dict[str, Any]:
    events = [{"seq": 1, "node": "input_security", "status": "completed", "at": AT}]
    return {
        "run_id": RUN,
        "thread_id": RUN,
        "status": status,
        "started_at": AT,
        "finished_at": None if status in ("running", "awaiting_clarification") else AT,
        "node_events": events,
        **extra,
    }


def session_views() -> dict[str, dict[str, Any]]:
    """Every state `get_session` can produce (service.py)."""
    questions = [
        q.model_dump(mode="json") for q in questions_for(["entity_type", "account_type"], [])
    ]
    clar = {"checkpoint_id": CKPT, "round": 1, "max_rounds": 2, "questions": questions}
    failed = workflow("failed")
    failed["node_events"].append(
        {"seq": 2, "node": "error:RuntimeError", "status": "failed", "at": AT}
    )
    return {
        "no_run": created() | {"workflow": None},
        "expired": created() | {"status": "expired", "workflow": None},
        "running": created() | {"workflow": workflow("running")},
        "awaiting": created()
        | {"workflow": workflow("awaiting_clarification", clarification=clar)},
        "awaiting_unreadable": created()
        | {"workflow": workflow("awaiting_clarification", clarification=None)},
        "completed": created()
        | {"workflow": workflow("completed", report_id=SID, outcome="DEGRADED")},
        "completed_no_report": created()
        | {"workflow": workflow("completed", report_id=None, outcome=None)},
        "failed": created() | {"workflow": failed},
        "abandoned": created() | {"workflow": workflow("abandoned")},
    }


def review_view(report: dict[str, Any]) -> dict[str, Any]:
    """As `PostgresReportStore.review_cases_for` + `get_review_cases` return them."""
    model = ComplianceReport.model_validate(report)
    cases = sorted(review_cases(model), key=lambda c: (c.requirement_id, c.reason))
    return {
        "report_id": model.report_id,
        "review_cases": [
            {
                "id": c.id,
                "requirement_id": c.requirement_id,
                "claim_id": c.claim_id,
                "reason": c.reason,
                "status": c.status,
                "created_at": datetime(2026, 11, 13, tzinfo=UTC).isoformat(),
            }
            for c in cases
        ],
    }


# --- the published document ----------------------------------------------------------------------


def app() -> Any:
    return create_app(Settings(_env_file=None, DATABASE_URL="x", REDIS_URL="y"))  # type: ignore[call-arg]


def test_the_committed_openapi_document_matches_the_app() -> None:
    assert DEFAULT_PATH.read_text("utf-8") == render(
        spec()
    ), "docs/api/openapi.json is stale: run `make openapi` and review the diff"


def test_every_workflow_route_documents_its_success_and_error_shapes() -> None:
    paths = spec()["paths"]
    expected = {
        ("post", "/api/v1/sessions"): ("201", "SessionCreated"),
        ("get", "/api/v1/sessions/{session_id}"): ("200", "SessionView"),
        ("post", "/api/v1/sessions/{session_id}/messages"): ("202", "RunAccepted"),
        ("post", "/api/v1/sessions/{session_id}/resume"): ("202", "RunAccepted"),
        ("post", "/api/v1/sessions/{session_id}/retry"): ("202", "RunAccepted"),
        ("get", "/api/v1/reports/{report_id}"): ("200", "ComplianceReport"),
        ("get", "/api/v1/reports/{report_id}/review-cases"): ("200", "ReviewCasesResponse"),
        ("get", "/api/v1/sessions/{session_id}/events"): ("200", "ProgressEvent"),
    }
    for (method, path), (status, model) in expected.items():
        responses = paths[path][method]["responses"]
        content = responses[status]["content"]
        media = "text/event-stream" if path.endswith("/events") else "application/json"
        assert content[media]["schema"]["$ref"] == f"#/components/schemas/{model}"
        errors = [s for s in responses if s.startswith(("4", "5"))]
        assert {"422", "503"} <= set(errors), path
        if path != "/api/v1/sessions":
            assert "404" in errors, path
        for s in errors:
            ref = responses[s]["content"]["application/json"]["schema"]["$ref"]
            assert ref == "#/components/schemas/ErrorResponse", (path, s)
    schemas = spec()["components"]["schemas"]
    assert "HTTPValidationError" not in schemas  # every 422 is the codes-only envelope


def test_request_bodies_are_documented_flat_and_match_the_service_models() -> None:
    paths = spec()["paths"]
    for path, required in [
        ("/api/v1/sessions", False),
        ("/api/v1/sessions/{session_id}/messages", True),
        ("/api/v1/sessions/{session_id}/resume", True),
        ("/api/v1/sessions/{session_id}/retry", False),
    ]:
        body = paths[path]["post"]["requestBody"]
        assert body["required"] is required
        schema = json.dumps(body["content"]["application/json"]["schema"])
        assert "$ref" not in schema and "$defs" not in schema
    pairs: list[tuple[type[BaseModel], type[BaseModel]]] = [
        (MessageRequest, MessageBody),
        (ResumeRequest, ResumeBody),
        (RetryRequest, RetryBody),
    ]
    for doc, real in pairs:
        assert set(doc.model_fields) == set(real.model_fields)
        assert doc.model_config.get("extra") == real.model_config.get("extra") == "forbid"


def test_routes_parse_no_body_and_reserialize_nothing() -> None:
    """FastAPI must not read a body before the service screens it (M3 S6), and must not
    pass the service output through a model (the JSON stays byte-identical)."""
    # The workflow router itself (FastAPI mounts included routers lazily).
    routes = [r for r in workflow_router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 8
    for route in routes:
        assert route.body_field is None, route.path
        assert route.response_model is None, route.path


# --- byte-identical pass-through -----------------------------------------------------------------


class Canned:
    """A service that returns fixed payloads; the routes must send them unchanged."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def create_session(self, body: Any) -> dict[str, Any]:
        return self.payload

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return self.payload

    async def submit_message(self, sid: str, key: str | None, body: Any) -> dict[str, Any]:
        return self.payload

    async def resume(self, sid: str, key: str | None, body: Any) -> dict[str, Any]:
        return self.payload

    async def retry(self, sid: str, key: str | None, body: Any) -> dict[str, Any]:
        return self.payload

    async def get_report(self, report_id: str) -> dict[str, Any]:
        return self.payload

    async def get_review_cases(self, report_id: str) -> dict[str, Any]:
        return self.payload


async def send(payload: dict[str, Any], method: str, path: str) -> httpx.Response:
    a = app()
    a.state.workflow = Canned(payload)
    transport = httpx.ASGITransport(app=a, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, f"/api/v1{path}", content=b"{}")


def same_bytes(r: httpx.Response, payload: dict[str, Any], status: int) -> None:
    assert r.status_code == status
    assert r.content == JSONResponse(content=payload).body
    assert r.headers["content-type"] == "application/json"


async def test_sessions_pass_through_and_match_the_documented_models() -> None:
    same_bytes(await send(created(), "POST", "/sessions"), created(), 201)
    SessionCreated.model_validate(created())
    for name, view in session_views().items():
        same_bytes(await send(view, "GET", f"/sessions/{SID}"), view, 200)
        assert SessionView.model_validate(view).session_id == SID, name
    accepted = {"session_id": SID, "run_id": RUN, "status": "running"}
    for path in ("messages", "resume", "retry"):
        same_bytes(await send(accepted, "POST", f"/sessions/{SID}/{path}"), accepted, 202)
    RunAccepted.model_validate(accepted)


async def test_reports_and_review_cases_pass_through_and_match(
    reports: dict[str, dict[str, Any]],
) -> None:
    for name, report in reports.items():
        same_bytes(await send(report, "GET", f"/reports/{SID}"), report, 200)
        ComplianceReport.model_validate(report)
        cases = review_view(report)
        same_bytes(await send(cases, "GET", f"/reports/{SID}/review-cases"), cases, 200)
        ReviewCasesResponse.model_validate(cases)
        if name == "v2_nli_off":
            assert cases["review_cases"], name  # one `not_verified` case per item


async def test_error_envelopes_match_the_documented_model() -> None:
    class Failing:
        def __init__(self, err: WorkflowError) -> None:
            self.err = err

        async def get_session(self, session_id: str) -> dict[str, Any]:
            raise self.err

    for err in [
        WorkflowError(404, "SESSION_NOT_FOUND", "Session not found."),
        WorkflowError(409, "RUN_IN_PROGRESS", "busy", retry_after=60),
        WorkflowError(422, "SENSITIVE_DATA_DETECTED", "no", categories=["pan"]),
    ]:
        a = app()
        a.state.workflow = Failing(err)
        transport = httpx.ASGITransport(app=a, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/v1/sessions/{SID}")
        assert r.status_code == err.status
        ErrorResponse.model_validate(r.json())


# --- the models catch drift ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (SessionCreated, created() | {"owner": "x"}),
        (SessionView, session_views()["completed"] | {"report": {}}),
        (RunAccepted, {"session_id": SID, "run_id": RUN, "status": "running", "x": 1}),
        (ErrorResponse, {"error": {"code": "C", "message": "m", "input": "echo"}}),
        (ProgressEvent, {"kind": "node", "run_id": RUN, "status": "completed", "at": AT, "x": 1}),
    ],
)
def test_an_undocumented_key_fails_validation(model: type[BaseModel], payload: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_review_cases_cannot_document_a_decision() -> None:
    case = review_view(trust_run()["report"])["review_cases"][0]
    for bad in ({"status": "approved"}, {"status": "resolved"}, {"decision": "reject"}):
        with pytest.raises(ValidationError):
            ReviewCasesResponse.model_validate({"report_id": SID, "review_cases": [case | bad]})
