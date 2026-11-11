"""Build the workflow runtime for the API process (M3 S6).

- Checkpointer: `AsyncPostgresSaver` over an `AsyncConnectionPool` whose `search_path` is
  the `langgraph` schema created by migration 0007 (no `setup()` at runtime). The pool
  opens without waiting, so the app starts even while Postgres is briefly unavailable.
- Graph deps: the real retriever (`retriever_from_settings`), the read-only corpus reader
  with the form cache, the insert-only report store, and the live-gated Groq stack. When
  the live gate is closed there is no LLM (decision S6-D5): intake asks every question
  and synthesis degrades, so outcomes stay honest. Nothing here sends anything anywhere
  at startup.
"""

import logging
from datetime import timedelta

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.cache import JsonCache
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.db.workflow import PostgresReportStore
from app.graph.builder import GraphDeps, build_graph
from app.graph.corpus import PostgresCorpusReader
from app.llm.common import ProviderUnavailable
from app.llm.groq import llm_from_settings
from app.llm.provider import LLMProvider
from app.retrieval.factory import retriever_from_settings
from app.workflow.service import WorkflowRuntime

log = logging.getLogger(__name__)
CHECKPOINT_SCHEMA = "langgraph"


def checkpoint_pool(settings: Settings) -> AsyncConnectionPool:
    url = make_url(settings.DATABASE_URL.get_secret_value()).set(drivername="postgresql")
    return AsyncConnectionPool(
        url.render_as_string(hide_password=False),
        min_size=1,
        max_size=settings.CHECKPOINT_POOL_MAX,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={CHECKPOINT_SCHEMA}",
        },
    )


def live_llm(settings: Settings) -> LLMProvider | None:
    try:
        return llm_from_settings(settings)
    except ProviderUnavailable:
        log.info("Groq live calls are disabled: intake asks every question, synthesis degrades")
        return None


async def build_runtime(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> WorkflowRuntime:
    pool = checkpoint_pool(settings)
    await pool.open(wait=False)
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    sync_sessions = create_sync_sessionmaker(create_sync_engine(settings))
    reports = PostgresReportStore(sync_sessions)
    deps = GraphDeps.from_settings(
        settings,
        llm=live_llm(settings),
        retriever=retriever_from_settings(settings, sessions=sync_sessions),
        corpus=PostgresCorpusReader(sync_sessions, JsonCache.from_settings(settings)),
        reports=reports,
    )
    return WorkflowRuntime(
        graph=build_graph(deps, checkpointer=saver),
        deps=deps,
        sessions=sessions,
        reports=reports,
        lease_s=settings.RUN_LEASE_S,
        heartbeat_s=settings.RUN_HEARTBEAT_S,
        session_ttl=timedelta(hours=settings.SESSION_TTL_H),
        query_max_chars=settings.QUERY_MAX_CHARS,
        on_close=[pool.close],
    )
