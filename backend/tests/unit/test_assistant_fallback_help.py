"""A fallback that explains and suggests (owner decision after the A6 browser test).

When the documents don't cover a combination, the answer keeps the prescribed fallback text
verbatim and adds (a) the coverage map's plain-language explanation and (b) suggestions for
combinations the map DOES cover. Nothing here changes coverage: the suggestions are read from
the same cells that decide coverage.
"""

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.assistant.answer import build_answer
from app.assistant.coverage import COVERAGE_FILE, Coverage, coverage
from app.assistant.dialogue import StepKind, TurnInput, opening, respond_deterministic
from app.assistant.profile import Slot, valid_value
from app.assistant.taxonomy import AccountType as A
from app.assistant.taxonomy import CustomerCategory as C
from app.assistant.wording import wording


def choose(slot: str, value: str) -> dict[str, Any]:
    return {"action": "choose", "slot": slot, "value": value}


def run(*bodies: dict[str, Any]) -> Any:
    state, response = opening()
    for body in bodies:
        state, response = respond_deterministic(state, TurnInput.model_validate(body))
    return state, response


def test_every_explanation_is_plain_customer_wording() -> None:
    cov = coverage()
    for r in cov.not_established_reasons:
        assert r.explanation and r.explanation != r.reason
        assert "corpus" not in r.explanation.lower() and "A0" not in r.explanation
    assert cov.default_explanation.startswith("The HDFC Bank documents")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["not_established_reasons"][0].update(explanation="Not in the corpus."),
        lambda d: d.update(default_explanation="See A0 amendment 2."),
        lambda d: d["not_established_reasons"][1].pop("explanation"),
    ],
)
def test_internal_wording_or_a_missing_explanation_fails_to_load(mutate: Any) -> None:
    data = yaml.safe_load(COVERAGE_FILE.read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(ValidationError):
        Coverage.model_validate(data)


def test_llp_savings_explains_and_suggests_the_covered_account_types() -> None:
    _, r = run(choose("account_type", "SAVINGS"), choose("customer_category", "LLP"))
    assert r.step is StepKind.NOT_ESTABLISHED
    assert r.fallback == wording().fallback  # still the prescribed text, verbatim
    assert r.explanation == coverage().not_established_explanation(C.LLP, A.SAVINGS)
    assert [(s.slot, s.value) for s in r.suggestions] == [
        (Slot.ACCOUNT_TYPE, a.value) for a in coverage().covered_accounts(C.LLP)
    ]
    assert [s.value for s in r.suggestions] == ["CURRENT", "FIXED_DEPOSIT", "RECURRING_DEPOSIT"]


def test_resident_savings_suggests_the_category_the_documents_cover() -> None:
    _, r = run(
        choose("account_type", "SAVINGS"), choose("customer_category", "RESIDENT_INDIVIDUAL")
    )
    assert "converting a resident account to an NRO account" in (r.explanation or "")
    assert [(s.slot, s.value) for s in r.suggestions] == [
        (Slot.CUSTOMER_CATEGORY, "NRI_INDIVIDUAL")
    ]


def test_salary_suggests_every_account_type_the_documents_cover() -> None:
    _, r = run(choose("account_type", "SALARY"))
    assert r.step is StepKind.NOT_ESTABLISHED
    assert "salary account" in (r.explanation or "")
    covered = [a.value for a in A if coverage().covered_categories(a)]
    assert [s.value for s in r.suggestions] == covered and "SALARY" not in covered


def test_suggestions_are_valid_choices_with_the_question_labels() -> None:
    cov, words = coverage(), wording()
    for cat in C:
        for acct in A:
            if cov.cell(cat, acct).covered:
                continue
            _, r = run(choose("account_type", acct.value), choose("customer_category", cat.value))
            if r.step is not StepKind.NOT_ESTABLISHED:
                continue
            for s in r.suggestions:
                assert valid_value(s.slot, s.value)
                assert s.label == words.label(s.slot, s.value)
                # Taking a suggestion leads to a covered combination.
                pair = (C(s.value), acct) if s.slot is Slot.CUSTOMER_CATEGORY else (cat, A(s.value))
                assert cov.cell(*pair).covered


def test_taking_a_suggestion_reaches_a_covered_combination() -> None:
    state, r = run(choose("account_type", "SAVINGS"), choose("customer_category", "LLP"))
    s = r.suggestions[0]
    _, r2 = respond_deterministic(
        state,
        TurnInput.model_validate({"action": "correct", "slot": s.slot.value, "value": s.value}),
    )
    assert r2.step is StepKind.READY


def test_the_api_answer_carries_explanation_and_suggestions_but_never_the_reason() -> None:
    state, r = run(choose("account_type", "SAVINGS"), choose("customer_category", "LLP"))
    answer = build_answer(state, r, wording()).model_dump(mode="json")
    assert answer["kind"] == "fallback" and answer["coverage_state"] == "NOT_ESTABLISHED"
    assert answer["explanation"] == r.explanation
    assert answer["suggestions"][0] == {
        "slot": "account_type",
        "value": "CURRENT",
        "label": "Current account",
    }
    assert "reason" not in answer
    assert "corpus" not in str(answer).lower()


def test_other_answers_have_no_suggestions() -> None:
    _, r = run(choose("account_type", "CURRENT"), choose("customer_category", "LLP"))
    assert r.step is StepKind.READY and r.explanation is None and r.suggestions == ()
