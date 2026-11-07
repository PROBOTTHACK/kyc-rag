from typing import Literal

from pydantic import BaseModel


class CheckResult(BaseModel):
    ok: bool
    latency_ms: float
    # Exception class name only; never messages that might contain URLs or credentials.
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, CheckResult]
