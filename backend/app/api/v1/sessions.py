"""Workflow endpoints (M3 S6; docs/07). Thin: everything is in `WorkflowService`.

Bodies are read as raw JSON so the service can screen every value BEFORE any validation
or parsing could echo or store it. `Idempotency-Key` is required on every state-changing
POST except session creation.

M5 S1: the request and response shapes are documented in OpenAPI from
`app.api.v1.schemas`, as documentation only. `response_model=None` keeps FastAPI from
validating or re-serializing the service output, and no route declares a body parameter,
so FastAPI never parses a body before the service screens it.
"""

import json
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.v1.schemas import (
    CreateSessionRequest,
    MessageRequest,
    ResumeRequest,
    RetryRequest,
    ReviewCasesResponse,
    RunAccepted,
    SessionCreated,
    SessionView,
    errors,
    json_body,
)
from app.graph.state import ComplianceReport
from app.workflow.service import WorkflowError, WorkflowService

router = APIRouter(tags=["workflow"])

_IDEMPOTENCY = "Required. Reuse the same key to retry the same request safely (at most 200 chars)."
_ACCEPTED: dict[int | str, dict[str, Any]] = {
    202: {"model": RunAccepted, "description": "The run started (or was replayed)."}
}


def service(request: Request) -> WorkflowService:
    svc = getattr(request.app.state, "workflow", None)
    if svc is None:
        raise WorkflowError(503, "WORKFLOW_UNAVAILABLE", "The workflow is not available.")
    return svc  # type: ignore[no-any-return]


async def raw_body(request: Request) -> Any:
    data = await request.body()
    if not data:
        return None
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise WorkflowError(
            422, "VALIDATION_ERROR", "The request body is not valid JSON."
        ) from None


@router.post(
    "/sessions",
    status_code=201,
    response_model=None,
    responses={201: {"model": SessionCreated, "description": "Created."}, **errors(422, 503)},
    openapi_extra=json_body(CreateSessionRequest, required=False),
)
async def create_session(request: Request) -> dict[str, Any]:
    return await service(request).create_session(await raw_body(request))


@router.get(
    "/sessions/{session_id}",
    response_model=None,
    responses={200: {"model": SessionView, "description": "The session."}, **errors(404, 422, 503)},
)
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    return await service(request).get_session(session_id)


@router.post(
    "/sessions/{session_id}/messages",
    status_code=202,
    response_model=None,
    responses={**_ACCEPTED, **errors(400, 404, 409, 410, 422, 503)},
    openapi_extra=json_body(MessageRequest, required=True),
)
async def post_message(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY
    ),
) -> JSONResponse:
    body = await raw_body(request)
    return JSONResponse(
        await service(request).submit_message(session_id, idempotency_key, body), status_code=202
    )


@router.post(
    "/sessions/{session_id}/resume",
    status_code=202,
    response_model=None,
    responses={**_ACCEPTED, **errors(400, 404, 409, 410, 422, 503)},
    openapi_extra=json_body(ResumeRequest, required=True),
)
async def post_resume(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY
    ),
) -> JSONResponse:
    body = await raw_body(request)
    return JSONResponse(
        await service(request).resume(session_id, idempotency_key, body), status_code=202
    )


@router.post(
    "/sessions/{session_id}/retry",
    status_code=202,
    response_model=None,
    responses={**_ACCEPTED, **errors(400, 404, 409, 410, 422, 503)},
    openapi_extra=json_body(RetryRequest, required=False),
)
async def post_retry(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY
    ),
) -> JSONResponse:
    body = await raw_body(request)
    return JSONResponse(
        await service(request).retry(session_id, idempotency_key, body), status_code=202
    )


@router.get(
    "/reports/{report_id}",
    response_model=None,
    responses={
        200: {"model": ComplianceReport, "description": "The stored report (v1 or v2)."},
        **errors(404, 422, 503),
    },
)
async def get_report(report_id: str, request: Request) -> dict[str, Any]:
    return await service(request).get_report(report_id)


@router.get(
    "/reports/{report_id}/review-cases",
    response_model=None,
    responses={
        200: {"model": ReviewCasesResponse, "description": "Open review cases (read-only)."},
        **errors(404, 422, 503),
    },
)
async def get_review_cases(report_id: str, request: Request) -> dict[str, Any]:
    """M4 S7: open review cases (read-only; no decision or status route exists)."""
    return await service(request).get_review_cases(report_id)


@router.get(
    "/sessions/{session_id}/events",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "`text/event-stream`. Each `data:` line is one ProgressEvent "
            "(event `node` or `run`); `id:` is the event id for Last-Event-ID.",
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/ProgressEvent"}}
            },
        },
        **errors(404, 422, 503),
    },
)
async def session_events(
    session_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Server-sent progress events (M3 S7): node events (durable, replayable) and run
    current-status events, read from PostgreSQL; Redis only wakes the stream up.
    Reconnect with `Last-Event-ID` to continue without gaps or duplicates."""
    body = await service(request).open_event_stream(
        session_id, last_event_id, request.is_disconnected
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
