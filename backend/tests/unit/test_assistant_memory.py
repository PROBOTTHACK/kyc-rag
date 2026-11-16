"""Conversation memory (memory.py): history, rolling summary and the LLM context.

PostgreSQL turns are the history (here: the in-memory store with the same contract). The
context keeps recent turns verbatim within a token budget, condenses older ones into a stored
rolling summary, never includes a blocked message, and is rebuilt identically after a reload
or a restart (it depends only on the stored state and turns).
"""

import json
from typing import Any

from app.assistant.checklist import ChecklistService
from app.assistant.memory import ConversationSummary, assemble, history_turn
from app.assistant.profile import Slot
from app.assistant.service import AssistantService
from app.assistant.store import TurnRecord
from app.assistant.wording import wording
from app.llm.fake import scripted_llm
from tests.unit.test_assistant_api import MemoryStore
from tests.unit.test_assistant_checklist import FakeChunks

PAN = "ABCPE1234F"


def svc(store: MemoryStore | None = None, **kwargs: Any) -> AssistantService:
    return AssistantService(
        store or MemoryStore(), lambda: ChecklistService(FakeChunks()), None, **kwargs
    )


_n = iter(range(10**6))


async def send(s: AssistantService, sid: str, body: dict[str, Any]) -> dict[str, Any]:
    out = await s.submit_turn(sid, f"k{next(_n)}", json.dumps(body).encode())
    return out["answer"]  # type: ignore[no-any-return]


def choose(slot: str, value: str) -> dict[str, Any]:
    return {"action": "choose", "slot": slot, "value": value}


async def context_now(s: AssistantService, sid: str) -> Any:
    session = s.store.sessions[__import__("uuid").UUID(sid)]  # type: ignore[attr-defined]
    turns = list(session.turns)
    previous = ConversationSummary.model_validate(session.summary[0]) if session.summary else None
    after = previous.through_turn if previous else 0
    return assemble(
        session.state,
        [t for t in turns if t.turn_no > after],
        previous,
        wording(),
        budget_tokens=s.context_tokens,
        max_recent=s.recent_turns,
    )


def test_a_choice_turn_reads_as_its_label_and_a_blocked_turn_is_skipped() -> None:
    w = wording()
    record = TurnRecord(
        2, None, (), {"action": "choose", "slot": "account_type", "value": "SAVINGS"},
        {"kind": "question", "reply": "Who is the account for?"},
    )  # fmt: skip
    h = history_turn(record, w)
    assert h is not None and h.customer == "Savings account"
    assert h.assistant == "Who is the account for?"
    blocked = TurnRecord(3, None, ("pan",), {"action": None}, {"kind": "blocked"})
    assert history_turn(blocked, w) is None


async def test_recent_turns_are_complete_ordered_and_within_the_budget() -> None:
    s = svc(context_tokens=300, recent_turns=4)
    sid = (await s.create_session())["session_id"]
    for message in ["Hi there", "savings please", "it is for me, I live in Dubai"]:
        await send(s, sid, {"message": message})
    context, _ = await context_now(s, sid)
    turns = [t.turn for t in context.recent]
    assert turns == sorted(turns) and len(turns) <= 4
    assert context.tokens > 0 and context.tokens <= 300 + 400  # recent + profile parts
    for t in context.recent:  # never a cut message
        assert t.customer is None or t.customer in {
            "Hi there", "savings please", "it is for me, I live in Dubai",
            "Which account would you like to open?",
        }  # fmt: skip


async def test_older_turns_move_into_a_stored_rolling_summary() -> None:
    store = MemoryStore()
    s = svc(store, context_tokens=200, recent_turns=2)
    sid = (await s.create_session())["session_id"]
    messages = ["Hello", "I want a current account", "for my LLP", "what is Form 60?", "thanks"]
    for m in messages:
        await send(s, sid, {"message": m})
    stored = store.sessions[__import__("uuid").UUID(sid)].summary
    assert stored is not None
    summary = ConversationSummary.model_validate(stored[0])
    assert summary.through_turn >= 1
    assert "Hello" in summary.earlier_messages  # an early, complete message is remembered
    assert summary.confirmed["account type"] == "Current account"
    assert summary.confirmed["customer category"] == "A limited liability partnership (LLP)"


async def test_a_correction_supersedes_the_old_value_in_the_summary() -> None:
    store = MemoryStore()
    s = svc(store, context_tokens=200, recent_turns=1)
    sid = (await s.create_session())["session_id"]
    await send(s, sid, choose("account_type", "SAVINGS"))
    await send(s, sid, choose("customer_category", "NRI_INDIVIDUAL"))
    await send(s, sid, {"action": "correct", "slot": "account_type", "value": "CURRENT"})
    await send(s, sid, {"message": "thanks"})
    summary = ConversationSummary.model_validate(
        store.sessions[__import__("uuid").UUID(sid)].summary[0]  # type: ignore[index]
    )
    assert summary.confirmed["account type"] == "Current account"
    assert any("Savings account (replaced by Current account)" in c for c in summary.corrected)


async def test_a_blocked_message_never_reaches_the_history_summary_or_llm() -> None:
    store = MemoryStore()
    llm = scripted_llm(*(['{"reply": "ok"}'] * 20), *(['{"mentions": []}'] * 20))
    s = AssistantService(
        store, lambda: ChecklistService(FakeChunks()), llm, context_tokens=200, recent_turns=1
    )
    sid = (await s.create_session())["session_id"]
    await send(s, sid, {"message": f"my PAN is {PAN}"})
    for m in ["hello", "a savings account", "thanks", "ok"]:
        await send(s, sid, {"message": m})
    session = store.sessions[__import__("uuid").UUID(sid)]
    assert PAN not in json.dumps(session.summary)
    context, _ = await context_now(s, sid)
    assert PAN not in context.render()
    assert all(PAN not in r.user for r in llm.requests)


async def test_the_context_is_rebuilt_identically_after_a_restart() -> None:
    store = MemoryStore()
    first = svc(store, context_tokens=400, recent_turns=3)
    sid = (await first.create_session())["session_id"]
    for m in ["hi", "current account", "for my LLP"]:
        await send(first, sid, {"message": m})
    before, _ = await context_now(first, sid)
    restarted = svc(store, context_tokens=400, recent_turns=3)  # a new process, same database
    after, _ = await context_now(restarted, sid)
    assert before.render() == after.render()
    assert after.profile["customer category"] == "A limited liability partnership (LLP)"


async def test_sessions_never_share_memory() -> None:
    store = MemoryStore()
    s = svc(store)
    a = (await s.create_session())["session_id"]
    b = (await s.create_session())["session_id"]
    await send(s, a, {"message": "a current account for my LLP"})
    await send(s, b, {"message": "hello"})
    context_b, _ = await context_now(s, b)
    assert "LLP" not in context_b.render()
    assert Slot  # imported for the profile keys used above


async def test_a_deleted_conversation_leaves_no_summary() -> None:
    store = MemoryStore()
    s = svc(store, context_tokens=200, recent_turns=1)
    sid = (await s.create_session())["session_id"]
    for m in ["hello", "current account", "thanks"]:
        await send(s, sid, {"message": m})
    del store.sessions[__import__("uuid").UUID(sid)]  # the cascade delete of the session row
    view = None
    try:
        view = await s.get_session(sid)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "SESSION_NOT_FOUND"
    assert view is None
