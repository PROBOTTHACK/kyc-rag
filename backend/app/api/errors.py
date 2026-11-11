"""The error envelope `{error: {code, message, ...}}` (docs/07; M3 S6).

Handlers never echo request input and never log request bodies: FastAPI's default
validation response includes the submitted values, which could contain personal data,
so it is replaced by a codes-only response. Unexpected errors return INTERNAL_ERROR with
no stack trace, and only the exception class is logged.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.workflow.service import WorkflowError

log = logging.getLogger(__name__)


def envelope(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    headers = {"Retry-After": str(extra["retry_after"])} if "retry_after" in extra else None
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, **extra}},
        headers=headers,
    )


async def workflow_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, WorkflowError)  # noqa: S101 - registered for this type only
    return envelope(exc.status, exc.code, exc.message, **exc.extra)


async def validation_error(request: Request, exc: Exception) -> JSONResponse:
    return envelope(422, "VALIDATION_ERROR", "The request is not valid.")


async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return envelope(500, "INTERNAL_ERROR", "An internal error occurred.")


def install(app: FastAPI) -> None:
    app.add_exception_handler(WorkflowError, workflow_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
