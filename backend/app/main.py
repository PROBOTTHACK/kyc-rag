"""FastAPI entrypoint: `uvicorn app.main:app`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.core.health import postgres_check, qdrant_check, redis_check
from app.core.logging import configure_logging
from app.db.session import create_engine, create_sessionmaker


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
    try:
        yield
    finally:
        await qdrant.close()
        await redis.aclose()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.APP_ENV)
    app = FastAPI(title="Agentic KYC Compliance Engine", version="0.0.1", lifespan=lifespan)
    app.state.settings = settings
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
