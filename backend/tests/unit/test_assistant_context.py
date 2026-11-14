"""Context-aware follow-ups (owner request before A7).

A message is read against the conversation so far. The profile only changes when the customer
chooses or confirms; a follow-up never overwrites it; a message about a different kind of
account holder is clarified, never merged; a separate request keeps the current checklist.
No live LLM: the deterministic path (and the service with an in-memory store).
"""

import json
from typing import Any

from app.assistant.checklist import ChecklistService
from app.assistant.dialogue import StepKind, TurnInput, opening, respond_deterministic
from app.assistant.profile import Slot
from app.assistant.service import AssistantService
from app.assistant.understand import Person, understand
from app.assistant.wording import wording
from tests.unit.test_assistant_api import MemoryStore
from tests.unit.test_assistant_checklist import FakeChunks

PAN = "ABCPE1234F"


def choose(slot: str, value: str) -> dict[str, Any]:
    return {"action": "choose", "slot": slot, "value": value}


def say(message: str) -> dict[str, Any]:
    return {"message": message}


LLP_CURRENT = (choose("account_type", "CURRENT"), choose("customer_category", "LLP"))
NRI_SAVINGS = (
    say("I live in Dubai and want an NRE savings account"),
    {"action": "confirm_yes"},
    choose("holding", "SINGLE"),
    choose("minor", "NO"),
)


def run(*bodies: dict[str, Any]) -> Any:
    state, response = opening()
    for body in bodies:
        state, response = respond_deterministic(state, TurnInput.model_validate(body))
    return state, response


def codes(response: Any) -> list[str]:
    return [n.code.value for n in response.notes]


def assert_llp(state: Any) -> None:
    assert state.value(Slot.CUSTOMER_CATEGORY) == "LLP"
    assert state.value(Slot.ACCOUNT_TYPE) == "CURRENT"


# --- 1. the reported failure ------------------------------------------------------------------


def test_the_reported_conversation_never_merges_the_llp_and_the_child() -> None:
    # "being opened for a minor" does not apply to an LLP current account: never stored.
    state, r = run(*LLP_CURRENT, say("it is being opened for a minor"))
    assert state.entry(Slot.MINOR).status.value == "unknown"
    assert_llp(state)
    assert r.step is StepKind.ASK and r.question.slot is Slot.CUSTOMER_CATEGORY
    assert "context_conflict" in codes(r)
    # The child question: still clarified, still no change, no LLP checklist.
    state, r = respond_deterministic(
        state, TurnInput(message="How do I open an account for my six-year-old child?")
    )
    assert r.step is StepKind.ASK and r.ready is None
    assert_llp(state)
    # "For a person.": asks whether the person lives in India; still no change.
    state, r = respond_deterministic(state, TurnInput(message="For a person."))
    assert r.step is StepKind.ASK and r.question.slot is Slot.CUSTOMER_CATEGORY
    assert wording().context.which_person in [n.text for n in r.notes]
    assert_llp(state)


def test_the_clarification_lists_individuals_first_and_offers_to_keep_the_llp() -> None:
    _, r = run(*LLP_CURRENT, say("What about my child?"))
    chips = [c.value for c in r.question.chips]
    assert chips[:3] == ["RESIDENT_INDIVIDUAL", "NRI_INDIVIDUAL", "LLP"]
    assert len(chips) == len(set(chips)) == 9  # every option stays available
    text = next(n.text for n in r.notes if n.code.value == "context_conflict")
    assert "current account for a limited liability partnership (LLP)" in text
    assert "don't want to mix" in text


def test_keeping_the_llp_returns_the_same_checklist_unchanged() -> None:
    state, r = run(*LLP_CURRENT, say("What about my child?"), choose("customer_category", "LLP"))
    assert r.step is StepKind.READY and r.unchanged
    assert_llp(state)


# --- 2-3. informational follow-ups vs explicit corrections -----------------------------------


def test_an_informational_follow_up_does_not_modify_the_profile() -> None:
    state, r = run(*LLP_CURRENT, say("Does that apply to me?"))
    assert r.step is StepKind.READY and "follow_up" in codes(r)
    assert_llp(state)


def test_an_explicit_correction_asks_who_then_changes_on_the_choice() -> None:
    state, r = run(*LLP_CURRENT, say("Actually, I want the account for my child instead."))
    assert "context_change" in codes(r) and r.question.slot is Slot.CUSTOMER_CATEGORY
    assert_llp(state)  # nothing changes until the customer chooses
    state, r = respond_deterministic(
        state, TurnInput(action="choose", slot=Slot.CUSTOMER_CATEGORY, value="NRI_INDIVIDUAL")
    )
    assert state.value(Slot.CUSTOMER_CATEGORY) == "NRI_INDIVIDUAL"
    assert "corrected" in codes(r)
    assert r.step is StepKind.ASK  # the NRI form's own questions follow


# --- 4. ambiguous account-type change ------------------------------------------------------------


def test_an_account_type_in_a_follow_up_is_confirmed_before_it_changes() -> None:
    state, r = run(*LLP_CURRENT, say("what about a fixed deposit?"))
    assert r.step is StepKind.CONFIRM
    assert (r.confirmation.slot, r.confirmation.value) == (Slot.ACCOUNT_TYPE, "FIXED_DEPOSIT")
    assert_llp(state)
    state, r = respond_deterministic(state, TurnInput(action="confirm_no"))
    assert_llp(state) and r.step is StepKind.READY


# --- 5-7. the examples -------------------------------------------------------------------------


