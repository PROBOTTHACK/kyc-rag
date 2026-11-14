"""The assistant's deterministic conversational reply (always available).

`compose_reply` turns one customer-facing answer into the line(s) the assistant says in the
chat: its notes, then the question or confirmation, or a short summary of the checklist (item
titles from the answer, counts, a pointer to the sources). It adds no fact of its own: every
title, label and number comes from the answer; the connecting words come from
`dialogue.yaml` (`replies`). The optional LLM voice (`voice.py`) may rephrase this, from the
same facts only, and falls back to it on any failure.

Fixed texts (the prescribed fallback, the explanation, the blocked text, the privacy message,
the technical message) are not part of the reply: the interface shows them verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.assistant.profile import Slot
from app.assistant.wording import Wording

if TYPE_CHECKING:
    from app.assistant.answer import AssistantAnswer

# Messages the interface shows verbatim (never folded into the reply).
FIXED_MESSAGE_CODES = frozenset({"privacy", "blocked"})
NEWLINE = chr(10)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


def checklist_summary(answer: AssistantAnswer, words: Wording) -> list[str]:
    r = words.replies
    checklist = answer.checklist
    if checklist is None:
        return []
    groups = checklist.groups
    if answer.coverage_state == "CONVERSION_ONLY":
        subject = r.conversion_subject
    else:
        account = answer.profile.get(Slot.ACCOUNT_TYPE)
        holder = answer.profile.get(Slot.CUSTOMER_CATEGORY)
        subject = r.subject.format(
            account=_lower_first((account.label if account else None) or ""),
            holder=_lower_first((holder.label if holder else None) or ""),
        )
    lines = [
        r.checklist_intro.format(
            subject=subject,
            documents=_count(len(groups.documents), r.documents_one, r.documents_many),
            details=_count(len(groups.information), r.details_one, r.details_many),
        )
    ]
    if groups.documents:
        lines.append(r.documents_lead)
        lines += [
            f"- {item.title}" + (f" ({' '.join(item.applies_if)})" if item.applies_if else "")
            for item in groups.documents
        ]
    if groups.not_confirmed:
        lines.append(r.asked_lead)
        lines += [f"- {item.title}" for item in groups.not_confirmed]
    lines.append(r.checklist_outro)
    return lines


def _subject(answer: AssistantAnswer, words: Wording) -> str:
    r = words.replies
    if answer.coverage_state == "CONVERSION_ONLY":
        return r.conversion_subject
    account = answer.profile.get(Slot.ACCOUNT_TYPE)
    holder = answer.profile.get(Slot.CUSTOMER_CATEGORY)
    return r.subject.format(
        account=_lower_first((account.label if account else None) or ""),
        holder=_lower_first((holder.label if holder else None) or ""),
    )


def focused_summary(answer: AssistantAnswer, words: Wording) -> list[str]:
    """A short answer about the current checklist: its documents and asked-about items."""
    r = words.replies
    checklist = answer.checklist
    if checklist is None:
        return []
    groups = checklist.groups
    lines: list[str] = []
    if groups.documents:
        lines.append(r.focused_lead.format(subject=_subject(answer, words)))
        lines += [
            f"- {item.title}" + (f" ({' '.join(item.applies_if)})" if item.applies_if else "")
            for item in groups.documents
        ]
    if groups.not_confirmed:
        lines.append(r.asked_lead)
        lines += [f"- {item.title}" for item in groups.not_confirmed]
    lines.append(r.checklist_outro)
    return lines


def compose_reply(answer: AssistantAnswer, unchanged: bool, words: Wording) -> str | None:
    """What the assistant says for this answer, or None when only fixed texts apply."""
    if answer.kind in ("blocked", "technical_error"):
        return None
    lines = [m.text for m in answer.messages if m.code not in FIXED_MESSAGE_CODES]
    r = words.replies
    codes = {m.code for m in answer.messages}
    if answer.kind == "checklist" and answer.checklist is not None:
        if answer.fallback:  # out of scope while the checklist stands
            lines.append(r.off_topic)
        elif "separate_request" in codes:
            pass  # the note says it all; the current checklist is kept, not repeated
        elif "follow_up" in codes or unchanged:
            # A follow-up about the same checklist: answer it briefly, don't repeat it all.
            if unchanged and "follow_up" not in codes:
                lines.append(r.unchanged)
            lines += focused_summary(answer, words)
        else:
            lines += checklist_summary(answer, words)
    elif answer.confirmation is not None:
        lines.append(answer.confirmation.text)
    elif answer.question is not None:
        lines.append(answer.question.text)
    elif answer.suggestions:
        lines.append(r.suggestions_lead)
    return NEWLINE.join(lines) or None
