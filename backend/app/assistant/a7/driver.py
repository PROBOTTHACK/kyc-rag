"""Drive scripted conversations through the real `AssistantService` and check expectations
against the customer-facing API answer (black box: no internal state is read here)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.assistant.a7.datasets import Expect, Turn
from app.assistant.profile import Slot
from app.assistant.service import AssistantService

SLOTS = tuple(s.value for s in Slot)


def profile_values(answer: dict[str, Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for slot in SLOTS:
        p = (answer.get("profile") or {}).get(slot) or {}
        out[slot] = p.get("value") if p.get("status") == "known" else None
    return out


def note_codes(answer: dict[str, Any]) -> list[str]:
    return [m.get("code", "") for m in answer.get("messages") or []]


def visible_text(answer: dict[str, Any]) -> str:
    """What the customer reads for this answer (reply, notes, fixed texts)."""
    parts = [answer.get("reply") or ""]
    parts += [m.get("text", "") for m in answer.get("messages") or []]
    parts += [answer.get("fallback") or "", answer.get("explanation") or ""]
    return "\n".join(p for p in parts if p)


def check(expect: Expect, answer: dict[str, Any], before: dict[str, Any] | None) -> list[str]:
    """The failed expectations of one turn (empty = pass)."""
    f: list[str] = []
    kind = answer.get("kind")
    codes = note_codes(answer)
    prof = profile_values(answer)
    q = (answer.get("question") or {}).get("slot")
    conf = answer.get("confirmation") or {}
    offer = answer.get("scenario_offer")
    text = visible_text(answer).lower()
    if expect.kind is not None and kind != expect.kind:
        f.append(f"kind {kind!r} != {expect.kind!r}")
    if expect.question is not None and q != expect.question:
        f.append(f"question {q!r} != {expect.question!r}")
    if expect.confirm is not None and (conf.get("slot"), conf.get("value")) != tuple(
        expect.confirm
    ):
        f.append(f"confirm {(conf.get('slot'), conf.get('value'))} != {tuple(expect.confirm)}")
    if expect.offer is not None and (offer is not None) != expect.offer:
        f.append(f"offer present={offer is not None} expected {expect.offer}")
    if expect.offer_actions is not None:
        got = tuple(o.get("action") for o in (offer or {}).get("options", []))
        if got != tuple(expect.offer_actions):
            f.append(f"offer actions {got} != {tuple(expect.offer_actions)}")
    for c in expect.notes_include:
        if c not in codes:
            f.append(f"note {c!r} missing (got {codes})")
    for c in expect.notes_exclude:
        if c in codes:
            f.append(f"note {c!r} present")
    for slot, value in expect.profile.items():
        if prof.get(slot) != value:
            f.append(f"profile {slot}={prof.get(slot)!r} != {value!r}")
    if expect.profile_unchanged and before is not None and prof != profile_values(before):
        f.append(f"profile changed: {profile_values(before)} -> {prof}")
    if expect.fallback is not None and bool(answer.get("fallback")) != expect.fallback:
        f.append(f"fallback present={bool(answer.get('fallback'))} expected {expect.fallback}")
    if expect.checklist is not None and (answer.get("checklist") is not None) != expect.checklist:
        f.append(f"checklist present={answer.get('checklist') is not None}")
    for s in expect.reply_has:
        if s.lower() not in text:
            f.append(f"text lacks {s!r}")
    for s in expect.reply_lacks:
        if s.lower() in text:
            f.append(f"text contains {s!r}")
    if expect.scenarios is not None and len(answer.get("scenarios") or []) != expect.scenarios:
        f.append(f"scenarios {len(answer.get('scenarios') or [])} != {expect.scenarios}")
    if expect.blocked is not None and (kind == "blocked") != expect.blocked:
        f.append(f"blocked={kind == 'blocked'} expected {expect.blocked}")
    return f


@dataclass
class TurnResult:
    index: int
    input: str
    answer: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    checked: bool = False


@dataclass
class ConversationRun:
    session_id: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(not t.failures for t in self.turns)


def describe(turn: Turn) -> str:
    if turn.reload:
        return "[reload]"
    if turn.restart:
        return "[restart]"
    return json.dumps(turn.body(), ensure_ascii=False)


async def run_turns(
    service: AssistantService,
    turns: tuple[Turn, ...],
    *,
    restart: Callable[[], AssistantService] | None = None,
    on_turn: Callable[[int, AssistantService, str], Any] | None = None,
) -> ConversationRun:
    """Run one scripted conversation in a fresh session. `restart` builds a new service on
    the same database; `on_turn(i, service, sid)` runs after each turn (memory probes)."""
    created = await service.create_session()
    sid = created["session_id"]
    run = ConversationRun(sid)
    previous: dict[str, Any] = created["answer"]
    for i, turn in enumerate(turns, 1):
        if turn.restart:
            if restart is None:
                raise ValueError("a restart turn needs a restart factory")
            service = restart()
        if turn.reload or turn.restart:
            answer = (await service.get_session(sid))["answer"]
        else:
            body = turn.body()
            out = await service.submit_turn(
                sid, f"a7-{uuid.uuid4().hex}", json.dumps(body).encode("utf-8")
            )
            answer = out["answer"]
        result = TurnResult(i, describe(turn), answer)
        expect = turn.expect
        if expect != Expect():
            result.checked = True
            result.failures = check(expect, answer, previous)
        run.turns.append(result)
        if on_turn is not None:
            await on_turn(i, service, sid)
        previous = answer
    return run
