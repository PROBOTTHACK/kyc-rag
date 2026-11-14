"""A5 unit tests: `/api/v2/assistant` end to end over an in-memory store (no database, no
LLM, fake stored chunks from the A4 tests).

Covers the golden conversations through the API, screening (identifiers refused, never
echoed or stored), idempotency, session errors, the technical-error path, what the answer
must never contain, and the documented contract."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from app.api.openapi import spec
from app.api.v2.assistant import router as assistant_router
from app.api.v2.schemas import (
    AssistantSessionStarted,
    AssistantSessionView,
    AssistantTurnRequest,
    AssistantTurnResult,
)
from app.assistant.catalogue import catalogue
from app.assistant.checklist import ChecklistService
from app.assistant.dialogue import TurnInput, opening, respond_deterministic
from app.assistant.profile import DialogueState
from app.assistant.service import SESSION_TTL, AssistantService
from app.assistant.store import Replay, StoredSession, TurnRecord
from app.assistant.wording import wording
from app.config import Settings
from app.main import create_app
from tests.unit.test_assistant_checklist import FakeChunks

PAN = "ABCPE1234F"
NOTICE = wording().notice


# --- an in-memory store with the same contract as PostgresAssistantStore --------------------------


@dataclass
class _Session:
    state: DialogueState
    expires_at: datetime
    turns: list[TurnRecord] = field(default_factory=list)
    keys: dict[str, tuple[str, int]] = field(default_factory=dict)
    summary: tuple[dict[str, Any], int] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _MemoryTurn:
    def __init__(self, sid: uuid.UUID, s: _Session | None) -> None:
        self._sid, self._s = sid, s

    @property
    def session(self) -> StoredSession | None:
        s = self._s
        if s is None:
            return None
        summary, through = s.summary if s.summary else (None, None)
        return StoredSession(self._sid, s.state, s.expires_at, (), summary, through)

    async def replay(self, key: str) -> Replay | None:
        assert self._s is not None
        found = self._s.keys.get(key)
        if found is None:
            return None
        sha, turn_no = found
        turn = next(t for t in self._s.turns if t.turn_no == turn_no)
        return Replay(sha, turn_no, turn.answer)

    async def turns_after(self, turn_no: int, limit: int) -> list[TurnRecord]:
        assert self._s is not None
        return [t for t in self._s.turns if t.turn_no > turn_no][-limit:]

    async def save(
        self,
        state: DialogueState,
        record: TurnRecord,
        key: str,
        sha: str,
        expires_at: datetime,
        summary: tuple[dict[str, Any], int] | None = None,
    ) -> None:
        assert self._s is not None
        assert all(t.turn_no != record.turn_no for t in self._s.turns)  # UNIQUE(session, turn)
        self._s.turns.append(record)
        self._s.keys[key] = (sha, record.turn_no)
        self._s.state, self._s.expires_at = state, expires_at
        if summary is not None:
            self._s.summary = summary


class MemoryStore:
    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, _Session] = {}

    async def create(
        self, sid: uuid.UUID, state: DialogueState, opening: TurnRecord, expires_at: datetime
    ) -> None:
        self.sessions[sid] = _Session(state, expires_at, [opening])

    async def load(self, sid: uuid.UUID) -> StoredSession | None:
        s = self.sessions.get(sid)
        if s is None:
            return None
        summary, through = s.summary if s.summary else (None, None)
        return StoredSession(sid, s.state, s.expires_at, tuple(s.turns), summary, through)

    @asynccontextmanager
    async def _turn(self, sid: uuid.UUID) -> AsyncIterator[_MemoryTurn]:
        s = self.sessions.get(sid)
        if s is None:
            yield _MemoryTurn(sid, None)
            return
        async with s.lock:
            yield _MemoryTurn(sid, s)

    def turn(self, sid: uuid.UUID) -> AbstractAsyncContextManager[_MemoryTurn]:
        return self._turn(sid)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    client: httpx.AsyncClient
    store: MemoryStore
    clock: Clock
    chunks: FakeChunks


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    store, clock, chunks = MemoryStore(), Clock(), FakeChunks()
    app = create_app(Settings(_env_file=None, DATABASE_URL="x", REDIS_URL="y"))  # type: ignore[call-arg]
    app.state.assistant = AssistantService(
        store, lambda: ChecklistService(chunks), None, clock=clock
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Env(client, store, clock, chunks)


BASE = "/api/v2/assistant/sessions"
_keys = iter(range(10**9))


async def start(env: Env) -> str:
    r = await env.client.post(BASE)
    assert r.status_code == 201, r.text
    return str(r.json()["session_id"])


async def turn(env: Env, sid: str, key: str | None = None, **body: Any) -> httpx.Response:
    key = key or f"k{next(_keys)}"
    return await env.client.post(
        f"{BASE}/{sid}/turns", content=json.dumps(body), headers={"Idempotency-Key": key}
    )


async def answer(env: Env, sid: str, **body: Any) -> dict[str, Any]:
    r = await turn(env, sid, **body)
    assert r.status_code == 200, r.text
    return r.json()["answer"]  # type: ignore[no-any-return]


async def choose(env: Env, sid: str, slot: str, value: str) -> dict[str, Any]:
    return await answer(env, sid, action="choose", slot=slot, value=value)


async def until_checklist(env: Env, sid: str, a: dict[str, Any]) -> dict[str, Any]:
    """Answer 'I'm not sure' to every remaining (non-essential) question."""
    for _ in range(6):
        if a["kind"] != "question":
            return a
        a = await answer(env, sid, action="not_sure", slot=a["question"]["slot"])
    raise AssertionError("no checklist after six questions")


