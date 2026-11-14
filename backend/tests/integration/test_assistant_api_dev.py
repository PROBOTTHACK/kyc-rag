"""A5 on the development stack: `/api/v2/assistant` with the Postgres store and the real
checklist (stored HDFC chunks, dense retriever).

Conversations are stored in a throwaway database (`temp_db`, migrated to head); checklist
evidence is read from the development database, read-only. Covers migration 0009, the
insert-only turns table, idempotency under concurrency, screening at rest, and a complete
NRI savings conversation whose citations resolve to stored text.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from typing import Any, TypeVar

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.assistant.service import AssistantService, LazyChecklist
from app.assistant.store import PostgresAssistantStore
from app.config import Settings, get_settings
from app.core.loop import selector_loop_factory
from app.main import create_app
from tests.integration.conftest import alembic_cfg, sync_engine

pytestmark = pytest.mark.integration

T = TypeVar("T")
PAN = "ABCPE1234F"
BASE = "/api/v2/assistant/sessions"
TABLES = {"assistant_sessions", "assistant_turns", "assistant_idempotency"}


def run(coro: Coroutine[Any, Any, T]) -> T:
    with asyncio.Runner(loop_factory=selector_loop_factory) as runner:  # psycopg on Windows
        return runner.run(coro)


@pytest.fixture(scope="module")
def checklist() -> Iterator[LazyChecklist]:
    """Built from the development settings BEFORE `temp_db` repoints DATABASE_URL."""
    lazy = LazyChecklist(get_settings())
    yield lazy
    lazy.close()


@pytest.fixture
def db(temp_db: str) -> str:
    command.upgrade(alembic_cfg(), "head")
    return temp_db


Client = Callable[[httpx.AsyncClient], Awaitable[T]]


async def with_client(url: str, checklist: Any, fn: Client[T]) -> T:
    engine = create_async_engine(url, poolclass=sa.pool.NullPool)
    try:
        app = create_app(Settings(_env_file=None, DATABASE_URL="x", REDIS_URL="y"))  # type: ignore[call-arg]
        store = PostgresAssistantStore(async_sessionmaker(engine, expire_on_commit=False))
        app.state.assistant = AssistantService(store, checklist, None)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await fn(c)
    finally:
        await engine.dispose()


async def post_turn(c: httpx.AsyncClient, sid: str, key: str, **body: Any) -> httpx.Response:
    return await c.post(
        f"{BASE}/{sid}/turns", content=json.dumps(body), headers={"Idempotency-Key": key}
    )


def rows(url: str, sql: str, **params: Any) -> list[dict[str, Any]]:
    with sync_engine(url).connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]


def no_checklist() -> Any:
    raise AssertionError("no checklist is built in this test")


# --- migration -----------------------------------------------------------------------------------


def test_0009_is_additive_and_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    command.upgrade(cfg, "0008_m4_review_cases")
    before = set(sa.inspect(sync_engine(temp_db)).get_table_names())
    assert not TABLES & before
    command.upgrade(cfg, "head")
    assert set(sa.inspect(sync_engine(temp_db)).get_table_names()) == before | TABLES
    command.check(cfg)
    command.downgrade(cfg, "0008_m4_review_cases")
    assert set(sa.inspect(sync_engine(temp_db)).get_table_names()) == before
    fn = "SELECT count(*) AS n FROM pg_proc WHERE proname = 'assistant_turns_reject_update'"
    assert rows(temp_db, fn) == [{"n": 0}]


# --- a full conversation -------------------------------------------------------------------------


def test_a_full_nri_savings_conversation_is_stored_and_cited(
    checklist: LazyChecklist, db: str
) -> None:
    async def converse(c: httpx.AsyncClient) -> tuple[str, dict[str, Any], str]:
        sid = (await c.post(BASE)).json()["session_id"]
        blocked = await post_turn(c, sid, "pii", message=f"my PAN is {PAN}")
        assert blocked.json()["answer"]["kind"] == "blocked" and PAN not in blocked.text
        await post_turn(c, sid, "a", action="choose", slot="account_type", value="SAVINGS")
        r = await post_turn(
            c, sid, "c", action="choose", slot="customer_category", value="NRI_INDIVIDUAL"
        )
        a = r.json()["answer"]
        for i in range(6):
            if a["kind"] != "question":
                break
            slot = a["question"]["slot"]
            a = (await post_turn(c, sid, f"n{i}", action="not_sure", slot=slot)).json()["answer"]
        view = await c.get(f"{BASE}/{sid}")
        return sid, a, view.text

    sid, a, view = run(with_client(db, checklist, converse))
    assert a["kind"] == "checklist" and a["coverage_state"] == "COVERED"
    items = [i for g in a["checklist"]["groups"].values() for i in g]
    assert items and all(i["classification"] != "policy_supported" for i in items)
    for item in items:
        for cite in item["citations"]:
            assert cite["document_key"] == "hdfc_nri_account_opening", item["id"]
        if item["source_wording"]:
            assert any(item["source_wording"] in c["passage"] for c in item["citations"])
    assert "chunk_id" not in json.dumps(a)

    turns = rows(db, "SELECT turn_no, message, screen_categories FROM assistant_turns "
                     "WHERE session_id = :s ORDER BY turn_no", s=sid)  # fmt: skip
    assert [t["turn_no"] for t in turns] == list(range(len(turns)))
    assert turns[1]["message"] is None and "pan" in turns[1]["screen_categories"]
    session = rows(db, "SELECT turn_count FROM assistant_sessions WHERE id = :s", s=sid)
    assert session == [{"turn_count": len(turns) - 1}]
    dump = rows(db, "SELECT string_agg(t::text, ' ') AS all FROM assistant_turns t")[0]["all"]
    dump += rows(db, "SELECT string_agg(s::text, ' ') AS all FROM assistant_sessions s")[0]["all"]
    assert PAN not in dump and PAN not in view


# --- storage guarantees --------------------------------------------------------------------------


def test_turns_are_insert_only_and_go_with_their_session(db: str) -> None:
    async def one_turn(c: httpx.AsyncClient) -> str:
        sid = (await c.post(BASE)).json()["session_id"]
        await post_turn(c, sid, "k", message="current account")
        return str(sid)

    sid = run(with_client(db, no_checklist, one_turn))
    eng = sync_engine(db)
    with pytest.raises(DBAPIError, match="insert-only"), eng.begin() as conn:
        conn.execute(sa.text("UPDATE assistant_turns SET message = 'x' WHERE session_id = :s"),
                     {"s": sid})  # fmt: skip
    with eng.begin() as conn:
        conn.execute(sa.text("DELETE FROM assistant_sessions WHERE id = :s"), {"s": sid})
    left = rows(
        db,
        "SELECT (SELECT count(*) FROM assistant_turns WHERE session_id = :s) AS turns, "
        "(SELECT count(*) FROM assistant_idempotency WHERE session_id = :s) AS keys",
        s=sid,
    )
    assert left == [{"turns": 0, "keys": 0}]


def test_concurrent_requests_with_one_key_store_one_turn(db: str) -> None:
    async def race(c: httpx.AsyncClient) -> tuple[str, list[httpx.Response]]:
        sid = (await c.post(BASE)).json()["session_id"]
        body = {"action": "choose", "slot": "account_type", "value": "CURRENT"}
        rs = await asyncio.gather(*(post_turn(c, sid, "same", **body) for _ in range(4)))
        return str(sid), list(rs)

    sid, rs = run(with_client(db, no_checklist, race))
    assert {r.status_code for r in rs} == {200} and len({r.content for r in rs}) == 1
    n = rows(db, "SELECT count(*) AS n FROM assistant_turns WHERE session_id = :s", s=sid)
    assert n == [{"n": 2}]  # the opening question + one turn


def test_an_unknown_session_in_postgres_is_not_found(db: str) -> None:
    async def missing(c: httpx.AsyncClient) -> list[httpx.Response]:
        sid = str(uuid.uuid4())
        return [await c.get(f"{BASE}/{sid}"), await post_turn(c, sid, "k", message="hi")]

    for r in run(with_client(db, no_checklist, missing)):
        assert (r.status_code, r.json()["error"]["code"]) == (404, "SESSION_NOT_FOUND")
