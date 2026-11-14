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
    # The customer is asked whether it is a separate account or a change; nothing changes.
    state, r = run(*LLP_CURRENT, say("it is being opened for a minor"))
    assert state.entry(Slot.MINOR).status.value == "unknown"
    assert_llp(state)
    assert r.step is StepKind.CHOOSE_SCENARIO and r.ready is None
    assert [o.action.value for o in r.scenario_offer.options] == [
        "new_scenario", "change_application",
    ]  # fmt: skip
    assert "current account for a limited liability partnership (LLP)" in r.scenario_offer.text


def test_the_daughter_flow_keeps_the_llp_and_reads_india_for_the_daughter() -> None:
    # The owner's flow: Current -> LLP -> a child's account -> separate or change? -> "my
    # daughter's account" -> where does she live? -> "No, we are in India."
    state, r = run(*LLP_CURRENT, say("How can I open an account for my daughter who is six?"))
    assert r.step is StepKind.CHOOSE_SCENARIO
    assert_llp(state)
    state, r = respond_deterministic(
        state, TurnInput(message="I wanna ask about my daughter account.")
    )
    assert "scenario_started" in codes(r)
    assert r.step is StepKind.ASK and r.question.slot is Slot.CUSTOMER_CATEGORY
    assert wording().context.which_person in [n.text for n in r.notes]
    assert [c.value for c in r.question.chips][:2] == ["RESIDENT_INDIVIDUAL", "NRI_INDIVIDUAL"]
    assert state.value(Slot.CUSTOMER_CATEGORY) is None  # the daughter's account, fresh
    assert len(state.scenarios) == 1  # the LLP current account, parked unchanged
    assert state.scenarios[0].slots[Slot.CUSTOMER_CATEGORY].value == "LLP"
    state, r = respond_deterministic(state, TurnInput(message="No, we are in India."))
    assert state.value(Slot.CUSTOMER_CATEGORY) == "RESIDENT_INDIVIDUAL"
    assert state.value(Slot.ACCOUNT_TYPE) is None  # never carried over from the LLP
    assert r.ready is None and r.step is not StepKind.CONFIRM
    assert state.scenarios[0].slots[Slot.ACCOUNT_TYPE].value == "CURRENT"


def test_the_customer_can_go_back_to_the_earlier_account() -> None:
    state, r = run(
        *LLP_CURRENT,
        say("I also want to open an account for my child."),
        say("we live in Dubai"),
    )
    assert state.value(Slot.CUSTOMER_CATEGORY) == "NRI_INDIVIDUAL"
    state, r = respond_deterministic(
        state, TurnInput(message="what about the LLP current account?")
    )
    assert r.step is StepKind.CHOOSE_SCENARIO
    assert r.scenario_offer.options[0].action.value == "switch_scenario"
    assert state.value(Slot.CUSTOMER_CATEGORY) == "NRI_INDIVIDUAL"  # nothing changed yet
    state, r = respond_deterministic(state, TurnInput(message="yes"))
    assert "scenario_switched" in codes(r) and r.step is StepKind.READY
    assert_llp(state)
    assert state.scenarios[0].slots[Slot.CUSTOMER_CATEGORY].value == "NRI_INDIVIDUAL"
    # The button does the same: back to the child's account.
    state, r = respond_deterministic(state, TurnInput(action="switch_scenario", value="0"))
    assert state.value(Slot.CUSTOMER_CATEGORY) == "NRI_INDIVIDUAL"


def test_change_this_application_asks_who_and_lists_individuals_first() -> None:
    state, r = run(*LLP_CURRENT, say("What about my child?"), {"action": "change_application"})
    assert r.step is StepKind.ASK and r.question.slot is Slot.CUSTOMER_CATEGORY
    chips = [c.value for c in r.question.chips]
    assert chips[:3] == ["RESIDENT_INDIVIDUAL", "NRI_INDIVIDUAL", "LLP"]
    assert len(chips) == len(set(chips)) == 9  # every option stays available
    assert_llp(state)
    assert not state.scenarios


