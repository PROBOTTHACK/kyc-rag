"""FastAPI entrypoint: `uvicorn app.main:app`.

M3 S6: the lifespan builds the workflow runtime (Postgres checkpointer pool, graph deps)
and service; tests pass `runtime_factory` to inject fakes. At startup stale run leases
are only logged, never re-executed; at shutdown running tasks are cancelled and their
runs become recoverable with POST /retry once their lease expires (decision S6-D2).
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import errors
from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.core.health import postgres_check, qdrant_check, redis_check
from app.core.logging import configure_logging
from app.db.session import create_engine, create_sessionmaker
from app.workflow.service import WorkflowRuntime, WorkflowService

log = logging.getLogger(__name__)
RuntimeFactory = Callable[[Settings, async_sessionmaker[AsyncSession]], Awaitable[WorkflowRuntime]]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    timeout = settings.HEALTH_CHECK_TIMEOUT_S
    engine = create_engine(settings)
    redis = Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None,
        timeout=int(timeout) or 1,
    )
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.redis = redis
    app.state.qdrant = qdrant
    # Tests may pre-set fake checks; don't overwrite them.
    if not hasattr(app.state, "health_checks"):
        app.state.health_checks = {
            "postgres": postgres_check(engine),
            "redis": redis_check(redis),
            "qdrant": qdrant_check(qdrant),
        }
    factory: RuntimeFactory | None = getattr(app.state, "runtime_factory", None)
    if factory is None:
        from app.workflow.runtime import build_runtime

        factory = build_runtime
    workflow = WorkflowService(await factory(settings, app.state.sessionmaker))
    app.state.workflow = workflow
    try:
        await workflow.log_stale_leases()
    except Exception as exc:  # noqa: BLE001 - startup must not fail on a reporting query
        log.warning("could not check stale workflow leases: %s", type(exc).__name__)
    try:
        yield
    finally:
        await workflow.shutdown()
        await qdrant.close()
        await redis.aclose()
        await engine.dispose()


def create_app(
    settings: Settings | None = None, *, runtime_factory: RuntimeFactory | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.APP_ENV)
    app = FastAPI(title="Agentic KYC Compliance Engine", version="0.0.1", lifespan=lifespan)
    app.state.settings = settings
    if runtime_factory is not None:
        app.state.runtime_factory = runtime_factory
    errors.install(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
