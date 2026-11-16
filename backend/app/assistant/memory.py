"""Conversation memory: what the assistant remembers between turns.

PostgreSQL is the authoritative history (`assistant_turns`, one row per turn). This module
turns that history and the dialogue state into the context the LLM receives on every turn:

- the **confirmed profile** of the active account scenario (enum values only change when the
  customer chooses or confirms; the LLM never commits them);
- the **other scenarios** discussed in this conversation;
- the **pending task** (the question or confirmation waiting for an answer);
- the **recent turns**, verbatim and in order, within a token budget;
- a **rolling summary** of the older turns, stored on the session row and updated each turn.

The summary is built deterministically from the dialogue state and the stored turns, never
by an LLM, so it cannot turn an assumption into a fact:
`confirmed` (the state's known answers), `corrected` (superseded answers and what replaced
them), `unconfirmed` (a proposal still awaiting yes / no), `open_question`, `other_scenarios`
and `earlier_messages` (complete earlier customer messages, newest kept first when the budget
is short; a message is never cut).

Blocked turns are never part of the history, the summary or the context: their text was
never stored, and the turn is skipped entirely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.profile import DialogueState, PendingKind, Slot
from app.assistant.store import TurnRecord
from app.assistant.wording import Wording
from app.core.ratelimit import CHARS_PER_TOKEN

# How a choice turn reads in the history (the customer's side).
CHOICE_LABELS = {
    "not_sure": "I'm not sure",
    "skip": "Skip this question",
    "explain": "What does this mean?",
    "confirm_yes": "Yes",
    "confirm_no": "No",
    "new_scenario": "Ask about a separate account",
    "change_application": "Change this application",
    "switch_scenario": "Go back to the earlier account",
}
EARLIER_MESSAGES_MAX = 30


def tokens(text: str) -> int:
    """A conservative estimate (the rate limiter's characters-per-token rule)."""
    return -(-len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class HistoryTurn:
    turn: int
    customer: str | None  # the screened message, or the label of the choice made
    assistant: str | None  # what the assistant said (its reply, or the fixed texts)

    def as_dict(self) -> dict[str, Any]:
        return {"turn": self.turn, "customer": self.customer, "assistant": self.assistant}


def history_turn(record: TurnRecord, words: Wording) -> HistoryTurn | None:
    """One stored turn as conversation history, or None when it must not be remembered (a
    blocked message: nothing of it may reach the history, the summary or the LLM)."""
    answer = record.answer or {}
    if answer.get("kind") == "blocked":
        return None
    customer = record.message
    if customer is None and record.request:
        action = record.request.get("action")
        slot, value = record.request.get("slot"), record.request.get("value")
        if action in ("choose", "correct") and slot and value:
            try:
                customer = words.label(Slot(slot), value)
            except (KeyError, ValueError):
                customer = None
        else:
            customer = CHOICE_LABELS.get(str(action))
    parts = [answer.get("reply")]
    if not answer.get("reply"):
        question = answer.get("question") or {}
        confirmation = answer.get("confirmation") or {}
        parts = [m.get("text") for m in answer.get("messages", []) if m.get("code") != "privacy"]
        parts += [answer.get("fallback"), confirmation.get("text"), question.get("text")]
    elif answer.get("fallback"):
        parts += [answer.get("fallback"), answer.get("explanation")]
    assistant = " ".join(p for p in parts if p) or None
    return HistoryTurn(record.turn_no, customer, assistant)


# --- the rolling summary -------------------------------------------------------------------------


class ConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    through_turn: int = Field(ge=0)
    confirmed: dict[str, str] = Field(default_factory=dict)  # slot -> answer (active scenario)
    corrected: list[str] = Field(default_factory=list)  # "Account type: X, now Y"
    unconfirmed: list[str] = Field(default_factory=list)  # proposals awaiting yes / no
    open_question: str | None = None
    other_scenarios: list[str] = Field(default_factory=list)
    earlier_messages: list[str] = Field(default_factory=list)  # complete, oldest first


def _slot_name(slot: Slot) -> str:
    return slot.value.replace("_", " ")


def profile_facts(state: DialogueState, words: Wording) -> dict[str, str]:
    """The confirmed answers of the active scenario: known values only."""
    out: dict[str, str] = {}
    for slot in Slot:
        e = state.entry(slot)
        if e.known and e.value is not None:
            out[_slot_name(slot)] = words.label(slot, e.value)
        elif e.status.value in ("not_sure", "skipped"):
            out[_slot_name(slot)] = e.status.value.replace("_", " ")
    return out


def corrections(state: DialogueState, words: Wording) -> list[str]:
    """Answers that were replaced, with what replaced them (superseded facts never count)."""
    out: list[str] = []
    for event in state.history:
        if event.superseded and event.value is not None:
            current = state.value(event.slot)
            now = words.label(event.slot, current) if current else "not given"
            was = words.label(event.slot, event.value)
            if was != now:
                line = f"{_slot_name(event.slot)}: {was} (replaced by {now})"
                if line not in out:
                    out.append(line)
    return out


def pending_task(state: DialogueState, words: Wording) -> tuple[str | None, list[str]]:
    """The open question, and any proposal waiting for confirmation."""
    p = state.pending
    if p is None:
        return None, []
    if p.kind is PendingKind.CONFIRM and p.value is not None:
        return None, [f"{_slot_name(p.slot)}: {words.label(p.slot, p.value)} (not yet confirmed)"]
    if p.kind is PendingKind.QUESTION:
        return words.slots[p.slot].question, []
    return None, []


def scenario_labels(state: DialogueState, words: Wording) -> list[str]:
    from app.assistant.scenarios import scenario_label

    return [scenario_label(s.slots, words) for s in state.scenarios]


def build_summary(
    state: DialogueState,
    previous: ConversationSummary | None,
    aged: list[HistoryTurn],
    words: Wording,
    budget_tokens: int,
) -> ConversationSummary:
    """The summary after `aged` turns leave the recent window. Profile parts always come from
    the current state, so a correction supersedes the old value immediately."""
    earlier = list(previous.earlier_messages) if previous else []
    earlier += [t.customer for t in aged if t.customer]
    # Keep complete messages, newest first, within half the budget.
    kept: list[str] = []
    used = 0
    for message in reversed(earlier[-EARLIER_MESSAGES_MAX:]):
        cost = tokens(message) + 2
        if used + cost > budget_tokens // 2:
            break
        kept.append(message)
        used += cost
    question, unconfirmed = pending_task(state, words)
    through = max([t.turn for t in aged], default=previous.through_turn if previous else 0)
    return ConversationSummary(
        through_turn=max(through, previous.through_turn if previous else 0),
        confirmed=profile_facts(state, words),
        corrected=corrections(state, words),
        unconfirmed=unconfirmed,
        open_question=question,
        other_scenarios=scenario_labels(state, words),
        earlier_messages=list(reversed(kept)),
    )


# --- the context -------------------------------------------------------------------------------


@dataclass
class ConversationContext:
    """Everything the LLM is given about the conversation (besides this turn's facts)."""

    profile: dict[str, str]
    corrected: list[str]
    unconfirmed: list[str]
    open_question: str | None
    other_scenarios: list[str]
    summary: ConversationSummary | None
    recent: list[HistoryTurn]
    tokens: int = 0
    assembly_ms: float = 0.0
    dropped_turns: int = 0
    stats: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed_profile": self.profile,
            "corrected_answers": self.corrected,
            "awaiting_confirmation": self.unconfirmed,
            "open_question": self.open_question,
            "other_account_scenarios": self.other_scenarios,
            "summary_of_earlier_turns": (
                None
                if self.summary is None
                else {"earlier_customer_messages": self.summary.earlier_messages}
            ),
            "recent_turns": [t.as_dict() for t in self.recent],
        }

    def render(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


def assemble(
    state: DialogueState,
    turns: list[TurnRecord],
    previous: ConversationSummary | None,
    words: Wording,
    *,
    budget_tokens: int,
    max_recent: int,
) -> tuple[ConversationContext, ConversationSummary | None]:
    """The context for this turn and the (possibly updated) rolling summary.

    `turns` are the stored turns after the previous summary, oldest first. The newest
    complete turns that fit in the budget stay verbatim (at most `max_recent`); older ones
    move into the summary. Never a partial turn."""
    started = time.perf_counter()
    history = [h for t in turns if (h := history_turn(t, words)) is not None]
    recent: list[HistoryTurn] = []
    used = 0
    for h in reversed(history):
        cost = tokens(json.dumps(h.as_dict(), ensure_ascii=False))
        if len(recent) >= max_recent or (recent and used + cost > budget_tokens):
            break
        recent.append(h)
        used += cost
    recent.reverse()
    aged = history[: len(history) - len(recent)]
    summary = previous
    if aged or previous is not None:
        summary = build_summary(state, previous, aged, words, budget_tokens)
    question, unconfirmed = pending_task(state, words)
    context = ConversationContext(
        profile=profile_facts(state, words),
        corrected=corrections(state, words),
        unconfirmed=unconfirmed,
        open_question=question,
        other_scenarios=scenario_labels(state, words),
        summary=summary,
        recent=recent,
        dropped_turns=len(aged),
    )
    context.tokens = tokens(context.render())
    context.assembly_ms = (time.perf_counter() - started) * 1000
    return context, summary
