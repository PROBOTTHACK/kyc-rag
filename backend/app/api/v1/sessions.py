"""Workflow endpoints (M3 S6; docs/07). Thin: everything is in `WorkflowService`.

Bodies are read as raw JSON so the service can screen every value BEFORE any validation
or parsing could echo or store it. `Idempotency-Key` is required on every state-changing
POST except session creation.
"""

import json
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.workflow.service import WorkflowError, WorkflowService

router = APIRouter(tags=["workflow"])


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


@router.post("/sessions", status_code=201)
async def create_session(request: Request) -> dict[str, Any]:
    return await service(request).create_session(await raw_body(request))


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    return await service(request).get_session(session_id)


@router.post("/sessions/{session_id}/messages", status_code=202)
async def post_message(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    body = await raw_body(request)
    return JSONResponse(
        await service(request).submit_message(session_id, idempotency_key, body), status_code=202
    )


@router.post("/sessions/{session_id}/resume", status_code=202)
async def post_resume(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    body = await raw_body(request)
    return JSONResponse(
        await service(request).resume(session_id, idempotency_key, body), status_code=202
    )


@router.post("/sessions/{session_id}/retry", status_code=202)
async def post_retry(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    body = await raw_body(request)
    return JSONResponse(
        await service(request).retry(session_id, idempotency_key, body), status_code=202
    )


@router.get("/reports/{report_id}")
async def get_report(report_id: str, request: Request) -> dict[str, Any]:
    return await service(request).get_report(report_id)


@router.get("/sessions/{session_id}/events")
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
