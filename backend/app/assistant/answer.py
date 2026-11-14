"""The answer object returned by the assistant API (A5; A1 §8.1).

One `AssistantAnswer` per turn, assembled from the dialogue step (A3) and, when the profile
is ready, the checklist (A4). Nothing here decides anything: it only selects and renames what the
customer may see. Left out on purpose:
- the not-established `reason` and the checklist `reason` (internal wording: coverage-map
  notes, stage names). The customer gets `explanation` instead, from the coverage map's own
  customer wording, plus `suggestions` (covered combinations) for a one-tap change;
- citation chunk ids, diagnostics, retrieval scores, why a value is being confirmed, and
  whether an LLM was consulted (A1 §8.2.6, §9.10).

The view models carry their own names (`AssistantQuestion`, `SourceCitation`, ...) so that
the published OpenAPI document keeps the v1 schema names unchanged.

`kind` extends A1 §8.1's four kinds with the dialogue's own outcomes: `confirm` (a yes/no
check of a proposed value), `cannot_build` (an essential answer is still unknown) and
`blocked` (input screening refused the message; it is never echoed).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.assistant.checklist import AnswerKind, ChecklistAnswer, Classification
from app.assistant.dialogue import Chip, DialogueResponse, StepKind
from app.assistant.profile import DialogueState, Origin, Slot, SlotStatus
from app.assistant.taxonomy import CoverageState
from app.assistant.wording import Wording


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssistantAnswerKind(StrEnum):
    QUESTION = "question"
    CONFIRM = "confirm"
    CHECKLIST = "checklist"
    FALLBACK = "fallback"
    CANNOT_BUILD = "cannot_build"
    BLOCKED = "blocked"
    TECHNICAL_ERROR = "technical_error"


class AssistantQuestion(_Frozen):
    slot: Slot
    text: str
    chips: tuple[Chip, ...]
    allow_not_sure: bool
    allow_skip: bool
    allow_explain: bool


class AssistantConfirmation(_Frozen):
    """A yes/no check of a proposed value. Why it was proposed (a hint, a conflict or an LLM
    suggestion) is internal and not sent."""

    slot: Slot
    value: str
    text: str


class AssistantSuggestion(_Frozen):
    """A combination the documents cover, offered after a not-established answer. The UI
    sends it back as a `correct` turn for `slot` with `value`."""

    slot: Slot
    value: str
    label: str


class AssistantMessage(_Frozen):
    """A short message shown with the answer (a dialogue note, the blocked text, or why a
    checklist cannot be built yet)."""

    code: str
    text: str
    slot: Slot | None = None


class ProfileSlot(_Frozen):
    status: SlotStatus
    value: str | None = None
    label: str | None = None  # the chip label for the value, as the customer saw it
    origin: Origin | None = None
    # Every valid value for this slot, with the same labels as the slot's question (A6 option
    # A: what a "Change" control offers). From the wording file, which is checked against the
    # allowed values; the server still validates every correction.
    choices: tuple[Chip, ...] = ()


class SourceCitation(_Frozen):
    document_key: str
    document_title: str
    page: int
    page_end: int | None
    section: str | None
    text_reliability: str
    extraction_method: str
    date_basis: str
    quotable: bool
    basis: bool
    passage: str


class SourceCaveat(_Frozen):
    code: str
    text: str


class AssistantItem(_Frozen):
    id: str  # a stable key for the UI; not shown to the customer
    title: str
    kind: str
    classification: Classification
    classification_label: str
    condition: str | None = None
    condition_note: str | None = None
    applies_if: tuple[str, ...] = ()
    source_wording: str | None = None
    approximate_reading: str | None = None
    uncertainty_warning: str | None = None
    establishes: str | None = None
    does_not_establish: str
    note: str | None = None
    citations: tuple[SourceCitation, ...] = ()
    related_citations: tuple[SourceCitation, ...] = ()
    caveats: tuple[SourceCaveat, ...] = ()
    asked: bool = False
    fallback: str | None = None


class AssistantGroups(_Frozen):
    documents: tuple[AssistantItem, ...]
    information: tuple[AssistantItem, ...]
    not_confirmed: tuple[AssistantItem, ...]


class AssistantSource(_Frozen):
    document_key: str
    title: str
    date_basis: str
    scanned: bool


class AssistantChecklist(_Frozen):
    groups: AssistantGroups
    sources: tuple[AssistantSource, ...]
    coverage_note: str | None = None


class AssistantAnswer(_Frozen):
    kind: AssistantAnswerKind
    question: AssistantQuestion | None = None
    confirmation: AssistantConfirmation | None = None
    messages: tuple[AssistantMessage, ...] = ()
    profile: dict[Slot, ProfileSlot]
    coverage_state: CoverageState | None = None
    checklist: AssistantChecklist | None = None
    fallback: str | None = None  # A1 §7, verbatim
    explanation: str | None = None  # why the documents don't cover it, in plain language
    suggestions: tuple[AssistantSuggestion, ...] = ()  # combinations they do cover
    technical_message: str | None = None  # A1 §7, verbatim
    notice: str  # A1 §7 dated-document notice, verbatim, on every answer


# --- assembly ------------------------------------------------------------------------------------


def profile_view(state: DialogueState, words: Wording) -> dict[Slot, ProfileSlot]:
    out = {}
    for slot in Slot:
        e = state.entry(slot)
        label = words.label(slot, e.value) if e.value is not None else None
        choices = tuple(Chip(value=v, label=t) for v, t in words.slots[slot].chips.items())
        out[slot] = ProfileSlot(
            status=e.status, value=e.value, label=label, origin=e.origin, choices=choices
        )
    return out


def _checklist_view(a: ChecklistAnswer) -> AssistantChecklist | None:
    if a.groups is None:
        return None
    # The dump already leaves out chunk ids and diagnostics; the view models forbid extra
    # fields, so a new A4 field fails here loudly instead of leaking.
    data = a.model_dump(mode="json", include={"groups", "sources", "coverage_note"})
    return AssistantChecklist.model_validate(data)


def build_answer(
    state: DialogueState,
    response: DialogueResponse,
    words: Wording,
    checklist: ChecklistAnswer | None = None,
) -> AssistantAnswer:
    """Map one dialogue step (and its checklist, if any) to the customer-facing answer."""
    messages = [
        AssistantMessage(code=n.code.value, text=n.text, slot=n.slot) for n in response.notes
    ]
    if response.step is StepKind.BLOCKED and response.blocked:
        messages.append(AssistantMessage(code="blocked", text=response.blocked))
    if response.step in (StepKind.CANNOT_BUILD, StepKind.FALLBACK) and response.reason:
        messages.append(AssistantMessage(code="cannot_build", text=response.reason))
    question = None
    if response.question is not None:
        question = AssistantQuestion.model_validate(response.question.model_dump())
    confirmation = None
    if response.confirmation is not None:
        c = response.confirmation
        confirmation = AssistantConfirmation(slot=c.slot, value=c.value, text=c.text)
    fallback = response.fallback
    coverage_state: CoverageState | None = None
    technical: str | None = None
    view = None
    notice = words.notice

    if checklist is not None:
        coverage_state = checklist.coverage_state
        notice = checklist.notice
        fallback = fallback or checklist.fallback
        technical = checklist.technical_message
        view = _checklist_view(checklist)
        kind = {
            AnswerKind.CHECKLIST: AssistantAnswerKind.CHECKLIST,
            AnswerKind.FALLBACK: AssistantAnswerKind.FALLBACK,
            AnswerKind.TECHNICAL_ERROR: AssistantAnswerKind.TECHNICAL_ERROR,
        }[checklist.kind]
    elif response.step is StepKind.BLOCKED:
        kind = AssistantAnswerKind.BLOCKED
    elif response.step is StepKind.CONFIRM:
        kind = AssistantAnswerKind.CONFIRM
    elif response.step is StepKind.ASK:
        kind = AssistantAnswerKind.QUESTION
    elif response.step is StepKind.NOT_ESTABLISHED:
        kind = AssistantAnswerKind.FALLBACK
        coverage_state = CoverageState.NOT_ESTABLISHED
    elif response.step is StepKind.CANNOT_BUILD:
        kind = AssistantAnswerKind.CANNOT_BUILD
    else:  # FALLBACK: an out-of-scope question; the next question may ride along
        kind = AssistantAnswerKind.FALLBACK

    return AssistantAnswer(
        kind=kind,
        question=question,
        confirmation=confirmation,
        messages=tuple(messages),
        profile=profile_view(state, words),
        coverage_state=coverage_state,
        checklist=view,
        fallback=fallback,
        explanation=response.explanation,
        suggestions=tuple(
            AssistantSuggestion(slot=x.slot, value=x.value, label=x.label)
            for x in response.suggestions
        ),
        technical_message=technical,
        notice=notice,
    )


def opening_answer(
    state: DialogueState, response: DialogueResponse, words: Wording
) -> AssistantAnswer:
    """The first answer of a conversation: the privacy message, then the first question."""
    answer = build_answer(state, response, words)
    privacy = AssistantMessage(code="privacy", text=words.privacy)
    return answer.model_copy(update={"messages": (privacy, *answer.messages)})


def technical_answer(state: DialogueState, words: Wording, message: str) -> AssistantAnswer:
    """The fixed technical message (A1 §7) when the checklist could not be built."""
    return AssistantAnswer(
        kind=AssistantAnswerKind.TECHNICAL_ERROR,
        profile=profile_view(state, words),
        technical_message=message,
        notice=words.notice,
    )
