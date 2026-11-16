"""Loads and validates the fixed dialogue wording (`dialogue.yaml`). Fails closed: a missing
question, explanation, not-sure note or chip label stops the load."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.profile import ESSENTIAL, SLOT_VALUES, Slot

WORDING_FILE = Path(__file__).with_name("dialogue.yaml")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotWording(_Model):
    question: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    not_sure: str = Field(min_length=1)
    chips: dict[str, str]


class ConfirmWording(_Model):
    hint: str
    llm: str
    conflict: str


class BlockedWording(_Model):
    identifier: str
    unusable: str
    other: str


class ReplyWording(_Model):
    """The assistant's own conversational lines (the deterministic reply; the LLM voice may
    rephrase them from the same facts). Placeholders are filled from the API answer only."""

    checklist_intro: str = Field(min_length=1)  # {subject} {documents} {details}
    subject: str = Field(min_length=1)  # {account} {holder}
    conversion_subject: str = Field(min_length=1)
    documents_one: str = Field(min_length=1)
    documents_many: str = Field(min_length=1)  # {n}
    details_one: str = Field(min_length=1)
    details_many: str = Field(min_length=1)  # {n}
    documents_lead: str = Field(min_length=1)
    asked_lead: str = Field(min_length=1)
    checklist_outro: str = Field(min_length=1)
    focused_lead: str = Field(min_length=1)  # {subject}
    unchanged: str = Field(min_length=1)
    off_topic: str = Field(min_length=1)
    suggestions_lead: str = Field(min_length=1)

    @model_validator(mode="after")
    def _placeholders(self) -> ReplyWording:
        for name, needed in [
            ("checklist_intro", ("{subject}", "{documents}", "{details}")),
            ("subject", ("{account}", "{holder}")),
            ("documents_many", ("{n}",)),
            ("details_many", ("{n}",)),
        ]:
            if not all(p in getattr(self, name) for p in needed):
                raise ValueError(f"reply wording {name} lost a placeholder")
        return self


class ContextWording(_Model):
    """What the assistant says when a message must be read against the conversation so far
    (owner request: context-aware follow-ups). {subject} / {holder} come from the profile."""

    change: str = Field(min_length=1)  # {holder}
    which_person: str = Field(min_length=1)
    follow_up: str = Field(min_length=1)  # {subject}
    not_applicable: str = Field(min_length=1)  # {subject}

    @model_validator(mode="after")
    def _placeholders(self) -> ContextWording:
        for name, needed in [
            ("change", ("{holder}",)),
            ("follow_up", ("{subject}",)),
            ("not_applicable", ("{subject}",)),
        ]:
            if not all(p in getattr(self, name) for p in needed):
                raise ValueError(f"context wording {name} lost a placeholder")
        return self


class SocialWording(_Model):
    """Replies to greetings and small talk: they never change the profile or checklist."""

    greeting: str = Field(min_length=1)
    greeting_again: str = Field(min_length=1)  # {subject}
    thanks: str = Field(min_length=1)
    goodbye: str = Field(min_length=1)
    acknowledgement: str = Field(min_length=1)
    small_talk: str = Field(min_length=1)


class ScenarioWording(_Model):
    """More than one account in a conversation (scenarios.py)."""

    account_for: str = Field(min_length=1)  # {holder}
    account_only: str = Field(min_length=1)  # {account}
    unnamed: str = Field(min_length=1)
    offer: str = Field(min_length=1)  # {subject}
    separate_option: str = Field(min_length=1)
    change_option: str = Field(min_length=1)
    started: str = Field(min_length=1)  # {subject}
    started_fresh: str = Field(min_length=1)
    switch_offer: str = Field(min_length=1)  # {subject}
    switch_option: str = Field(min_length=1)
    stay_option: str = Field(min_length=1)
    switched: str = Field(min_length=1)  # {subject}

    @model_validator(mode="after")
    def _placeholders(self) -> ScenarioWording:
        for name, needed in [
            ("account_for", "{holder}"),
            ("account_only", "{account}"),
            ("offer", "{subject}"),
            ("started", "{subject}"),
            ("switch_offer", "{subject}"),
            ("switched", "{subject}"),
        ]:
            if needed not in getattr(self, name):
                raise ValueError(f"scenario wording {name} lost {needed}")
        return self


class Wording(_Model):
    version: str
    slots: dict[Slot, SlotWording]
    skipped: str
    skip_not_available: str
    unclear: str
    unclear_gave_up: str
    confirm: ConfirmWording
    confirm_declined: str
    corrected: str
    ambiguous: str
    organisation_unspecified: str
    unsupported_customer: str
    out_of_scope: str
    cannot_build: dict[Slot, str]
    blocked: BlockedWording
    privacy: str
    replies: ReplyWording
    context: ContextWording
    social: SocialWording
    scenarios: ScenarioWording
    fallback: str
    notice: str

    @model_validator(mode="after")
    def _complete(self) -> Wording:
        for slot in Slot:
            if slot not in self.slots:
                raise ValueError(f"no wording for slot {slot}")
            expected = {v.value for v in SLOT_VALUES[slot]}
            if set(self.slots[slot].chips) != expected:
                raise ValueError(f"{slot}: chip labels must cover exactly {sorted(expected)}")
        if set(self.cannot_build) != set(ESSENTIAL):
            raise ValueError("cannot_build explains each essential slot")
        if "{value}" not in self.corrected or "{old}" not in self.confirm.conflict:
            raise ValueError("templates lose their placeholders")
        return self

    def label(self, slot: Slot, value: str) -> str:
        return self.slots[slot].chips[value]


def load_wording(path: Path = WORDING_FILE) -> Wording:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("wording file is not a mapping")
    return Wording.model_validate(raw)


@lru_cache(maxsize=1)
def wording() -> Wording:
    return load_wording()