def walk_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_keys(v)


INTERNAL_KEYS = {"chunk_id", "diagnostics", "reason", "score", "llm_consulted", "ready", "step"}


def assert_customer_safe(payload: Any) -> None:
    keys = set(walk_keys(payload))
    assert not keys & INTERNAL_KEYS, keys & INTERNAL_KEYS
    text = json.dumps(payload).lower()
    for word in ("corpus", "a0 amendment", "§", "ruling"):
        assert word not in text, word


# --- conversations -------------------------------------------------------------------------------


async def test_a_new_session_opens_with_the_account_type_question(env: Env) -> None:
    r = await env.client.post(BASE)
    body = r.json()
    assert r.status_code == 201 and body["turn"] == 0
    a = body["answer"]
    assert a["kind"] == "question" and a["question"]["slot"] == "account_type"
    assert {c["value"] for c in a["question"]["chips"]} >= {"SAVINGS", "CURRENT"}
    assert a["notice"] == NOTICE
    AssistantSessionStarted.model_validate(body)


async def test_llp_current_account_and_a_pan_card_question(env: Env) -> None:
    sid = await start(env)
    await choose(env, sid, "account_type", "CURRENT")
    a = await until_checklist(env, sid, await choose(env, sid, "customer_category", "LLP"))
    assert a["kind"] == "checklist" and a["coverage_state"] == "COVERED_OCR"
    groups = a["checklist"]["groups"]
    assert groups["documents"] or groups["information"]
    assert all(i["classification"] != "policy_supported" for g in groups.values() for i in g)
    assert groups["not_confirmed"] == []  # Group C only when asked

    a = await answer(env, sid, message="Do I need to give a copy of the PAN card?")
    assert a["kind"] == "checklist", a
    c = a["checklist"]["groups"]["not_confirmed"]
    assert [i["id"] for i in c] == ["org_entity_pan_card_copy"]
    assert c[0]["classification"] == "unconfirmed" and c[0]["citations"] == []
    assert "do not confirm whether a copy" in c[0]["does_not_establish"]
    assert_customer_safe(a)


async def test_nri_savings_from_free_text_with_citations_and_the_notice(env: Env) -> None:
    sid = await start(env)
    a = await answer(env, sid, message="I live in Dubai and want to open a savings account")
    seen = [a]
    for _ in range(6):
        if a["kind"] == "confirm":
            a = await answer(env, sid, action="confirm_yes")
        elif a["kind"] == "question":
            a = await answer(env, sid, action="not_sure", slot=a["question"]["slot"])
        else:
            break
        seen.append(a)
    assert a["kind"] == "checklist" and a["coverage_state"] == "COVERED"
    assert a["profile"]["customer_category"]["value"] == "NRI_INDIVIDUAL"
    assert a["profile"]["account_type"]["value"] == "SAVINGS"
    item = a["checklist"]["groups"]["documents"][0]
    cite = item["citations"][0]
    assert cite["document_key"] == "hdfc_nri_account_opening" and cite["page"] >= 1
    assert cite["passage"]
    assert all(x["notice"] == NOTICE for x in seen)
    for x in seen:
        assert_customer_safe(x)


async def test_a_combination_the_documents_do_not_cover_gets_the_fallback(env: Env) -> None:
    sid = await start(env)
    await choose(env, sid, "account_type", "SAVINGS")
    a = await choose(env, sid, "customer_category", "RESIDENT_INDIVIDUAL")
    assert a["kind"] == "fallback" and a["coverage_state"] == "NOT_ESTABLISHED"
    assert a["fallback"] == wording().fallback and a["checklist"] is None
    assert_customer_safe(a)  # the internal not-established reason is never sent


