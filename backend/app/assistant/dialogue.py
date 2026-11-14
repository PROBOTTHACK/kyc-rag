"""Dialogue policy (A3; A1 §6 R1-R10). A plain per-turn service: no LangGraph, no I/O.

`advance(state, turn, interpretation)` is a pure, deterministic state transition. It returns
the new state and ONE step:
- ASK: one slot question (chips + "I'm not sure", "Skip" where non-essential, "What does this
  mean?");
- CONFIRM: a yes/no confirmation of a proposed value (hints, conflicts, LLM suggestions);
- READY: the profile is complete enough for a checklist (built in A4, not here);
- NOT_ESTABLISHED: the coverage map has no applicable opening material (prescribed fallback);
- CANNOT_BUILD: an essential slot stays unknown, with the explanation of why;
- FALLBACK: an out-of-scope question or an unsupported customer type (the next question, if
  any, rides along so the conversation can continue);
- BLOCKED: input screening refused the message (fixed text; the message is never echoed).

Which follow-ups are material comes from the coverage map, never from this module. The
optional LLM (`respond`) can only propose slot values, and each one is confirmed by the
customer before it is used. Nothing here decides items, evidence classes or citations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.assistant.coverage import DEFAULT_NOT_ESTABLISHED, Coverage, coverage
from app.assistant.llm_extract import LLMHint, extract_with_llm
from app.assistant.profile import (
    ESSENTIAL,
    FACET_SLOTS,
    ConfirmReason,
    DialogueState,
    Origin,
    Pending,
    PendingKind,
    Slot,
    SlotEntry,
    SlotEvent,
    SlotStatus,
    slot_for_facet,
    valid_value,
)
from app.assistant.taxonomy import AccountType, CoverageState, CustomerCategory, Intent
from app.assistant.understand import Control, Person, Understanding, understand
from app.assistant.wording import Wording, wording
from app.llm.provider import LLMProvider
from app.security.screen import ScreenResult, screen_text

MAX_UNCLEAR = 1  # one reworded re-ask; the next unclear reply counts as "I'm not sure"
ORGANISATIONS = (
    CustomerCategory.SOLE_PROPRIETORSHIP, CustomerCategory.PARTNERSHIP_FIRM, CustomerCategory.LLP,
    CustomerCategory.COMPANY, CustomerCategory.TRUST, CustomerCategory.HUF,
    CustomerCategory.OTHER_ORGANISATION,
)  # fmt: skip


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- input ---------------------------------------------------------------------------------------


class Action(StrEnum):
    CHOOSE = "choose"  # a chip
    CORRECT = "correct"  # an edit of the profile (A1 R6)
    NOT_SURE = "not_sure"
    SKIP = "skip"
    EXPLAIN = "explain"
    CONFIRM_YES = "confirm_yes"
    CONFIRM_NO = "confirm_no"
    # Account scenarios (more than one account in a conversation).
    NEW_SCENARIO = "new_scenario"  # ask about a separate account; this one is kept
    CHANGE_APPLICATION = "change_application"  # change this account's holder instead
    SWITCH_SCENARIO = "switch_scenario"  # go back to an earlier account (value: its number)


class TurnInput(_Frozen):
    message: str | None = None
    action: Action | None = None
    slot: Slot | None = None
    value: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TurnInput:
        if self.message is None and self.action is None:
            raise ValueError("a turn has a message, an action, or both")
        needs_slot = {Action.CHOOSE, Action.CORRECT, Action.NOT_SURE, Action.SKIP, Action.EXPLAIN}
        if self.action in needs_slot and self.slot is None:
            raise ValueError(f"{self.action} names its slot")
        if self.action in (Action.CHOOSE, Action.CORRECT):
            if self.slot is None or self.value is None or not valid_value(self.slot, self.value):
                raise ValueError("a choice or correction carries a valid value for its slot")
        elif self.action is Action.SWITCH_SCENARIO:
            if self.slot is not None or self.value is None or not self.value.isdigit():
                raise ValueError("a scenario switch carries the scenario number only")
            if len(self.value) > 2:
                raise ValueError("a scenario number is small")
        elif self.value is not None:
            raise ValueError("only a choice or correction carries a value")
        return self


@dataclass(frozen=True)
class Interpretation:
    """The screened message and its deterministic reading (None when blocked)."""

    screen: ScreenResult
    understanding: Understanding | None

    @property
    def blocked(self) -> bool:
        return self.screen.blocked

    @property
    def llm_allowed(self) -> bool:
        """Only clean, screened text may reach an LLM (A1 R9; CLAUDE.md principle 8)."""
        return self.screen.action in ("allow", "redact") and not self.screen.sensitive


def interpret(message: str) -> Interpretation:
    res = screen_text(message)
    if res.blocked or res.text is None:
        return Interpretation(res, None)
    return Interpretation(res, understand(res.text))


# --- output --------------------------------------------------------------------------------------


class StepKind(StrEnum):
    ASK = "ask"
    CONFIRM = "confirm"
    READY = "ready"
    NOT_ESTABLISHED = "not_established"
    CANNOT_BUILD = "cannot_build"
    FALLBACK = "fallback"
    BLOCKED = "blocked"


class NoteCode(StrEnum):
    EXPLANATION = "explanation"
    NOT_SURE = "not_sure"
    SKIPPED = "skipped"
    SKIP_NOT_AVAILABLE = "skip_not_available"
    UNCLEAR = "unclear"
    UNCLEAR_GAVE_UP = "unclear_gave_up"
    CORRECTED = "corrected"
    CONFIRM_DECLINED = "confirm_declined"
    AMBIGUOUS = "ambiguous"
    ORGANISATION_UNSPECIFIED = "organisation_unspecified"
    UNSUPPORTED_CUSTOMER = "unsupported_customer"
    OUT_OF_SCOPE = "out_of_scope"
    # Context-aware handling: the message is read against the conversation so far.
    CONTEXT_CONFLICT = "context_conflict"  # it is about another kind of holder; ask first
    CONTEXT_CHANGE = "context_change"  # an explicit change of holder; ask who
    FOLLOW_UP = "follow_up"  # about the current checklist; nothing changes
    SEPARATE_REQUEST = "separate_request"  # another account: keep this one, offer a new one
    NOT_APPLICABLE = "not_applicable"  # an answer that does not apply to this account


class Note(_Frozen):
    code: NoteCode
    text: str
    slot: Slot | None = None


class Chip(_Frozen):
    value: str
    label: str


class Question(_Frozen):
    slot: Slot
    text: str
    chips: tuple[Chip, ...]
    allow_not_sure: bool = True
    allow_skip: bool
    allow_explain: bool = True


class Confirmation(_Frozen):
    slot: Slot
    value: str
    reason: ConfirmReason
    text: str


class ReadyProfile(_Frozen):
    """What A4 receives. Facets: only the material ones; None = not sure / skipped."""

    intent: Intent
    customer_category: CustomerCategory | None
    account_type: AccountType | None
    coverage_state: CoverageState
    facets: dict[Slot, str | None]
    topics: tuple[str, ...]


class Suggestion(_Frozen):
    """A combination the documents DO cover, offered after a not-established answer. Taken
    from the coverage map only; the customer's choice is sent back as an ordinary `correct`
    turn, which the server validates like any other."""

    slot: Slot
    value: str
    label: str


class DialogueResponse(_Frozen):
    step: StepKind
    question: Question | None = None
    confirmation: Confirmation | None = None
    notes: tuple[Note, ...] = ()
    ready: ReadyProfile | None = None
    reason: str | None = None  # not-established reason (internal) or cannot-build explanation
    explanation: str | None = None  # not-established: why, in the customer's words
    suggestions: tuple[Suggestion, ...] = ()  # not-established: covered alternatives
    unchanged: bool = False  # a profile is ready and this turn changed no answer or topic
    fallback: str | None = None  # A1 §7, verbatim
    notice: str | None = None  # A1 §7 dated-document notice, verbatim
    blocked: str | None = None  # fixed screening message
    llm_consulted: bool = False


# --- the transition ------------------------------------------------------------------------------


@dataclass
class _Work:
    """Mutable working copy of one turn."""

    state: DialogueState
    cov: Coverage
    words: Wording
    slots: dict[Slot, SlotEntry] = field(init=False)
    history: list[SlotEvent] = field(init=False)
    pending: Pending | None = field(init=False)
    unclear: dict[Slot, int] = field(init=False)
    notes: list[Note] = field(default_factory=list)
    force_ask: Slot | None = None
    new_confirm: bool = False
    fallback: bool = False
    # Ask this (known) slot again to clarify the context; the profile is unchanged until the
    # customer chooses (owner: never silently merge incompatible contexts).
    clarify: Slot | None = None
    individuals_first: bool = False  # the message is about a person: list individuals first
    organisation_hint: bool = field(init=False)
    intent: Intent | None = field(init=False)
    topics: list[str] = field(init=False)

    def __post_init__(self) -> None:
        s = self.state
        self.slots = dict(s.slots)
        self.history = list(s.history)
        self.pending = s.pending
        self.unclear = dict(s.unclear)
        self.organisation_hint = s.organisation_hint
        self.intent = s.intent
        self.topics = list(s.open_topics)

    @property
    def turn(self) -> int:
        return self.state.turn

    def entry(self, slot: Slot) -> SlotEntry:
        return self.slots.get(slot, SlotEntry())

    def note(self, code: NoteCode, text: str, slot: Slot | None = None) -> None:
        self.notes.append(Note(code=code, text=text, slot=slot))

    def _record(self, slot: Slot, entry: SlotEntry) -> None:
        self.history = [
            e.model_copy(update={"superseded": True}) if e.slot is slot and not e.superseded else e
            for e in self.history
        ]
        self.history.append(
            SlotEvent(turn=self.turn, slot=slot, status=entry.status, value=entry.value,
                      origin=entry.origin)  # fmt: skip
        )
        self.slots[slot] = entry
        self.unclear.pop(slot, None)
        if self.pending is not None and self.pending.slot is slot:
            self.pending = None

    def set_value(self, slot: Slot, value: str, origin: Origin) -> None:
        old = self.entry(slot)
        if old.known and old.value == value:
            if self.pending is not None and self.pending.slot is slot:
                self.pending = None
            return
        if old.known:
            origin = Origin.CORRECTED
        self._record(slot, SlotEntry(status=SlotStatus.KNOWN, value=value, origin=origin))
        if origin is Origin.CORRECTED:
            self.note(
                NoteCode.CORRECTED,
                self.words.corrected.format(value=self.words.label(slot, value)),
                slot,
            )
            if slot in ESSENTIAL:
                self._clear_immaterial_facets()

    def set_status(self, slot: Slot, status: SlotStatus) -> None:
        self._record(slot, SlotEntry(status=status))

    def _clear_immaterial_facets(self) -> None:
        """R6: facets that no longer apply are cleared once both essentials are known."""
        cat, acct = self.category, self.account
        if cat is None or acct is None:
            return
        material = {slot_for_facet(f) for f in self.cov.cell(cat, acct).material_facets}
        for slot in FACET_SLOTS:
            if slot not in material and self.entry(slot).status is not SlotStatus.UNKNOWN:
                self._record(slot, SlotEntry())

    @property
    def category(self) -> CustomerCategory | None:
        e = self.entry(Slot.CUSTOMER_CATEGORY)
        return CustomerCategory(e.value) if e.known and e.value else None

    @property
    def account(self) -> AccountType | None:
        e = self.entry(Slot.ACCOUNT_TYPE)
        return AccountType(e.value) if e.known and e.value else None

    def material(self, slot: Slot) -> bool:
        """Essentials always matter; a facet only for a covered cell whose form has it (R3)."""
        if slot in ESSENTIAL:
            return True
        cat, acct = self.category, self.account
        if cat is None or acct is None:
            return False
        cell = self.cov.cell(cat, acct)
        return cell.covered and slot in {slot_for_facet(f) for f in cell.material_facets}

    @property
    def ready_profile(self) -> bool:
        return self.category is not None and self.account is not None

    def subject(self) -> str:
        """'a current account for a limited liability partnership (LLP)', from the profile."""
        acct, cat = self.account, self.category
        return self.words.replies.subject.format(
            account=_lower_first(self.words.label(Slot.ACCOUNT_TYPE, acct.value) if acct else ""),
            holder=_lower_first(self.words.label(Slot.CUSTOMER_CATEGORY, cat.value) if cat else ""),
        )

    def holder(self) -> str:
        cat = self.category
        return _lower_first(self.words.label(Slot.CUSTOMER_CATEGORY, cat.value)) if cat else ""

    def confirm(self, slot: Slot, value: str, reason: ConfirmReason) -> None:
        if self.new_confirm:
            return  # one question per turn: the first proposal wins
        self.pending = Pending(kind=PendingKind.CONFIRM, slot=slot, value=value, reason=reason)
        self.new_confirm = True


def advance(
    state: DialogueState,
    turn: TurnInput,
    interpretation: Interpretation | None = None,
    *,
    llm_hints: tuple[LLMHint, ...] = (),
    cov: Coverage | None = None,
    words: Wording | None = None,
) -> tuple[DialogueState, DialogueResponse]:
    """One deterministic turn. `interpretation` is required when the turn has a message."""
    cov = cov or coverage()
    words = words or wording()
    state = state.model_copy(update={"turn": state.turn + 1})
    if turn.message is not None:
        if interpretation is None:
            interpretation = interpret(turn.message)
        if interpretation.blocked:
            return state, _blocked(interpretation.screen, words)
    w = _Work(state, cov, words)
    previous_pending = w.pending

    if turn.action is not None:
        _apply_action(w, turn)
    u = interpretation.understanding if interpretation is not None else None
    if u is not None:
        _apply_understanding(w, u, previous_pending, llm_hints, explicit=turn.action is not None)
    response = _decide(w)
    if u is not None and u.out_of_scope:
        w.fallback = True  # A1 §7 case 4; the next question still rides along
    if (
        response.step is StepKind.READY
        and turn.action is None
        and u is not None
        and not u.substantive
        and u.control is None
    ):
        # Nothing in the message was understood ("bleeh", an instruction to the assistant):
        # nothing changes and there is nothing to answer, so it is out of scope (A1 §7).
        w.fallback = True
    if w.fallback and response.step in (StepKind.ASK, StepKind.READY, StepKind.CANNOT_BUILD):
        response = response.model_copy(
            update={"step": StepKind.FALLBACK, "fallback": words.fallback, "notice": words.notice}
        )
    pending_confirm = w.pending is not None and w.pending.kind is PendingKind.CONFIRM
    if response.step is not StepKind.CONFIRM and pending_confirm:
        w.pending = None
    new_state = state.model_copy(
        update={
            "slots": w.slots,
            "history": tuple(w.history),
            "pending": w.pending,
            "unclear": w.unclear,
            "organisation_hint": w.organisation_hint,
            "intent": w.intent,
            "open_topics": tuple(w.topics),
        }
    )
    unchanged = response.ready is not None and (
        dict(w.slots) == dict(state.slots) and tuple(w.topics) == state.open_topics
    )
    return new_state, response.model_copy(update={"notes": tuple(w.notes), "unchanged": unchanged})


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _inline(label: str) -> str:
    """A chip label inside a sentence: "A company ..." -> "a company ...", "FCNR ..." kept."""
    if len(label) > 1 and (label[1].islower() or label[1] == " "):
        return label[0].lower() + label[1:]
    return label


def _apply_action(w: _Work, turn: TurnInput) -> None:
    a, slot = turn.action, turn.slot
    if a is Action.CONFIRM_YES or a is Action.CONFIRM_NO:
        _resolve_confirm(w, a is Action.CONFIRM_YES)
        return
    assert slot is not None  # guaranteed by TurnInput validation  # noqa: S101
    if a is Action.CHOOSE or a is Action.CORRECT:
        assert turn.value is not None  # noqa: S101
        w.set_value(slot, turn.value, Origin.CORRECTED if a is Action.CORRECT else Origin.CHOSEN)
    elif a is Action.NOT_SURE:
        _not_sure(w, slot)
    elif a is Action.SKIP:
        _skip(w, slot)
    elif a is Action.EXPLAIN:
        _explain(w, slot)


def _resolve_confirm(w: _Work, yes: bool) -> None:
    p = w.pending
    if p is None or p.kind is not PendingKind.CONFIRM or p.value is None:
        return
    w.pending = None
    if yes:
        origin = Origin.CORRECTED if p.reason is ConfirmReason.CONFLICT else Origin.CHOSEN
        w.set_value(p.slot, p.value, origin)
    else:
        w.note(NoteCode.CONFIRM_DECLINED, w.words.confirm_declined, p.slot)


def _not_sure(w: _Work, slot: Slot) -> None:
    w.set_status(slot, SlotStatus.NOT_SURE)
    w.note(NoteCode.NOT_SURE, w.words.slots[slot].not_sure, slot)


def _skip(w: _Work, slot: Slot) -> None:
    if slot in ESSENTIAL:
        w.note(NoteCode.SKIP_NOT_AVAILABLE, w.words.skip_not_available, slot)
        w.force_ask = slot
        return
    w.set_status(slot, SlotStatus.SKIPPED)
    w.note(NoteCode.SKIPPED, w.words.skipped, slot)


def _explain(w: _Work, slot: Slot) -> None:
    w.note(NoteCode.EXPLANATION, w.words.slots[slot].explanation, slot)
    w.force_ask = slot


def _apply_understanding(
    w: _Work,
    u: Understanding,
    previous: Pending | None,
    llm_hints: tuple[LLMHint, ...],
    *,
    explicit: bool,
) -> None:
    # 1. Control replies to the question asked last turn (only when no chip was used).
    if not explicit and previous is not None and u.control is not None:
        if previous.kind is PendingKind.CONFIRM and u.control in (Control.YES, Control.NO):
            _resolve_confirm(w, u.control is Control.YES)
        elif previous.kind is PendingKind.QUESTION and previous.slot not in u.values:
            if u.control is Control.NOT_SURE:
                _not_sure(w, previous.slot)
            elif u.control is Control.SKIP:
                _skip(w, previous.slot)
            elif u.control is Control.EXPLAIN:
                _explain(w, previous.slot)
            elif u.control in (Control.YES, Control.NO) and valid_value(
                previous.slot, u.control.value.upper()
            ):  # a yes / no question (minor): the reply is the answer
                w.set_value(previous.slot, u.control.value.upper(), Origin.STATED)
    if previous is not None and previous.kind is PendingKind.CONFIRM and w.pending == previous:
        w.pending = None  # anything but yes / no leaves the proposal unconfirmed (R7)

    # 1b. Another account ("I also want one for my child"): this conversation's profile and
    # checklist stay as they are; the customer is offered a separate checklist instead.
    if u.separate_request and w.ready_profile and not explicit:
        w.note(NoteCode.SEPARATE_REQUEST, w.words.context.separate)
        return

    # 2. Values stated in words. A value for a question that does not apply to the current
    # account (e.g. "for a minor" on an LLP current account) is never stored: it would merge
    # two incompatible contexts. A minor mentioned that way is read as a child (step 3b).
    child_mentioned = False
    for slot, value in u.values.items():
        if slot in FACET_SLOTS and w.ready_profile and not w.material(slot):
            child_mentioned |= slot is Slot.MINOR and value == "YES"
            if not (slot is Slot.MINOR and value == "YES"):
                w.note(NoteCode.NOT_APPLICABLE,
                       w.words.context.not_applicable.format(subject=w.subject()))  # fmt: skip
            continue
        entry = w.entry(slot)
        if entry.known and entry.value != value and not u.correction_cue:
            w.confirm(slot, value, ConfirmReason.CONFLICT)  # never silently overwrite
        else:
            w.set_value(slot, value, Origin.STATED)
    for slot in u.ambiguous:
        if not w.entry(slot).known and w.material(slot):
            w.note(NoteCode.AMBIGUOUS, w.words.ambiguous, slot)
            w.force_ask = w.force_ask or slot
            break

    # 3. Context.
    if u.organisation_unspecified and not w.entry(Slot.CUSTOMER_CATEGORY).known:
        w.organisation_hint = True
        w.note(NoteCode.ORGANISATION_UNSPECIFIED, w.words.organisation_unspecified,
               Slot.CUSTOMER_CATEGORY)  # fmt: skip
    if u.unsupported is not None and not w.entry(Slot.CUSTOMER_CATEGORY).known:
        w.note(NoteCode.UNSUPPORTED_CUSTOMER, w.words.unsupported_customer, Slot.CUSTOMER_CATEGORY)
        w.fallback = True
    if u.intent is not None:
        w.intent = u.intent
    for t in u.topics:
        if t.value not in w.topics:
            w.topics.append(t.value)
    if u.out_of_scope:
        w.note(NoteCode.OUT_OF_SCOPE, w.words.out_of_scope)

    # 3b. Who the message is about, read against the profile (never a value by itself).
    person = u.person or (Person.CHILD if child_mentioned else None)
    category_stated = Slot.CUSTOMER_CATEGORY in u.values or Slot.CUSTOMER_CATEGORY in u.ambiguous
    if person is not None and not explicit and not category_stated:
        cat = w.category
        if cat is None:
            w.individuals_first = True  # the category question lists individuals first
        elif cat in ORGANISATIONS:
            # An account for a person (or a child) while the profile is an organisation's:
            # explain, ask once, change nothing until the customer chooses.
            w.individuals_first = True
            w.clarify = Slot.CUSTOMER_CATEGORY
            if previous is not None and previous.slot is Slot.CUSTOMER_CATEGORY:
                w.note(NoteCode.CONTEXT_CHANGE, w.words.context.which_person,
                       Slot.CUSTOMER_CATEGORY)  # fmt: skip
            elif u.correction_cue:
                w.note(NoteCode.CONTEXT_CHANGE,
                       w.words.context.change.format(holder=w.holder()),
                       Slot.CUSTOMER_CATEGORY)  # fmt: skip
            else:
                w.note(NoteCode.CONTEXT_CONFLICT,
                       w.words.context.conflict.format(subject=w.subject(), holder=w.holder()),
                       Slot.CUSTOMER_CATEGORY)  # fmt: skip
        elif (
            person is Person.CHILD
            and Slot.MINOR not in u.values
            and w.material(Slot.MINOR)
            and w.entry(Slot.MINOR).value != "YES"
        ):
            # An individual's account and a child mentioned: the minor question, confirmed
            # first (a hint if unanswered, a conflict if the answer was "no").
            reason = ConfirmReason.CONFLICT if w.entry(Slot.MINOR).known else ConfirmReason.HINT
            w.confirm(Slot.MINOR, "YES", reason)

    # 3c. A follow-up about the current checklist ("does that apply to me?"): nothing
    # changes; the reply relates it to the account being discussed.
    if (
        u.follow_up
        and not explicit
        and w.ready_profile
        and w.clarify is None
        and not u.values
        and not w.new_confirm
    ):
        w.note(NoteCode.FOLLOW_UP, w.words.context.follow_up.format(subject=w.subject()))

    # 4. Hints and LLM proposals become confirmation questions, never values (R7).
    proposals = [(h.slot, h.value, ConfirmReason.HINT) for h in u.hints]
    proposals += [(h.slot, h.value, ConfirmReason.LLM) for h in llm_hints]
    for slot, value, reason in proposals:
        if not w.entry(slot).known and w.material(slot):
            w.confirm(slot, value, reason)

    # 5. An unclear reply to a question: one re-ask, then "not sure" (bounded; R4).
    answered = previous is not None and not w.entry(previous.slot).open
    if (
        previous is not None
        and previous.kind is PendingKind.QUESTION
        and not answered
        and u.control is None
        and not explicit
        and not u.substantive
        and not llm_hints
    ):
        count = w.unclear.get(previous.slot, 0) + 1
        if count > MAX_UNCLEAR:
            w.set_status(previous.slot, SlotStatus.NOT_SURE)
            w.note(NoteCode.UNCLEAR_GAVE_UP, w.words.unclear_gave_up, previous.slot)
        else:
            w.unclear[previous.slot] = count
            w.note(NoteCode.UNCLEAR, w.words.unclear, previous.slot)
            w.force_ask = previous.slot


# --- the next step -------------------------------------------------------------------------------


def _decide(w: _Work) -> DialogueResponse:
    if w.pending is not None and w.pending.kind is PendingKind.CONFIRM and w.new_confirm:
        return _confirmation(w)
    if w.clarify is not None:
        return _ask(w, w.clarify)  # a known slot asked again: the context is unclear
    if w.force_ask is not None and not w.entry(w.force_ask).known:
        return _ask(w, w.force_ask)
    if w.intent is Intent.RESIDENT_TO_NRO_CONVERSION:
        return _ready(w, CoverageState.CONVERSION_ONLY, material=())
    cat, acct = w.category, w.account
    cat_e, acct_e = w.entry(Slot.CUSTOMER_CATEGORY), w.entry(Slot.ACCOUNT_TYPE)
    if cat is not None and acct is not None:
        cell = w.cov.cell(cat, acct)
        if not cell.covered:
            alternatives: tuple[Suggestion, ...] = _suggest(
                w, Slot.ACCOUNT_TYPE, w.cov.covered_accounts(cat)
            ) or _suggest(
                w,
                Slot.CUSTOMER_CATEGORY,
                # Only the same kind of holder: an individual is never offered a company.
                [c for c in w.cov.covered_categories(acct)
                 if (c in ORGANISATIONS) == (cat in ORGANISATIONS)],
            )  # fmt: skip
            return _not_established(
                w,
                cell.reason or DEFAULT_NOT_ESTABLISHED,
                w.cov.not_established_explanation(cat, acct),
                alternatives,
            )
        material = tuple(slot_for_facet(f) for f in cell.material_facets)
        for slot in FACET_SLOTS:  # fixed asking order
            if slot in material and w.entry(slot).open:
                return _ask(w, slot)
        return _ready(w, cell.state, material)
    if acct is not None:
        if not any(w.cov.cell(c, acct).covered for c in CustomerCategory):
            covered = [a for a in AccountType if w.cov.covered_categories(a)]
            return _not_established(  # the category is immaterial
                w,
                _account_reason(w.cov, acct),
                w.cov.not_established_explanation(None, acct),
                _suggest(w, Slot.ACCOUNT_TYPE, covered),
            )
        return _ask(w, Slot.CUSTOMER_CATEGORY) if cat_e.open else _cannot_build(w)
    if cat is not None:
        if not any(w.cov.cell(cat, a).covered for a in AccountType):
            return _not_established(
                w,
                w.cov.not_established_reason(cat, AccountType.SAVINGS),
                w.cov.not_established_explanation(cat, None),
            )
        return _ask(w, Slot.ACCOUNT_TYPE) if acct_e.open else _cannot_build(w)
    if acct_e.open:
        return _ask(w, Slot.ACCOUNT_TYPE)  # R2: account type first
    if cat_e.open:
        return _ask(w, Slot.CUSTOMER_CATEGORY)
    return _cannot_build(w)


def _account_reason(cov: Coverage, acct: AccountType) -> str:
    for r in cov.not_established_reasons:
        if r.category is None and r.account_type is acct:
            return r.reason
    return DEFAULT_NOT_ESTABLISHED


def _ask(w: _Work, slot: Slot) -> DialogueResponse:
    words = w.words.slots[slot]
    values = list(words.chips)
    if slot is Slot.CUSTOMER_CATEGORY and w.organisation_hint:
        orgs = [c.value for c in ORGANISATIONS]
        values = orgs + [v for v in values if v not in orgs]  # all options stay available (R8)
    if slot is Slot.CUSTOMER_CATEGORY and w.individuals_first:
        # Individuals first; the current answer next (choosing it keeps the checklist).
        first = [c.value for c in CustomerCategory if c not in ORGANISATIONS]
        current = [w.category.value] if w.category is not None else []
        lead = first + [v for v in current if v not in first]
        values = lead + [v for v in values if v not in lead]
    chips = tuple(Chip(value=v, label=words.chips[v]) for v in values)
    w.pending = Pending(kind=PendingKind.QUESTION, slot=slot)
    return DialogueResponse(
        step=StepKind.ASK,
        question=Question(slot=slot, text=words.question, chips=chips,
                          allow_skip=slot not in ESSENTIAL),  # fmt: skip
    )


def _confirmation(w: _Work) -> DialogueResponse:
    p = w.pending
    assert p is not None and p.value is not None and p.reason is not None  # noqa: S101
    label = _inline(w.words.label(p.slot, p.value))
    if p.reason is ConfirmReason.CONFLICT:
        old = w.entry(p.slot).value
        text = w.words.confirm.conflict.format(
            old=_inline(w.words.label(p.slot, old)) if old else "", value=label
        )
    elif p.reason is ConfirmReason.LLM:
        text = w.words.confirm.llm.format(value=label)
    else:
        text = w.words.confirm.hint.format(value=label)
    return DialogueResponse(
        step=StepKind.CONFIRM,
        confirmation=Confirmation(slot=p.slot, value=p.value, reason=p.reason, text=text),
    )


def _ready(w: _Work, state: CoverageState, material: tuple[Slot, ...]) -> DialogueResponse:
    w.pending = None
    facets = {s: (w.entry(s).value if w.entry(s).known else None) for s in material}
    return DialogueResponse(
        step=StepKind.READY,
        ready=ReadyProfile(
            intent=w.intent or Intent.OPEN_ACCOUNT,
            customer_category=w.category,
            account_type=w.account,
            coverage_state=state,
            facets=facets,
            topics=tuple(w.topics),
        ),
    )


def _suggest(w: _Work, slot: Slot, values: Iterable[StrEnum]) -> tuple[Suggestion, ...]:
    return tuple(
        Suggestion(slot=slot, value=v.value, label=w.words.label(slot, v.value)) for v in values
    )


def _not_established(
    w: _Work, reason: str, explanation: str, suggestions: tuple[Suggestion, ...] = ()
) -> DialogueResponse:
    w.pending = None
    return DialogueResponse(
        step=StepKind.NOT_ESTABLISHED,
        reason=reason,
        explanation=explanation,
        suggestions=suggestions,
        fallback=w.words.fallback,
        notice=w.words.notice,
    )


def _cannot_build(w: _Work) -> DialogueResponse:
    w.pending = None
    missing = [s for s in (Slot.ACCOUNT_TYPE, Slot.CUSTOMER_CATEGORY) if not w.entry(s).known]
    return DialogueResponse(
        step=StepKind.CANNOT_BUILD,
        reason=" ".join(w.words.cannot_build[s] for s in missing),
    )


def _blocked(res: ScreenResult, words: Wording) -> DialogueResponse:
    if res.sensitive:
        text = words.blocked.identifier
    elif res.unusable:
        text = words.blocked.unusable
    else:
        text = words.blocked.other
    return DialogueResponse(step=StepKind.BLOCKED, blocked=text)


# --- entry points --------------------------------------------------------------------------------


def opening(
    *, cov: Coverage | None = None, words: Wording | None = None
) -> tuple[DialogueState, DialogueResponse]:
    """The first question of a new conversation (A5: session creation). Nothing has been said
    yet, so the turn counter stays at 0; the question is pending so that a free-text reply
    to it is read as its answer."""
    w = _Work(DialogueState(), cov or coverage(), words or wording())
    response = _decide(w)
    return DialogueState().model_copy(update={"pending": w.pending}), response


def respond_deterministic(
    state: DialogueState, turn: TurnInput
) -> tuple[DialogueState, DialogueResponse]:
    """The guided path without any LLM (A1 D-D: everything core works this way)."""
    interp = interpret(turn.message) if turn.message is not None else None
    return advance(state, turn, interp)


async def respond(
    state: DialogueState,
    turn: TurnInput,
    *,
    llm: LLMProvider | None = None,
    llm_timeout_s: float = 15.0,
    context: str | None = None,
) -> tuple[DialogueState, DialogueResponse]:
    """As `respond_deterministic`, plus the optional LLM: consulted only for a free-text
    message the rules could not read at all, and only to PROPOSE slot values, each of which
    the customer then confirms. Any LLM failure falls back to the deterministic path."""
    interp = interpret(turn.message) if turn.message is not None else None
    hints: tuple[LLMHint, ...] = ()
    consulted = False
    if (
        llm is not None
        and interp is not None
        and interp.understanding is not None
        and interp.llm_allowed
        and turn.action is None
        and not interp.understanding.substantive
        and interp.understanding.control is None
        and interp.screen.text is not None
    ):
        open_slots = tuple(s for s in state.slots if not state.entry(s).known)
        if open_slots:
            consulted = True
            hints = await extract_with_llm(
                llm, interp.screen.text, open_slots, timeout_s=llm_timeout_s, context=context
            )
    new_state, response = advance(state, turn, interp, llm_hints=hints)
    return new_state, response.model_copy(update={"llm_consulted": consulted})
