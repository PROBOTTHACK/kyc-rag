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