async def test_the_conversion_request_builds_its_own_checklist(env: Env) -> None:
    sid = await start(env)
    a = await answer(env, sid, message="I am moving abroad and want to convert my account to NRO")
    for _ in range(4):
        if a["kind"] != "confirm":
            break
        a = await answer(env, sid, action="confirm_yes")
    assert a["kind"] == "checklist" and a["coverage_state"] == "CONVERSION_ONLY", a
    keys = {c["document_key"] for g in a["checklist"]["groups"].values() for i in g
            for c in i["citations"]}  # fmt: skip
    assert keys == {"hdfc_resident_to_nro_conversion"}


async def test_the_api_follows_the_dialogue_exactly(env: Env) -> None:
    """The service adds no policy: its states match the A3 dialogue run directly."""
    sid = await start(env)
    state, _ = opening()
    steps: list[dict[str, Any]] = [
        {"message": "current account please"},
        {"action": "choose", "slot": "customer_category", "value": "COMPANY"},
    ]
    for body in steps:
        state, _ = respond_deterministic(state, TurnInput.model_validate(body))
        await answer(env, sid, **body)
    assert env.store.sessions[uuid.UUID(sid)].state == state


# --- screening -----------------------------------------------------------------------------------


async def test_an_identifier_is_refused_and_never_echoed_or_stored(env: Env) -> None:
    sid = await start(env)
    r = await turn(env, sid, message=f"my PAN is {PAN}, what do I need?")
    assert r.status_code == 200 and PAN not in r.text
    a = r.json()["answer"]
    assert a["kind"] == "blocked" and a["messages"][-1]["code"] == "blocked"
    stored = env.store.sessions[uuid.UUID(sid)]
    assert stored.turns[-1].message is None and "pan" in stored.turns[-1].screen_categories
    assert PAN not in repr(stored)
    view = await env.client.get(f"{BASE}/{sid}")
    assert PAN not in view.text


async def test_a_clean_message_is_stored_screened(env: Env) -> None:
    sid = await start(env)
    await turn(env, sid, message="savings account, email me at a@b.co")
    msg = env.store.sessions[uuid.UUID(sid)].turns[-1].message
    assert msg is not None and "a@b.co" not in msg and "[REDACTED_EMAIL]" in msg


# --- idempotency ---------------------------------------------------------------------------------


async def test_the_same_key_replays_the_same_answer_once(env: Env) -> None:
    sid = await start(env)
    first = await turn(env, sid, key="same", action="choose", slot="account_type", value="CURRENT")
    again = await turn(env, sid, key="same", action="choose", slot="account_type", value="CURRENT")
    assert first.status_code == again.status_code == 200 and first.content == again.content
    assert len(env.store.sessions[uuid.UUID(sid)].turns) == 2  # the opening + one turn


async def test_a_reused_key_with_another_request_is_a_conflict(env: Env) -> None:
    sid = await start(env)
    await turn(env, sid, key="k", action="choose", slot="account_type", value="CURRENT")
    r = await turn(env, sid, key="k", action="choose", slot="account_type", value="SAVINGS")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "IDEMPOTENCY_MISMATCH")


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "x" * 201}, {"Idempotency-Key": ""}])
async def test_a_turn_needs_an_idempotency_key(env: Env, headers: dict[str, str]) -> None:
    sid = await start(env)
    r = await env.client.post(f"{BASE}/{sid}/turns", content=b'{"message": "hi"}', headers=headers)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")


# --- errors --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"{SECRET-WORD",
        b'["SECRET-WORD"]',
        b"",
        b'{"message": "SECRET-WORD", "extra": 1}',
        b'{"action": "choose", "slot": "account_type", "value": "SECRET-WORD"}',
        b'{"action": "SECRET-WORD"}',
        b'{"message": 12345}',
        b'{"message": "' + b"a" * 17_000 + b'"}',
    ],
)
async def test_invalid_turns_are_rejected_without_echo(env: Env, content: bytes) -> None:
    sid = await start(env)
    r = await env.client.post(
        f"{BASE}/{sid}/turns", content=content, headers={"Idempotency-Key": "k"}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "VALIDATION_ERROR")
    assert b"SECRET-WORD" not in r.content
    assert len(env.store.sessions[uuid.UUID(sid)].turns) == 1


