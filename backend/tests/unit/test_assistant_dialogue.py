"""A3: the dialogue policy (A1 §6 R1-R10), with no LLM. One question per turn, material
follow-ups only (from the coverage map), "I'm not sure" / "Skip" / "What does this mean?",
corrections, bounded re-asks, and never a guessed value."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.assistant.dialogue import (
    Action,
    DialogueResponse,
    NoteCode,
    StepKind,
    TurnInput,
    respond_deterministic,
)
from app.assistant.profile import (
    ConfirmReason,
    DialogueState,
    Origin,
    PendingKind,
    Slot,
    SlotStatus,
)
from app.assistant.taxonomy import AccountType, CoverageState, CustomerCategory, Intent
from app.assistant.wording import wording

A, C = AccountType, CustomerCategory
ACC, CAT = Slot.ACCOUNT_TYPE, Slot.CUSTOMER_CATEGORY
FALLBACK = (
    "I couldn't find this information in the available HDFC Bank documents. "
    "I don't want to give you an incorrect answer."
)
NOTICE = (
    "These answers are based on HDFC Bank documents held in this prototype. The documents are "
    "dated, and their current validity has not been independently verified. Please confirm the "
    "requirements with HDFC Bank before you apply."
)
SYNTHETIC_PAN = "ABCPE1234F"  # well-formed but synthetic


class Chat:
    """Drives the deterministic dialogue and keeps every response."""

    def __init__(self) -> None:
        self.state = DialogueState()
        self.responses: list[DialogueResponse] = []

    def say(self, message: str) -> DialogueResponse:
        return self._turn(TurnInput(message=message))

    def act(
        self, action: Action, slot: Slot | None = None, value: str | None = None
    ) -> DialogueResponse:
        return self._turn(TurnInput(action=action, slot=slot, value=value))

    def choose(self, slot: Slot, value: str) -> DialogueResponse:
        return self.act(Action.CHOOSE, slot, value)

    def _turn(self, turn: TurnInput) -> DialogueResponse:
        self.state, r = respond_deterministic(self.state, turn)
        self.responses.append(r)
        assert_one_question(r)
        return r

    def entry(self, slot: Slot) -> Any:
        return self.state.entry(slot)


def assert_one_question(r: DialogueResponse) -> None:
    """R4: at most one question per turn, and the step carries what it needs."""
    assert not (r.question and r.confirmation)
    if r.step is StepKind.ASK:
        assert r.question is not None
    if r.step is StepKind.CONFIRM:
        assert r.confirmation is not None
    if r.step in (StepKind.NOT_ESTABLISHED, StepKind.FALLBACK):
        assert r.fallback == FALLBACK and r.notice == NOTICE
    if r.step is StepKind.READY:
        assert r.ready is not None and r.question is None


def asks(r: DialogueResponse, slot: Slot) -> bool:
    return r.question is not None and r.question.slot is slot


# --- R1 / R2: essentials, account type first ---------------------------------------------------


def test_with_nothing_known_the_account_type_is_asked_first() -> None:
    r = Chat().say("Hello, I'd like to open an account")
    assert r.step is StepKind.ASK and asks(r, ACC)
    assert r.question is not None
    assert r.question.text == "Which account would you like to open?"
    assert [c.value for c in r.question.chips] == [a.value for a in AccountType]
    assert r.question.allow_not_sure and r.question.allow_explain and not r.question.allow_skip


def test_when_the_account_type_is_named_only_the_category_is_asked() -> None:
    chat = Chat()
    r = chat.say("I want to open a current account")
    assert asks(r, CAT) and chat.entry(ACC).origin is Origin.STATED


def test_when_the_category_is_named_only_the_account_type_is_asked() -> None:
    assert asks(Chat().say("It is for my LLP"), ACC)


def test_chips_complete_an_organisation_profile_without_facets() -> None:
    chat = Chat()
    chat.choose(ACC, "CURRENT")
    r = chat.choose(CAT, "LLP")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.coverage_state is CoverageState.COVERED_OCR
    assert r.ready.facets == {}  # the non-individual form has no NRI / joint / minor sections
    assert chat.entry(CAT).origin is Origin.CHOSEN


# --- R3: only material follow-ups, from the coverage map ---------------------------------------


def test_nri_savings_asks_scheme_then_holding_then_minor() -> None:
    chat = Chat()
    chat.choose(ACC, "SAVINGS")
    order = []
    r = chat.choose(CAT, "NRI_INDIVIDUAL")
    while r.step is StepKind.ASK:
        assert r.question is not None
        order.append(r.question.slot)
        r = chat.choose(r.question.slot, r.question.chips[0].value)
    assert order == [Slot.NRI_SCHEME, Slot.HOLDING, Slot.MINOR]
    assert r.ready is not None and r.ready.facets == {
        Slot.NRI_SCHEME: "NRE", Slot.HOLDING: "SINGLE", Slot.MINOR: "YES",
    }  # fmt: skip


def test_fcnr_asks_only_holding() -> None:
    chat = Chat()
    chat.say("FCNR deposit")
    r = chat.choose(CAT, "NRI_INDIVIDUAL")
    assert asks(r, Slot.HOLDING)
    r = chat.choose(Slot.HOLDING, "JOINT")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.facets == {Slot.HOLDING: "JOINT"}


def test_facets_stated_up_front_are_not_asked_again() -> None:
    chat = Chat()
    r = chat.say("I'm an NRI and want a joint NRE savings account with my wife")
    assert asks(r, Slot.MINOR)  # scheme and holding were stated; only minor remains


def test_facets_are_never_asked_before_both_essentials_are_known() -> None:
    chat = Chat()
    r = chat.say("What is the difference between NRE and NRO?")
    assert not asks(r, Slot.NRI_SCHEME)


# --- not established and the fallback ----------------------------------------------------------


@pytest.mark.parametrize(
    ("turns", "phrase"),
    [
        ([("CURRENT", ACC), ("RESIDENT_INDIVIDUAL", CAT)], "resident individuals"),
        ([("SAVINGS", ACC), ("LLP", CAT)], "savings account"),
        ([("FCNR_RFC", ACC), ("COMPANY", CAT)], "NRI individual form"),
    ],
)
def test_unsupported_combinations_get_the_fallback(
    turns: list[tuple[str, Slot]], phrase: str
) -> None:
    chat = Chat()
    for value, slot in turns:
        r = chat.choose(slot, value)
    assert r.step is StepKind.NOT_ESTABLISHED and r.reason and phrase in r.reason
    assert r.fallback == FALLBACK and r.notice == NOTICE and r.ready is None


def test_salary_needs_no_category_question() -> None:
    r = Chat().say("I want to open a salary account")
    assert r.step is StepKind.NOT_ESTABLISHED and r.reason and "salary account" in r.reason


def test_resident_individuals_need_no_account_question() -> None:
    chat = Chat()
    r = chat.choose(CAT, "RESIDENT_INDIVIDUAL")
    assert r.step is StepKind.NOT_ESTABLISHED and r.reason and "resident individuals" in r.reason


def test_fixed_texts_match_the_specification() -> None:
    words = wording()
    assert words.fallback == FALLBACK and words.notice == NOTICE
    spec = (__import__("pathlib").Path(__file__).resolve().parents[3]
            / "docs/assistant/A1_PRODUCT_SPEC.md").read_text(encoding="utf-8")  # fmt: skip
    assert FALLBACK in spec and NOTICE in spec


# --- R4: not sure, skip, what does this mean, unclear replies ----------------------------------


def test_not_sure_about_the_account_type_moves_on_then_explains_the_gap() -> None:
    chat = Chat()
    chat.say("hi")
    r = chat.act(Action.NOT_SURE, ACC)
    assert asks(r, CAT) and [n.code for n in r.notes] == [NoteCode.NOT_SURE]
    assert chat.entry(ACC).status is SlotStatus.NOT_SURE
    r = chat.say("I'm an NRI")
    assert r.step is StepKind.CANNOT_BUILD
    assert r.reason == wording().cannot_build[ACC]


def test_not_sure_on_both_essentials_explains_both() -> None:
    chat = Chat()
    chat.act(Action.NOT_SURE, ACC)
    r = chat.say("not sure")  # free-text answer to the category question
    assert r.step is StepKind.CANNOT_BUILD
    assert r.reason == f"{wording().cannot_build[ACC]} {wording().cannot_build[CAT]}"


def test_a_not_sure_essential_can_still_lead_to_the_fallback() -> None:
    chat = Chat()
    chat.act(Action.NOT_SURE, ACC)
    r = chat.choose(CAT, "RESIDENT_INDIVIDUAL")
    assert r.step is StepKind.NOT_ESTABLISHED  # no account type would change that


def test_not_sure_on_a_facet_keeps_it_open_and_continues() -> None:
    chat = Chat()
    chat.say("NRI savings account")
    r = chat.say("I'm not sure")
    assert asks(r, Slot.HOLDING) and chat.entry(Slot.NRI_SCHEME).status is SlotStatus.NOT_SURE
    assert r.notes[0].text == wording().slots[Slot.NRI_SCHEME].not_sure


def test_skip_is_offered_and_works_for_facets() -> None:
    chat = Chat()
    r = chat.say("NRI current account")
    assert asks(r, Slot.NRI_SCHEME) and r.question is not None and r.question.allow_skip
    r = chat.act(Action.SKIP, Slot.NRI_SCHEME)
    assert asks(r, Slot.HOLDING) and r.notes[0].code is NoteCode.SKIPPED
    chat.say("skip")
    r = chat.say("skip")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.facets == {Slot.NRI_SCHEME: None, Slot.HOLDING: None, Slot.MINOR: None}


def test_skip_is_refused_for_essentials() -> None:
    chat = Chat()
    chat.say("hello")
    r = chat.act(Action.SKIP, ACC)
    assert asks(r, ACC) and r.notes[0].code is NoteCode.SKIP_NOT_AVAILABLE
    assert chat.entry(ACC).status is SlotStatus.UNKNOWN


def test_what_does_this_mean_explains_and_asks_the_same_question() -> None:
    chat = Chat()
    chat.say("NRI savings")
    for r in (chat.act(Action.EXPLAIN, Slot.NRI_SCHEME), chat.say("what does this mean?")):
        assert asks(r, Slot.NRI_SCHEME)
        assert r.notes[0].code is NoteCode.EXPLANATION
        assert r.notes[0].text == wording().slots[Slot.NRI_SCHEME].explanation
    assert chat.entry(Slot.NRI_SCHEME).status is SlotStatus.UNKNOWN


def test_an_unclear_reply_is_re_asked_once_then_left_open() -> None:
    chat = Chat()
    chat.say("hello")
    r = chat.say("purple elephants")
    assert asks(r, ACC) and r.notes[0].code is NoteCode.UNCLEAR
    r = chat.say("more purple elephants")
    assert r.notes[0].code is NoteCode.UNCLEAR_GAVE_UP and asks(r, CAT)  # no loop
    assert chat.entry(ACC).status is SlotStatus.NOT_SURE


def test_yes_or_no_answers_the_minor_question() -> None:
    chat = Chat()
    chat.say("NRI savings account, NRE, just me")
    r = chat.say("no")
    assert r.step is StepKind.READY and chat.entry(Slot.MINOR).value == "NO"


# --- R6: corrections ---------------------------------------------------------------------------


def test_a_chip_correction_replaces_the_value_and_keeps_history() -> None:
    chat = Chat()
    chat.choose(ACC, "CURRENT")
    chat.choose(CAT, "LLP")
    r = chat.act(Action.CORRECT, CAT, "COMPANY")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.customer_category is C.COMPANY
    assert chat.entry(CAT).origin is Origin.CORRECTED
    assert any(n.code is NoteCode.CORRECTED for n in r.notes)
    events = [e for e in chat.state.history if e.slot is CAT]
    assert [(e.value, e.superseded) for e in events] == [("LLP", True), ("COMPANY", False)]


def test_a_correction_in_words_needs_a_cue() -> None:
    chat = Chat()
    chat.say("current account for my LLP")
    r = chat.say("Actually it's for my company")
    assert r.step is StepKind.READY and chat.entry(CAT).value == "COMPANY"
    assert chat.entry(CAT).origin is Origin.CORRECTED


def test_a_conflicting_answer_without_a_cue_is_confirmed_not_overwritten() -> None:
    chat = Chat()
    chat.say("current account for my LLP")
    r = chat.say("partnership firm")
    assert r.step is StepKind.CONFIRM and r.confirmation is not None
    assert r.confirmation.reason is ConfirmReason.CONFLICT
    assert "limited liability partnership" in r.confirmation.text
    assert chat.entry(CAT).value == "LLP"  # unchanged until confirmed
    r = chat.act(Action.CONFIRM_YES)
    assert (
        chat.entry(CAT).value == "PARTNERSHIP_FIRM" and chat.entry(CAT).origin is Origin.CORRECTED
    )


def test_declining_a_conflict_keeps_the_earlier_answer() -> None:
    chat = Chat()
    chat.say("current account for my LLP")
    chat.say("partnership firm")
    r = chat.say("no")
    assert chat.entry(CAT).value == "LLP" and r.notes[0].code is NoteCode.CONFIRM_DECLINED
    assert r.step is StepKind.READY


def test_a_correction_clears_facets_that_no_longer_apply() -> None:
    chat = Chat()
    chat.say("I'm an NRI and want a joint NRE current account")
    assert chat.entry(Slot.HOLDING).value == "JOINT"
    r = chat.act(Action.CORRECT, CAT, "LLP")
    assert r.step is StepKind.READY and r.ready is not None and r.ready.facets == {}
    for slot in (Slot.NRI_SCHEME, Slot.HOLDING):
        assert chat.entry(slot).status is SlotStatus.UNKNOWN
        assert any(e.slot is slot and e.superseded for e in chat.state.history)


def test_a_correction_keeps_facets_that_still_apply() -> None:
    chat = Chat()
    chat.say("I'm an NRI, NRE savings, just me, not for a minor")
    chat.choose(Slot.MINOR, "NO")
    r = chat.say("Sorry, I meant a current account")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.account_type is A.CURRENT and r.ready.facets[Slot.NRI_SCHEME] == "NRE"


# --- R7: never guess ---------------------------------------------------------------------------


def test_living_abroad_is_confirmed_before_it_becomes_nri() -> None:
    chat = Chat()
    r = chat.say("I live in Dubai and want a fixed deposit")
    assert r.step is StepKind.CONFIRM and r.confirmation is not None
    assert r.confirmation.value == "NRI_INDIVIDUAL" and r.confirmation.reason is ConfirmReason.HINT
    assert r.confirmation.text == (
        "Just to check: is the account for an individual living outside India (NRI / PIO / OCI)?"
    )
    assert chat.entry(CAT).status is SlotStatus.UNKNOWN
    r = chat.say("yes")
    assert chat.entry(CAT).value == "NRI_INDIVIDUAL" and chat.entry(CAT).origin is Origin.CHOSEN
    assert asks(r, Slot.NRI_SCHEME)


def test_declining_a_hint_asks_the_question_normally() -> None:
    chat = Chat()
    chat.say("I live in Dubai and want a fixed deposit")
    r = chat.act(Action.CONFIRM_NO)
    assert asks(r, CAT) and chat.entry(CAT).status is SlotStatus.UNKNOWN


def test_ignoring_a_confirmation_leaves_the_slot_unknown() -> None:
    chat = Chat()
    chat.say("I live in India")
    r = chat.say("what documents do I need?")
    assert chat.entry(CAT).status is SlotStatus.UNKNOWN and asks(r, ACC)
    assert chat.state.pending is not None and chat.state.pending.kind is PendingKind.QUESTION


def test_my_business_is_never_mapped_to_a_kind_of_organisation() -> None:
    chat = Chat()
    r = chat.say("I need a current account for my business")
    assert asks(r, CAT) and chat.entry(CAT).status is SlotStatus.UNKNOWN
    assert r.notes[0].code is NoteCode.ORGANISATION_UNSPECIFIED
    assert r.question is not None
    values = [c.value for c in r.question.chips]
    assert values[0] == "SOLE_PROPRIETORSHIP" and set(values) == {c.value for c in C}  # all kept


def test_an_ambiguous_answer_is_asked_again() -> None:
    chat = Chat()
    r = chat.say("savings or current account, which is better?")
    assert asks(r, ACC) and r.notes[0].code is NoteCode.AMBIGUOUS
    assert chat.entry(ACC).status is SlotStatus.UNKNOWN


def test_an_unsupported_customer_type_gets_the_fallback_and_no_category() -> None:
    chat = Chat()
    r = chat.say("I'm a foreign national and want a savings account")
    assert r.step is StepKind.FALLBACK and asks(r, CAT)
    assert r.notes[0].code is NoteCode.UNSUPPORTED_CUSTOMER
    assert chat.entry(CAT).status is SlotStatus.UNKNOWN
    assert chat.entry(ACC).value == "SAVINGS"


# --- R5 / R8: free text, item questions, out of scope, conversion ------------------------------


def test_item_questions_wait_for_the_profile_and_reach_ready() -> None:
    chat = Chat()
    r = chat.say("Does an LLP need a PAN card?")
    assert asks(r, ACC)
    r = chat.say("current")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.topics == ("pan_card_copy",)


def test_questions_after_ready_are_added() -> None:
    chat = Chat()
    chat.say("current account for my LLP")
    r = chat.say("Do I need a photograph?")
    assert r.step is StepKind.READY and r.ready is not None and r.ready.topics == ("photograph",)


def test_out_of_scope_gets_the_fallback_and_the_conversation_continues() -> None:
    chat = Chat()
    r = chat.say("What is the interest rate on an FD?")
    assert r.step is StepKind.FALLBACK and asks(r, CAT)  # the FD was still understood
    assert any(n.code is NoteCode.OUT_OF_SCOPE for n in r.notes)


def test_approval_questions_are_out_of_scope() -> None:
    r = Chat().say("Will my LLP current account be approved?")
    assert r.step is StepKind.FALLBACK


def test_conversion_is_its_own_intent() -> None:
    r = Chat().say("I have become an NRI. How do I convert my resident account to an NRO account?")
    assert r.step is StepKind.READY and r.ready is not None
    assert r.ready.intent is Intent.RESIDENT_TO_NRO_CONVERSION
    assert r.ready.coverage_state is CoverageState.CONVERSION_ONLY


# --- R9: screening -----------------------------------------------------------------------------


def test_identifiers_are_blocked_never_stored_or_echoed() -> None:
    chat = Chat()
    chat.say("NRI savings")
    before = chat.state
    r = chat.say(f"My PAN is {SYNTHETIC_PAN}, I'm an NRI")
    assert r.step is StepKind.BLOCKED and r.blocked == wording().blocked.identifier
    assert SYNTHETIC_PAN not in r.model_dump_json()
    assert chat.state.slots == before.slots and chat.state.pending == before.pending
    assert SYNTHETIC_PAN not in chat.state.model_dump_json()


def test_unusable_input_is_blocked() -> None:
    assert Chat().say("   ").step is StepKind.BLOCKED


def test_the_state_never_holds_the_customer_s_words() -> None:
    chat = Chat()
    chat.say("Hello, I am Priyamvada from Toronto and I want an NRE savings account")
    dumped = chat.state.model_dump_json().lower()
    assert "priyamvada" not in dumped and "toronto" not in dumped and "hello" not in dumped


# --- R10 and input validation ------------------------------------------------------------------


def test_no_approval_language_in_any_fixed_text() -> None:
    text = wording().model_dump_json().lower()
    for word in ("eligible", "approved", "rejected", "compliant", "mandatory", "required"):
        assert word not in text, word


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"action": Action.CHOOSE, "slot": ACC},
        {"action": Action.CHOOSE, "slot": ACC, "value": "LLP"},
        {"action": Action.NOT_SURE},
        {"action": Action.SKIP, "slot": ACC, "value": "SAVINGS"},
        {"message": "hi", "value": "SAVINGS"},
    ],
)
def test_invalid_turns_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TurnInput(**kwargs)


def test_a_full_conversation_is_deterministic() -> None:
    script = ["hi", "an NRE account", "yes", "savings", "jointly", "no"]
    runs = []
    for _ in range(2):
        chat = Chat()
        runs.append([chat.say(m).model_dump_json() for m in script])
    assert runs[0] == runs[1]
    assert chat.responses[-1].step is StepKind.READY
