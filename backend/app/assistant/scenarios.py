"""Account scenarios: more than one account in one conversation.

The active scenario is the dialogue state's own answers; other scenarios are parked copies
(`DialogueState.scenarios`). Starting a separate account parks the current one; returning
swaps them. A scenario's answers never leak into another, so an LLP current account and a
child's account are never combined into one checklist.
"""

from __future__ import annotations

from app.assistant.profile import DialogueState, Slot, SlotEntry
from app.assistant.wording import Wording


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def scenario_label(slots: dict[Slot, SlotEntry], words: Wording) -> str:
    """'a current account for a limited liability partnership (LLP)', or as much as is known."""
    acct = slots.get(Slot.ACCOUNT_TYPE)
    cat = slots.get(Slot.CUSTOMER_CATEGORY)
    account = (
        _lower_first(words.label(Slot.ACCOUNT_TYPE, acct.value))
        if acct is not None and acct.known and acct.value
        else None
    )
    holder = (
        _lower_first(words.label(Slot.CUSTOMER_CATEGORY, cat.value))
        if cat is not None and cat.known and cat.value
        else None
    )
    if account and holder:
        return words.replies.subject.format(account=account, holder=holder)
    if holder:
        return words.scenarios.account_for.format(holder=holder)
    if account:
        return words.scenarios.account_only.format(account=account)
    return words.scenarios.unnamed


def has_answers(state: DialogueState) -> bool:
    return any(e.status.value != "unknown" for e in state.slots.values())
