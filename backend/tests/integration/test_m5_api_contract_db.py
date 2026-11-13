"""M5 S1: real responses from the real app (Postgres checkpointer, workflow tables, report
store) validate against the documented contract (`app.api.v1.schemas`, `extra="forbid"`).

Covers every documented success shape on the LLP flow (create, view before the first
message, accepted, awaiting clarification, completed, the v2 report, its review cases,
the SSE events) and the error envelope for the main error codes. Scripted LLM; no network.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from alembic import command
from app.api.v1.schemas import (
    ErrorResponse,
    ReviewCasesResponse,
    RunAccepted,
    SessionCreated,
    SessionView,
)
from app.core.events import ProgressEvent
from app.graph.state import ComplianceReport
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.test_workflow_events import parse, read_stream
from tests.integration.workflow_harness import (
    LLP_QUESTION,
    PAN,
    SchemaLLM,
    api,
    enable_app_logging,
    wait_for,
)

pytestmark = pytest.mark.integration
UNKNOWN = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def engine(temp_db: str) -> Iterator[sa.Engine]:
    command.upgrade(alembic_cfg(), "head")
    enable_app_logging()
    eng = sync_engine(temp_db)
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: sa.Engine) -> Iterator[TestClient]:
    with api(SchemaLLM()) as c:
        yield c


def post(client: TestClient, path: str, body: Any, key: str | None) -> Any:
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/v1{path}", content=json.dumps(body), headers=headers)


def error(r: Any, status: int, code: str) -> ErrorResponse:
    assert r.status_code == status, r.text
    parsed = ErrorResponse.model_validate(r.json())
    assert parsed.error.code == code
    return parsed


def test_every_success_shape_of_the_llp_flow_matches_the_contract(client: TestClient) -> None:
    r = client.post("/api/v1/sessions", json={"data_class": "synthetic_demo"})
    assert r.status_code == 201
    sid = SessionCreated.model_validate(r.json()).session_id
    assert SessionView.model_validate(client.get(f"/api/v1/sessions/{sid}").json()).workflow is None

    r = post(client, f"/sessions/{sid}/messages", {"message": LLP_QUESTION}, "m1")
    assert r.status_code == 202
    RunAccepted.model_validate(r.json())

    view = SessionView.model_validate(wait_for(client, sid, "awaiting_clarification"))
    assert view.workflow is not None and view.workflow.clarification is not None
    clar = view.workflow.clarification
    assert [q.field for q in clar.questions] == ["account_type"]

    body = {"checkpoint_id": clar.checkpoint_id, "answers": {"account_type": "CURRENT_ACCOUNT"}}
    r = post(client, f"/sessions/{sid}/resume", body, "r1")
    assert r.status_code == 202
    RunAccepted.model_validate(r.json())

    done = SessionView.model_validate(wait_for(client, sid, "completed"))
    assert done.workflow is not None and done.workflow.report_id
    assert done.workflow.outcome == "DEGRADED"  # NLI disabled (M4)

    report = client.get(f"/api/v1/reports/{done.workflow.report_id}").json()
    assert ComplianceReport.model_validate(report).report_version == 2
    cases = client.get(f"/api/v1/reports/{done.workflow.report_id}/review-cases").json()
    parsed = ReviewCasesResponse.model_validate(cases)
    assert parsed.review_cases and {c.reason for c in parsed.review_cases} == {"not_verified"}

    events = parse(read_stream(client, sid))
    assert events
    for _, kind, data in events:
        assert ProgressEvent.model_validate(data).kind == kind


def test_error_shapes_match_the_contract(client: TestClient) -> None:
    error(client.get(f"/api/v1/sessions/{UNKNOWN}"), 404, "SESSION_NOT_FOUND")
    error(client.get(f"/api/v1/reports/{UNKNOWN}"), 404, "REPORT_NOT_FOUND")
    error(client.get(f"/api/v1/reports/{UNKNOWN}/review-cases"), 404, "REPORT_NOT_FOUND")
    error(client.post("/api/v1/sessions", json={"data_class": "real"}), 422, "VALIDATION_ERROR")

    sid = client.post("/api/v1/sessions", json={"data_class": "synthetic_demo"}).json()[
        "session_id"
    ]
    error(post(client, f"/sessions/{sid}/messages", {"message": "x"}, None), 400,
          "IDEMPOTENCY_KEY_REQUIRED")  # fmt: skip
    pii = error(
        post(client, f"/sessions/{sid}/messages", {"message": f"PAN {PAN}"}, "p1"),
        422,
        "SENSITIVE_DATA_DETECTED",
    )
    assert pii.error.categories and PAN not in json.dumps(pii.model_dump())
    error(post(client, f"/sessions/{sid}/retry", {}, "t1"), 409, "NOTHING_TO_RETRY")
    resume = {"checkpoint_id": UNKNOWN, "answers": {"account_type": "CURRENT_ACCOUNT"}}
    error(post(client, f"/sessions/{sid}/resume", resume, "q1"), 409, "NOT_AWAITING_CLARIFICATION")