def test_what_about_my_child_for_an_individual_confirms_the_minor_question() -> None:
    state, r = run(*NRI_SAVINGS, say("What about my child?"))
    assert r.step is StepKind.CONFIRM
    assert (r.confirmation.slot, r.confirmation.value) == (Slot.MINOR, "YES")
    assert state.value(Slot.MINOR) == "NO"  # unchanged until confirmed


def test_does_that_apply_to_me_and_documents_for_that_are_follow_ups() -> None:
    for message in ("Does that apply to me?", "What documents are needed for that?"):
        state, r = run(*NRI_SAVINGS, say(message))
        assert r.step is StepKind.READY and "follow_up" in codes(r), message
        assert state.value(Slot.CUSTOMER_CATEGORY) == "NRI_INDIVIDUAL"


def test_the_understanding_cues() -> None:
    assert understand("How do I open an account for my six-year-old child?").person is Person.CHILD
    assert understand("I meant a personal account.").person is Person.INDIVIDUAL
    assert understand("What documents are needed for that?").follow_up
    assert understand("I also want to open an account for my child.").separate_request
    # A named category is never second-guessed as "a person".
    assert understand("an NRI individual").person is None


# --- 8. a separate request ---------------------------------------------------------------------


def test_a_separate_request_keeps_the_current_profile() -> None:
    state, r = run(*LLP_CURRENT, say("I also want to open an account for my child."))
    assert r.step is StepKind.READY and "separate_request" in codes(r)
    assert_llp(state) and state.entry(Slot.MINOR).status.value == "unknown"


# --- 9-12. through the service: checklist continuity, regeneration, reload ---------------------


async def service() -> tuple[AssistantService, str]:
    svc = AssistantService(MemoryStore(), lambda: ChecklistService(FakeChunks()), None)
    sid = (await svc.create_session())["session_id"]
    return svc, sid


_n = iter(range(10**6))


async def send(svc: AssistantService, sid: str, body: dict[str, Any]) -> dict[str, Any]:
    out = await svc.submit_turn(sid, f"k{next(_n)}", json.dumps(body).encode())
    return out["answer"]  # type: ignore[no-any-return]


def item_ids(answer: dict[str, Any]) -> list[str]:
    g = answer["checklist"]["groups"]
    return [i["id"] for group in ("documents", "information", "not_confirmed") for i in g[group]]


async def test_a_follow_up_preserves_the_checklist_and_answers_briefly() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        first = await send(svc, sid, body)
    follow = await send(svc, sid, say("What documents are needed for that?"))
    assert follow["kind"] == "checklist" and item_ids(follow) == item_ids(first)
    assert "the form asks you to bring:" in follow["reply"]
    assert "details to fill in" not in follow["reply"]  # not the whole checklist again


async def test_a_separate_request_keeps_the_checklist_and_does_not_repeat_it() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        first = await send(svc, sid, body)
    out = await send(svc, sid, say("I also want to open an account for my child."))
    assert out["kind"] == "checklist" and item_ids(out) == item_ids(first)
    assert out["reply"] == wording().context.separate
    assert any(m["code"] == "separate_request" for m in out["messages"])


async def test_a_confirmed_correction_regenerates_the_checklist_from_the_new_profile() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        llp = await send(svc, sid, body)
    assert {s["title"] for s in llp["checklist"]["sources"]} != set()
    ask = await send(svc, sid, say("Actually, I want the account for my child instead."))
    assert ask["kind"] == "question" and ask["checklist"] is None
    await send(svc, sid, choose("customer_category", "NRI_INDIVIDUAL"))
    await send(svc, sid, choose("nri_scheme", "NRE"))
    await send(svc, sid, choose("holding", "SINGLE"))
    nri = await send(svc, sid, choose("minor", "YES"))
    assert nri["kind"] == "checklist"
    assert {s["title"] for s in nri["checklist"]["sources"]}.isdisjoint(
        {s["title"] for s in llp["checklist"]["sources"]}
    )
    assert nri["profile"]["minor"]["value"] == "YES"


async def test_a_child_account_for_a_resident_gets_the_fallback_not_invented_items() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        await send(svc, sid, body)
    await send(svc, sid, say("What about my child?"))
    out = await send(svc, sid, choose("customer_category", "RESIDENT_INDIVIDUAL"))
    assert out["kind"] == "fallback" and out["checklist"] is None
    assert out["fallback"] == wording().fallback
    assert [s["value"] for s in out["suggestions"]] == ["NRI_INDIVIDUAL"]


async def test_a_reloaded_conversation_keeps_the_context() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        await send(svc, sid, body)
    await send(svc, sid, say("What about my child?"))
    view = await svc.get_session(sid)
    assert view["profile"]["customer_category"]["value"] == "LLP"  # unchanged by the question
    assert view["answer"]["question"]["slot"] == "customer_category"  # the clarification
    # The next message continues the clarification after the reload.
    out = await send(svc, sid, say("for my daughter"))
    assert any(m["code"] == "context_change" for m in out["messages"])


# --- 13. privacy -------------------------------------------------------------------------------


async def test_identifiers_in_a_follow_up_are_still_refused_and_never_stored() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        await send(svc, sid, body)
    out = await send(svc, sid, say(f"my son's PAN is {PAN}, what about him?"))
    assert out["kind"] == "blocked" and PAN not in json.dumps(out)
    view = await svc.get_session(sid)
    assert PAN not in json.dumps(view)
    assert view["profile"]["customer_category"]["value"] == "LLP"


def test_a_clarification_reply_asks_once() -> None:
    from app.assistant.answer import build_answer

    state, r = run(*LLP_CURRENT, say("What about my child?"))
    reply = build_answer(state, r, wording()).reply or ""
    assert "don't want to mix" in reply
    assert r.question.text not in reply  # the note itself asks; no second question
