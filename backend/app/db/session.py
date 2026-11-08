"""SQLAlchemy engines and session factories.

The API uses the async engine. Ingestion (CLI, workers) uses the sync engine: the
persistence functions in `app.db.persistence` / `app.db.repository` take a sync
`Session`, and async callers can reuse them unchanged through
`AsyncSession.run_sync(lambda s: persist_document(s, ...))`.
Both engines use the same psycopg 3 URL.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

CONNECT_TIMEOUT_S = 5


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def create_sync_engine(settings: Settings) -> sa.Engine:
    return sa.create_engine(
        settings.DATABASE_URL.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


def create_sync_sessionmaker(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