@pytest.mark.parametrize("sid", [str(uuid.uuid4()), "not-a-uuid"])
async def test_an_unknown_session_is_not_found(env: Env, sid: str) -> None:
    for r in (await env.client.get(f"{BASE}/{sid}"), await turn(env, sid, message="hi")):
        assert (r.status_code, r.json()["error"]["code"]) == (404, "SESSION_NOT_FOUND")
        assert b"not-a-uuid" not in r.content


async def test_an_idle_session_expires_and_activity_extends_it(env: Env) -> None:
    sid = await start(env)
    env.clock.now += SESSION_TTL - timedelta(minutes=1)
    assert (await turn(env, sid, message="savings")).status_code == 200
    env.clock.now += SESSION_TTL - timedelta(minutes=1)
    assert (await env.client.get(f"{BASE}/{sid}")).status_code == 200
    env.clock.now += timedelta(minutes=2)
    for r in (await env.client.get(f"{BASE}/{sid}"), await turn(env, sid, message="current")):
        assert (r.status_code, r.json()["error"]["code"]) == (410, "SESSION_EXPIRED")


async def test_a_storage_failure_gives_the_technical_message_and_stores_nothing(env: Env) -> None:
    sid = await start(env)
    await choose(env, sid, "account_type", "CURRENT")
    env.chunks.fail = True
    r = await turn(env, sid, key="t", action="choose", slot="customer_category", value="COMPANY")
    a = r.json()["answer"]
    assert r.status_code == 200 and a["kind"] == "technical_error"
    assert a["technical_message"] == catalogue().wording.technical_error
    assert "database" not in r.text.lower() and "ConnectionError" not in r.text
    s = env.store.sessions[uuid.UUID(sid)]
    assert len(s.turns) == 2 and s.state.value  # nothing stored for the failed turn
    env.chunks.fail = False
    r = await turn(env, sid, key="t", action="choose", slot="customer_category", value="COMPANY")
    assert r.json()["answer"]["kind"] == "checklist"  # the same request, sent again


async def test_a_failing_checklist_factory_also_gives_the_technical_message() -> None:
    def broken() -> ChecklistService:
        raise RuntimeError("model download failed")

    store = MemoryStore()
    svc = AssistantService(store, broken, None)
    sid = (await svc.create_session())["session_id"]
    await svc.submit_turn(sid, "a", b'{"action":"choose","slot":"account_type","value":"CURRENT"}')
    out = await svc.submit_turn(
        sid, "b", b'{"action":"choose","slot":"customer_category","value":"LLP"}'
    )
    assert out["answer"]["kind"] == "technical_error"


async def test_no_service_means_503() -> None:
    app = create_app(Settings(_env_file=None, DATABASE_URL="x", REDIS_URL="y"))  # type: ignore[call-arg]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(BASE)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "ASSISTANT_UNAVAILABLE")


# --- reload and the contract ---------------------------------------------------------------------


async def test_the_session_view_reloads_the_conversation(env: Env) -> None:
    sid = await start(env)
    await turn(env, sid, message="current account")
    last = await choose(env, sid, "customer_category", "TRUST")
    r = await env.client.get(f"{BASE}/{sid}")
    view = r.json()
    AssistantSessionView.model_validate(view)
    assert [t["turn"] for t in view["turns"]] == [0, 1, 2]
    assert view["turns"][0]["choice"] is None and view["turns"][1]["message"] == "current account"
    assert view["turns"][2]["choice"] == {
        "action": "choose", "slot": "customer_category", "value": "TRUST"
    }  # fmt: skip
    assert view["answer"] == last
    entry = dict(view["profile"]["customer_category"])
    entry.pop("choices")
    assert entry == {"status": "known", "value": "TRUST", "label": "A trust", "origin": "chosen"}


async def test_turn_responses_match_the_documented_model(env: Env) -> None:
    sid = await start(env)
    r = await turn(env, sid, message="fixed deposit for my company")
    AssistantTurnResult.model_validate(r.json())


def test_the_v2_routes_are_documented_and_parse_no_body() -> None:
    paths = spec()["paths"]
    expected = {
        ("post", "/api/v2/assistant/sessions"): ("201", "AssistantSessionStarted"),
        ("get", "/api/v2/assistant/sessions/{session_id}"): ("200", "AssistantSessionView"),
        ("post", "/api/v2/assistant/sessions/{session_id}/turns"): ("200", "AssistantTurnResult"),
    }
    for (method, path), (status, model) in expected.items():
        responses = paths[path][method]["responses"]
        ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert ref == f"#/components/schemas/{model}"
        errors = [s for s in responses if s.startswith(("4", "5"))]
        assert {"422", "503"} <= set(errors)
        for s in errors:
            ref = responses[s]["content"]["application/json"]["schema"]["$ref"]
            assert ref == "#/components/schemas/ErrorResponse"
    body = paths["/api/v2/assistant/sessions/{session_id}/turns"]["post"]["requestBody"]
    assert body["required"] is True
    assert "$ref" not in json.dumps(body) and "$defs" not in json.dumps(body)
    assert set(AssistantTurnRequest.model_fields) == set(TurnInput.model_fields)
    for route in assistant_router.routes:
        assert isinstance(route, APIRoute)
        assert route.body_field is None and route.response_model is None, route.path


