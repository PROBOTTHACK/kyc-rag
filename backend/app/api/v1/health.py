from fastapi import APIRouter, Request, Response, status

from app.core.health import run_health_checks
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "At least one check failed."}},
)
async def health(request: Request, response: Response) -> HealthResponse:
    result = await run_health_checks(
        request.app.state.health_checks, request.app.state.settings.HEALTH_CHECK_TIMEOUT_S
    )
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
