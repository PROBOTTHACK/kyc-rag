"""Profile slots and dialogue state (A3; A1 §6).

The state is plain, serialisable data (Pydantic, frozen) so a later stage can persist it.
It never holds the customer's message text: only slot values, their status and origin, and
a history of slot events (A1 R6: superseded answers stay in the history).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.taxonomy import (
    AccountType,
    CustomerCategory,
    Facet,
    Holding,
    Intent,
    Minor,
    NriScheme,
)


class Slot(StrEnum):
    ACCOUNT_TYPE = "account_type"
    CUSTOMER_CATEGORY = "customer_category"
    NRI_SCHEME = Facet.NRI_SCHEME.value
    HOLDING = Facet.HOLDING.value
    MINOR = Facet.MINOR.value


ESSENTIAL: frozenset[Slot] = frozenset({Slot.ACCOUNT_TYPE, Slot.CUSTOMER_CATEGORY})
FACET_SLOTS: tuple[Slot, ...] = (Slot.NRI_SCHEME, Slot.HOLDING, Slot.MINOR)  # asking order

SLOT_VALUES: dict[Slot, type[StrEnum]] = {
    Slot.ACCOUNT_TYPE: AccountType,
    Slot.CUSTOMER_CATEGORY: CustomerCategory,
    Slot.NRI_SCHEME: NriScheme,
    Slot.HOLDING: Holding,
    Slot.MINOR: Minor,
}


def valid_value(slot: Slot, value: str) -> bool:
    return value in {v.value for v in SLOT_VALUES[slot]}


def slot_for_facet(facet: Facet) -> Slot:
    return Slot(facet.value)


class SlotStatus(StrEnum):
    UNKNOWN = "unknown"  # never answered
    KNOWN = "known"
    NOT_SURE = "not_sure"  # the customer said "I'm not sure": stays unknown, not asked again
    SKIPPED = "skipped"  # non-essential slots only


class Origin(StrEnum):
    """A1 §6: how a value entered the profile."""

    CHOSEN = "chosen"  # a chip, or "yes" to a confirmation question
    STATED = "stated"  # explicit words in the customer's message (deterministic match)
    CORRECTED = "corrected"  # replaced an earlier value


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotEntry(_Frozen):
    status: SlotStatus = SlotStatus.UNKNOWN
    value: str | None = None
    origin: Origin | None = None

    @property
    def known(self) -> bool:
        return self.status is SlotStatus.KNOWN

    @property
    def open(self) -> bool:
        """Still to be asked (never answered)."""
        return self.status is SlotStatus.UNKNOWN


class SlotEvent(_Frozen):
    turn: int = Field(ge=1)
    slot: Slot
    status: SlotStatus
    value: str | None
    origin: Origin | None
    superseded: bool = False


class PendingKind(StrEnum):
    QUESTION = "question"  # a slot question
    CONFIRM = "confirm"  # a yes/no confirmation of a proposed value


class ConfirmReason(StrEnum):
    HINT = "hint"  # context suggests a value (e.g. "I live abroad" -> NRI?)
    CONFLICT = "conflict"  # a new value contradicts an earlier answer, without a correction cue
    LLM = "llm"  # the optional LLM proposed a value: never accepted without confirmation


class Pending(_Frozen):
    kind: PendingKind
    slot: Slot
    value: str | None = None  # the proposed value (CONFIRM only)
    reason: ConfirmReason | None = None


def _empty_slots() -> dict[Slot, SlotEntry]:
    return {s: SlotEntry() for s in Slot}


class DialogueState(_Frozen):
    turn: int = 0
    intent: Intent | None = None
    slots: dict[Slot, SlotEntry] = Field(default_factory=_empty_slots)
    pending: Pending | None = None
    unclear: dict[Slot, int] = Field(default_factory=dict)  # unclear replies per slot (bounded)
    organisation_hint: bool = False  # "my business" etc.: an organisation, kind unknown (R7)
    open_topics: tuple[str, ...] = ()  # item questions waiting for the checklist (R5)
    history: tuple[SlotEvent, ...] = ()

    def entry(self, slot: Slot) -> SlotEntry:
        return self.slots.get(slot, SlotEntry())

    def value(self, slot: Slot) -> str | None:
        e = self.entry(slot)
        return e.value if e.known else None

    @property
    def account_type(self) -> AccountType | None:
        v = self.value(Slot.ACCOUNT_TYPE)
        return AccountType(v) if v else None

    @property
    def customer_category(self) -> CustomerCategory | None:
        v = self.value(Slot.CUSTOMER_CATEGORY)
        return CustomerCategory(v) if v else None
