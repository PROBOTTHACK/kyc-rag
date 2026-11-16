"""`/api/v2/assistant` (A5). Thin: everything is in `AssistantService`.

As in v1, no route declares a body parameter (the raw body is parsed by the service, which
never echoes it) and `response_model=None` sends the service output unchanged; the shapes
are documented from `app.api.v2.schemas`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc

from app.api.v1.schemas import errors, json_body
from app.api.v2.schemas import (
    AssistantSessionStarted,
    AssistantSessionView,
    AssistantTurnRequest,
    AssistantTurnResult,
)
from app.assistant.service import AssistantError, AssistantService

router = APIRouter(prefix="/assistant", tags=["assistant"])

log = logging.getLogger(__name__)
UNAVAILABLE = AssistantError(503, "ASSISTANT_UNAVAILABLE", "The assistant is not available.")
# The conversation store cannot be reached (connection refused, dropped, pool timeout): the
# documented 503, codes only. Any other error stays a 500 (A7 finding: these were 500s).
_STORE_DOWN = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.TimeoutError,
    ConnectionError,
    TimeoutError,
)


@asynccontextmanager
async def _store_available() -> AsyncIterator[None]:
    try:
        yield
    except _STORE_DOWN as exc:
        log.warning("assistant storage unavailable: %s", type(exc).__name__)
        raise UNAVAILABLE from None


_IDEMPOTENCY = "Required. Reuse the same key to retry the same request safely (at most 200 chars)."


def service(request: Request) -> AssistantService:
    svc = getattr(request.app.state, "assistant", None)
    if svc is None:
        raise UNAVAILABLE
    return svc  # type: ignore[no-any-return]


@router.post(
    "/sessions",
    status_code=201,
    response_model=None,
    responses={
        201: {"model": AssistantSessionStarted, "description": "Started."},
        **errors(422, 503),
    },
)
async def create_session(request: Request) -> JSONResponse:
    async with _store_available():
        return JSONResponse(await service(request).create_session(), status_code=201)


@router.get(
    "/sessions/{session_id}",
    response_model=None,
    responses={
        200: {"model": AssistantSessionView, "description": "The conversation."},
        **errors(404, 410, 422, 503),
    },
)
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    async with _store_available():
        return await service(request).get_session(session_id)


@router.post(
    "/sessions/{session_id}/turns",
    response_model=None,
    responses={
        200: {"model": AssistantTurnResult, "description": "The answer to this turn."},
        **errors(400, 404, 409, 410, 422, 503),
    },
    openapi_extra=json_body(AssistantTurnRequest, required=True),
)
async def post_turn(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY
    ),
) -> dict[str, Any]:
    body = await request.body()
    async with _store_available():
        return await service(request).submit_turn(session_id, idempotency_key, body)
