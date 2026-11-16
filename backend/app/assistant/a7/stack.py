"""The evaluation stack: conversations in a throwaway PostgreSQL database, evidence read
read-only from the development database (the same split as the A5 integration tests).

The throwaway database is created, migrated to head and dropped on exit; the development
database and its corpus are never written. A "restart" is a new service, store and engine
on the same throwaway database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.assistant.service import AssistantService, LazyChecklist
from app.assistant.store import PostgresAssistantStore
from app.config import Settings, get_settings

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _alembic() -> Config:
    cfg = Config(os.path.join(BACKEND, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND, "alembic"))
    return cfg


@contextmanager
def throwaway_database(prefix: str = "kyc_a7") -> Iterator[str]:
    """A migrated, empty database for conversations; dropped afterwards."""
    base = make_url(get_settings().DATABASE_URL.get_secret_value())
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = base.set(database=name).render_as_string(hide_password=False)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        command.upgrade(_alembic(), "head")
        yield url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass
class Stack:
    """Builds services over one conversation database and one shared checklist factory."""

    url: str
    checklist: Any  # a ChecklistFactory (LazyChecklist, possibly wrapped)
    engines: list[AsyncEngine] = field(default_factory=list)

    def service(
        self,
        llm: Any = None,
        *,
        context_tokens: int = 1500,
        recent_turns: int = 10,
        tracer: Any = None,
    ) -> AssistantService:
        # NullPool: a connection per use, closed afterwards, so the many services an
        # evaluation builds (one per conversation, more for restarts) never exhaust the
        # server's connection limit.
        engine = create_async_engine(
            make_url(self.url).set(drivername="postgresql+psycopg"), poolclass=NullPool
        )
        self.engines.append(engine)
        store = PostgresAssistantStore(async_sessionmaker(engine, expire_on_commit=False))
        return AssistantService(
            store,
            self.checklist,
            llm,
            context_tokens=context_tokens,
            recent_turns=recent_turns,
            tracer=tracer,
        )

    async def close(self) -> None:
        for e in self.engines:
            await e.dispose()
        self.engines.clear()


def development_checklist(settings: Settings | None = None) -> LazyChecklist:
    """The production checklist over the development corpus (read-only)."""
    return LazyChecklist(settings or get_settings())