def test_greetings_and_thanks_are_answered_and_change_nothing() -> None:
    w = wording()
    _, r = run(say("Yoooo"))
    assert "social" in codes(r) and r.question.slot is Slot.ACCOUNT_TYPE
    assert w.social.greeting in [n.text for n in r.notes]
    state, r = run(*LLP_CURRENT, say("Hello"))
    assert r.step is StepKind.READY and r.unchanged and "social" in codes(r)
    for message in ("Thanks!", "bye", "ok"):
        after, r = respond_deterministic(state, TurnInput(message=message))
        assert "social" in codes(r) and r.step is StepKind.READY, message
        assert after.slots == state.slots


def test_a_greeting_keeps_a_pending_confirmation() -> None:
    state, r = run(say("I live in Dubai"))
    assert r.step is StepKind.CONFIRM
    state, r = respond_deterministic(state, TurnInput(message="hi"))
    assert r.step is StepKind.CONFIRM and "social" in codes(r)


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


def test_a_separate_request_starts_a_new_account_and_keeps_the_current_one() -> None:
    state, r = run(*LLP_CURRENT, say("I also want to open an account for my child."))
    assert "scenario_started" in codes(r) and r.ready is None
    assert r.question.slot is Slot.CUSTOMER_CATEGORY
    assert state.value(Slot.CUSTOMER_CATEGORY) is None
    assert state.scenarios[0].slots[Slot.CUSTOMER_CATEGORY].value == "LLP"


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


async def test_a_separate_request_parks_the_checklist_and_lists_it() -> None:
    svc, sid = await service()
    for body in LLP_CURRENT:
        await send(svc, sid, body)
    out = await send(svc, sid, say("I also want to open an account for my child."))
    assert out["kind"] == "question" and out["checklist"] is None
    assert [s["label"] for s in out["scenarios"]] == [
        "a current account for a limited liability partnership (LLP)"
    ]
    assert out["reply"].startswith("OK, let's look at that account separately.")
    back = await send(svc, sid, {"action": "switch_scenario", "value": "0"})
    assert back["kind"] == "checklist"
    assert back["profile"]["customer_category"]["value"] == "LLP"


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
    assert view["answer"]["scenario_offer"] is not None  # the open offer survives a reload
    # The next message answers the offer after the reload: a separate account for her.
    out = await send(svc, sid, say("for my daughter"))
    assert any(m["code"] == "scenario_started" for m in out["messages"])
    out = await send(svc, sid, say("we are in India"))
    assert out["profile"]["customer_category"]["value"] == "RESIDENT_INDIVIDUAL"
    assert [s["label"] for s in out["scenarios"]] == [
        "a current account for a limited liability partnership (LLP)"
    ]


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


def test_an_offer_reply_asks_once() -> None:
    from app.assistant.answer import build_answer

    state, r = run(*LLP_CURRENT, say("What about my child?"))
    reply = build_answer(state, r, wording()).reply or ""
    assert reply.count(r.scenario_offer.text) == 1


def test_residency_outside_the_which_person_question_is_still_confirmed() -> None:
    # R7: "we live in Dubai" on its own never becomes a category; it is a hint to confirm.
    state, r = run(say("an account for my child, we live in Dubai"))
    assert r.step is StepKind.CONFIRM
    assert (r.confirmation.slot, r.confirmation.value) == (Slot.CUSTOMER_CATEGORY, "NRI_INDIVIDUAL")
    assert state.value(Slot.CUSTOMER_CATEGORY) is None


def test_a_separate_request_does_not_accept_a_pending_offer() -> None:
    """A7 finding: with an offer open for a named account ("a current account for my
    company"), "I also want one for my child" used to start the offered account too. The
    offered values are never applied unless chosen; only one new account starts."""
    state, r = run(
        say("I live in Dubai and want an NRE savings account"),
        {"action": "confirm_yes"},
        choose("holding", "SINGLE"),
        choose("minor", "NO"),
        say("and a current account for my company"),
    )
    assert r.step is StepKind.CHOOSE_SCENARIO
    state, r = respond_deterministic(
        state, TurnInput(message="I also want to open an account for my child.")
    )
    assert codes(r).count("scenario_started") == 1
    parked = [s.slots[Slot.CUSTOMER_CATEGORY].value for s in state.scenarios]
    assert parked == ["NRI_INDIVIDUAL"]  # the NRI account, unchanged; no company account
    assert state.value(Slot.CUSTOMER_CATEGORY) is None and not state.offer_values