def test_the_dialogue_state_survives_a_json_round_trip() -> None:
    state, _ = opening()
    for body in [
        {"message": "I live in London and need an NRE savings account"},
        {"action": "confirm_yes"},
        {"action": "not_sure", "slot": "holding"},
        {"message": "do I need a photograph?"},
    ]:
        state, _ = respond_deterministic(state, TurnInput.model_validate(body))
        again = DialogueState.model_validate(json.loads(json.dumps(state.model_dump(mode="json"))))
        assert again == state


def test_the_notice_is_the_same_in_the_dialogue_and_the_checklist() -> None:
    assert wording().notice == catalogue().wording.notice


# --- A5 close-out: session security and privacy ---------------------------------------------------


async def test_the_opening_answer_warns_against_real_personal_information(env: Env) -> None:
    a = (await env.client.post(BASE)).json()["answer"]
    first = a["messages"][0]
    assert first["code"] == "privacy" and first["text"] == wording().privacy
    assert "real personal or banking information" in first["text"]


async def test_session_ids_are_random_uuid4_and_distinct(env: Env) -> None:
    ids = [uuid.UUID(await start(env)) for _ in range(50)]
    assert len(set(ids)) == 50 and all(i.version == 4 for i in ids)


async def test_a_session_view_shows_only_its_own_conversation(env: Env) -> None:
    a, b = await start(env), await start(env)
    await turn(env, a, message="savings account for the Zebrafinch trust")
    await turn(env, b, message="fixed deposit please")
    view_b = (await env.client.get(f"{BASE}/{b}")).text
    assert a not in view_b and "Zebrafinch" not in view_b and "fixed deposit please" in view_b
    listing = await env.client.get(BASE)  # there is no way to list conversations
    assert listing.status_code == 405


def test_the_access_log_masks_assistant_session_ids() -> None:
    import logging

    from app.core.logging import MaskAssistantSessionIds

    sid = str(uuid.uuid4())
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "POST", f"/api/v2/assistant/sessions/{sid}/turns", "1.1", 200), None,
    )  # fmt: skip
    assert MaskAssistantSessionIds().filter(record)
    line = record.getMessage()
    assert sid not in line and "/api/v2/assistant/sessions/<session>/turns" in line
    legacy = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s", (f"/api/v1/sessions/{sid}",), None
    )
    MaskAssistantSessionIds().filter(legacy)
    assert sid in legacy.getMessage()  # the preserved v1 API is unchanged


def test_no_assistant_module_logs_messages_or_session_ids() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "app"
    files = [*(root / "assistant").glob("*.py"), *(root / "api" / "v2").glob("*.py")]
    for path in files:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "log"
            ):
                names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
                assert not names & {"message", "turn", "body", "sid", "raw_sid", "key"}, path


# --- A6 option A: valid choices on every profile slot ---------------------------------------------


async def test_every_profile_slot_offers_exactly_its_valid_choices(env: Env) -> None:
    from app.assistant.profile import SLOT_VALUES, Slot

    a = (await env.client.post(BASE)).json()["answer"]
    for slot in Slot:
        choices = a["profile"][slot.value]["choices"]
        values = [c["value"] for c in choices]
        assert len(values) == len(set(values))
        assert set(values) == {v.value for v in SLOT_VALUES[slot]}
        assert {c["value"]: c["label"] for c in choices} == wording().slots[slot].chips


async def test_a_change_uses_a_listed_choice_and_the_server_still_validates(env: Env) -> None:
    sid = await start(env)
    a = await answer(env, sid, message="NRI savings account")
    choices = [c["value"] for c in a["profile"]["account_type"]["choices"]]
    assert "CURRENT" in choices
    a = await answer(env, sid, action="correct", slot="account_type", value="CURRENT")
    assert a["profile"]["account_type"]["value"] == "CURRENT"
    assert any(m["code"] == "corrected" for m in a["messages"])
    r = await turn(env, sid, action="correct", slot="account_type", value="GOLD_ACCOUNT")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "VALIDATION_ERROR")
