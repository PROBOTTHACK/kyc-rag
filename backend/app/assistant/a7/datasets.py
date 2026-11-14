"""The A7 datasets (data/eval/a7_*): typed, validated on load, synthetic only.

- `a7_coverage_profiles.jsonl`: generated from coverage.yaml (`generate_profiles.py`), never
  hand-written: one line per complete profile of a covered cell, per not-established cell,
  and per conversion phrasing / near miss.
- `a7_conversations.yaml`: scripted multi-turn conversations with per-turn expectations.
- `a7_memory_cases.yaml`: conversations plus facts that must / must not reach the LLM.
- `a7_pii_messages.jsonl`: synthetic identifiers (must be refused or redacted) and negative
  controls (must pass).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.profile import Slot, valid_value

REPO_ROOT = Path(__file__).resolve().parents[4]
EVAL_DIR = REPO_ROOT / "data" / "eval"
PROFILES_FILE = EVAL_DIR / "a7_coverage_profiles.jsonl"
CONVERSATIONS_FILE = EVAL_DIR / "a7_conversations.yaml"
MEMORY_FILE = EVAL_DIR / "a7_memory_cases.yaml"
PII_FILE = EVAL_DIR / "a7_pii_messages.jsonl"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- coverage profiles ---------------------------------------------------------------------------


class CoverageProfile(_Model):
    id: str
    kind: Literal["covered", "not_established", "conversion", "conversion_near_miss"]
    customer_category: str | None = None
    account_type: str | None = None
    state: str | None = None  # the coverage map's state for the cell
    sources: tuple[str, ...] = ()  # the cell's permitted source documents
    facets: dict[str, str] = Field(default_factory=dict)  # slot -> value ("NOT_SURE" allowed)
    message: str | None = None  # conversion phrasings

    @model_validator(mode="after")
    def _consistent(self) -> CoverageProfile:
        for slot, value in self.facets.items():
            if value != "NOT_SURE" and not valid_value(Slot(slot), value):
                raise ValueError(f"{self.id}: invalid facet {slot}={value}")
        if self.kind in ("conversion", "conversion_near_miss") and not self.message:
            raise ValueError(f"{self.id}: a conversion case needs a message")
        return self


def load_profiles(path: Path = PROFILES_FILE) -> list[CoverageProfile]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [CoverageProfile.model_validate_json(line) for line in lines if line.strip()]


# --- conversations -------------------------------------------------------------------------------


class Expect(_Model):
    """What must hold after one turn. Every field is optional; absent = not checked."""

    kind: str | None = None  # API answer kind
    question: str | None = None  # the slot asked (kind question)
    confirm: tuple[str, str] | None = None  # (slot, value) of a confirmation
    offer: bool | None = None  # a scenario offer is shown
    offer_actions: tuple[str, ...] | None = None
    notes_include: tuple[str, ...] = ()
    notes_exclude: tuple[str, ...] = ()
    profile: dict[str, str | None] = Field(default_factory=dict)  # slot -> value (None = unknown)
    profile_unchanged: bool = False  # same profile values as before the turn
    fallback: bool | None = None  # the A1 §7 fallback text is present
    checklist: bool | None = None  # a checklist is attached
    reply_has: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()
    scenarios: int | None = None  # parked accounts
    blocked: bool | None = None


class Turn(_Model):
    say: str | None = None
    choose: tuple[str, str] | None = None  # (slot, value)
    correct: tuple[str, str] | None = None
    action: str | None = None  # confirm_yes, new_scenario, switch_scenario, ...
    slot: str | None = None  # with actions not_sure, skip, explain
    value: str | None = None  # with action switch_scenario
    reload: bool = False  # GET the session and continue from the stored state
    restart: bool = False  # a new service instance on the same database
    expect: Expect = Field(default_factory=Expect)

    @model_validator(mode="after")
    def _one_input(self) -> Turn:
        inputs = [
            self.say is not None,
            self.choose is not None,
            self.correct is not None,
            self.action is not None,
            self.reload or self.restart,
        ]
        if sum(inputs) != 1:
            raise ValueError("a turn has exactly one input (say, choose, correct, action, reload)")
        return self

    def body(self) -> dict[str, Any] | None:
        if self.say is not None:
            return {"message": self.say}
        if self.choose is not None:
            return {"action": "choose", "slot": self.choose[0], "value": self.choose[1]}
        if self.correct is not None:
            return {"action": "correct", "slot": self.correct[0], "value": self.correct[1]}
        if self.action is not None:
            body: dict[str, Any] = {"action": self.action}
            if self.slot is not None:
                body["slot"] = self.slot
            if self.value is not None:
                body["value"] = self.value
            return body
        return None


CATEGORIES = (
    "greetings",
    "follow_ups",
    "ambiguous",
    "corrections",
    "separate_accounts",
    "context_retention",
    "off_topic",
)


class Conversation(_Model):
    id: str
    category: Literal[
        "greetings",
        "follow_ups",
        "ambiguous",
        "corrections",
        "separate_accounts",
        "context_retention",
        "off_topic",
    ]
    description: str
    turns: tuple[Turn, ...] = Field(min_length=1)


class ConversationSet(_Model):
    version: str
    conversations: tuple[Conversation, ...]

    @model_validator(mode="after")
    def _unique(self) -> ConversationSet:
        ids = [c.id for c in self.conversations]
        if len(ids) != len(set(ids)):
            raise ValueError("conversation ids must be unique")
        return self


def load_conversations(path: Path = CONVERSATIONS_FILE) -> ConversationSet:
    return ConversationSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- memory cases --------------------------------------------------------------------------------


class MemoryCheck(_Model):
    """The context the LLM received while answering scripted turn `at_turn` (1-based; a
    free-text turn) must contain every `present` fact and none of the `absent` ones; the
    rolling summary stored after that turn must hold `summary_*`."""

    at_turn: int = Field(ge=1)
    present: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    summary_confirmed: dict[str, str] = Field(default_factory=dict)  # label -> value
    summary_corrected: tuple[str, ...] = ()  # substrings of `corrected` entries


class MemoryCase(_Model):
    id: str
    description: str
    context_tokens: int = Field(default=1500, ge=100)
    recent_turns: int = Field(default=10, ge=1)
    turns: tuple[Turn, ...] = Field(min_length=1)
    checks: tuple[MemoryCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _checks_on_messages(self) -> MemoryCase:
        for c in self.checks:
            if c.at_turn > len(self.turns) or self.turns[c.at_turn - 1].say is None:
                raise ValueError(f"{self.id}: at_turn {c.at_turn} must be a free-text turn")
        return self


class MemorySet(_Model):
    version: str
    cases: tuple[MemoryCase, ...]


def load_memory_cases(path: Path = MEMORY_FILE) -> MemorySet:
    return MemorySet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- PII messages --------------------------------------------------------------------------------


class PiiMessage(_Model):
    id: str
    message: str
    expect: Literal["block", "redact", "allow"]
    category: str  # pan, aadhaar, passport, card_number, account_number, email, phone, control
    secret: str | None = None  # the synthetic identifier that must never appear anywhere


def load_pii(path: Path = PII_FILE) -> list[PiiMessage]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [PiiMessage.model_validate(json.loads(line)) for line in lines if line.strip()]
