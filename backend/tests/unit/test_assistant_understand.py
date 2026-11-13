"""A3: deterministic understanding. Explicit words fill slots; anything weaker is a hint,
an ambiguity or nothing at all (A1 R7: never guess)."""

import pytest

from app.assistant.profile import Slot
from app.assistant.taxonomy import AccountType, CustomerCategory, Intent
from app.assistant.understand import (
    Control,
    QuestionKind,
    Residency,
    Topic,
    Unsupported,
    normalise,
    understand,
)

A, C = AccountType, CustomerCategory
ACC, CAT = Slot.ACCOUNT_TYPE, Slot.CUSTOMER_CATEGORY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I want to open a savings account", A.SAVINGS),
        ("Open an NRE savings a/c please", A.SAVINGS),
        ("savings", A.SAVINGS),  # a short reply: the bare option
        ("We need a current account for the firm", A.CURRENT),
        ("current", A.CURRENT),
        ("What do I need for a salary account?", A.SALARY),
        ("I'd like a fixed deposit", A.FIXED_DEPOSIT),
        ("an NRE FD", A.FIXED_DEPOSIT),
        ("term deposit", A.FIXED_DEPOSIT),
        ("Can I start a recurring deposit?", A.RECURRING_DEPOSIT),
        ("FCNR deposit", A.FCNR_RFC),
        ("an FCNR fixed deposit", A.FCNR_RFC),  # FCNR is its own option, not also an FD
        ("RFC account", A.FCNR_RFC),
    ],
)
def test_account_types(text: str, expected: AccountType) -> None:
    u = understand(text)
    assert u.values.get(ACC) == expected and ACC not in u.ambiguous


@pytest.mark.parametrize(
    "text",
    [
        "I want to open an account",  # no type stated
        # a long message: a bare "current" is not the option
        "Please update my current address details for the application form",
        "I want to put my savings somewhere safe for the next few years",
        "I want to open an NRE account",  # NRE is a scheme, not an account type
    ],
)
def test_no_account_type_is_guessed(text: str) -> None:
    assert ACC not in understand(text).values


@pytest.mark.parametrize(
    "text",
    [
        "savings account or current account?",
        "savings or current account, which is better?",
        "should I choose a savings or a current account for this",
    ],
)
def test_two_account_types_are_ambiguous_not_chosen(text: str) -> None:
    u = understand(text)
    assert ACC not in u.values and u.ambiguous[ACC] == (A.CURRENT, A.SAVINGS)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I am an NRI", C.NRI_INDIVIDUAL),
        ("I'm a non-resident Indian", C.NRI_INDIVIDUAL),
        ("I hold an OCI card", C.NRI_INDIVIDUAL),
        ("person of Indian origin", C.NRI_INDIVIDUAL),
        ("I'm a resident Indian", C.RESIDENT_INDIVIDUAL),
        ("for my sole proprietorship", C.SOLE_PROPRIETORSHIP),
        ("we are a partnership firm", C.PARTNERSHIP_FIRM),
        ("my LLP", C.LLP),
        ("a limited liability partnership", C.LLP),  # not also a partnership firm
        ("our private limited company", C.COMPANY),
        ("XYZ Pvt Ltd", C.COMPANY),
        ("a charitable trust", C.TRUST),
        ("trust", C.TRUST),
        ("for our HUF", C.HUF),
        ("Hindu Undivided Family", C.HUF),
        ("a housing society", C.OTHER_ORGANISATION),
        ("our cricket club", C.OTHER_ORGANISATION),
        ("an NGO", C.OTHER_ORGANISATION),
    ],
)
def test_customer_categories(text: str, expected: CustomerCategory) -> None:
    u = understand(text)
    assert u.values.get(CAT) == expected and CAT not in u.ambiguous


@pytest.mark.parametrize(
    "text",
    [
        "I trust that you can help me",  # the verb, not a trust
        "for my business",  # an organisation, kind unknown: never a proprietorship
        "I live in Dubai",  # residency is a hint, not a category
        "I live in India",
        "I am not an NRI",  # negated
        "an individual",
        "my son",
    ],
)
def test_no_category_is_guessed(text: str) -> None:
    assert CAT not in understand(text).values


def test_non_resident_indian_is_never_read_as_resident() -> None:
    u = understand("non resident indian")
    assert u.values[CAT] == C.NRI_INDIVIDUAL


def test_negation_picks_the_remaining_option() -> None:
    u = understand("not a company but an LLP")
    assert u.values[CAT] == C.LLP and u.correction_cue


def test_two_categories_are_ambiguous() -> None:
    u = understand("is it the same for a company and an LLP?")
    assert CAT not in u.values and set(u.ambiguous[CAT]) == {C.COMPANY, C.LLP}


def test_business_without_kind_is_flagged_not_mapped() -> None:
    u = understand("I want a current account for my business")
    assert u.organisation_unspecified and CAT not in u.values and u.hints == ()


def test_a_named_kind_overrides_the_business_flag() -> None:
    u = understand("current account for my business, it's an LLP")
    assert u.values[CAT] == C.LLP and not u.organisation_unspecified


@pytest.mark.parametrize(
    ("text", "residency", "hint"),
    [
        ("I live in Dubai", Residency.OUTSIDE_INDIA, C.NRI_INDIVIDUAL),
        ("I'm based abroad", Residency.OUTSIDE_INDIA, C.NRI_INDIVIDUAL),
        ("working in the UK", Residency.OUTSIDE_INDIA, C.NRI_INDIVIDUAL),
        ("I live in India", Residency.IN_INDIA, C.RESIDENT_INDIVIDUAL),
    ],
)
def test_residency_gives_a_hint_only(
    text: str, residency: Residency, hint: CustomerCategory
) -> None:
    u = understand(text)
    assert u.residency is residency and CAT not in u.values
    assert [(h.slot, h.value) for h in u.hints] == [(CAT, hint)]


def test_unknown_places_are_not_interpreted() -> None:
    u = understand("I live in Mumbai")  # no gazetteer of Indian cities: nothing inferred
    assert u.residency is None and u.hints == ()


def test_living_in_india_with_a_business_gives_no_individual_hint() -> None:
    assert understand("I live in India and need an account for my business").hints == ()


def test_nre_without_category_hints_nri_but_does_not_fill_it() -> None:
    u = understand("I want an NRE account")
    assert u.values == {Slot.NRI_SCHEME: "NRE"}
    assert [(h.slot, h.value) for h in u.hints] == [(CAT, C.NRI_INDIVIDUAL)]


@pytest.mark.parametrize(
    ("text", "slot", "value"),
    [
        ("NRO account", Slot.NRI_SCHEME, "NRO"),
        ("a joint account", Slot.HOLDING, "JOINT"),
        ("with my wife", Slot.HOLDING, "JOINT"),
        ("only me", Slot.HOLDING, "SINGLE"),
        ("single", Slot.HOLDING, "SINGLE"),
        ("for my daughter", Slot.MINOR, "YES"),
        ("the account is for a minor", Slot.MINOR, "YES"),
    ],
)
def test_facets(text: str, slot: Slot, value: str) -> None:
    assert understand(text).values.get(slot) == value


def test_nre_and_nro_together_are_ambiguous_and_a_term_question() -> None:
    u = understand("What is the difference between NRE and NRO?")
    assert Slot.NRI_SCHEME in u.ambiguous and u.question_kind is QuestionKind.TERM


@pytest.mark.parametrize(
    ("text", "topics", "kind"),
    [
        ("Do I need to submit a photograph?", {Topic.PHOTOGRAPH}, QuestionKind.ITEM),
        ("Does an LLP need a PAN card?", {Topic.PAN_CARD_COPY}, QuestionKind.ITEM),
        ("photocopy of my PAN", {Topic.PAN_CARD_COPY}, QuestionKind.ITEM),
        ("Is PAN required?", {Topic.PAN}, QuestionKind.ITEM),
        ("I don't have a PAN", {Topic.PAN_NOT_AVAILABLE}, QuestionKind.ITEM),
        ("Can I give Form 60?", {Topic.FORM_60}, QuestionKind.ITEM),
        ("Do I need my passport?", {Topic.PASSPORT}, QuestionKind.ITEM),
        ("Is Aadhaar mandatory?", {Topic.AADHAAR}, QuestionKind.ITEM),
        ("what address proof works", {Topic.ADDRESS_PROOF}, QuestionKind.ITEM),
        ("details of authorised signatories", {Topic.SIGNATORIES}, QuestionKind.ITEM),
        ("What documents do I need?", {Topic.DOCUMENTS}, QuestionKind.CHECKLIST),
        ("How do I open an account?", {Topic.DOCUMENTS}, QuestionKind.CHECKLIST),
        ("hello", set(), QuestionKind.NONE),
    ],
)
def test_question_topics(text: str, topics: set[Topic], kind: QuestionKind) -> None:
    u = understand(text)
    assert set(u.topics) == topics and u.question_kind is kind


def test_passport_size_photo_is_a_photograph_not_a_passport() -> None:
    assert understand("a passport size photo").topics == (Topic.PHOTOGRAPH,)


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("What is the interest rate on an FD?", "rates_and_charges"),
        ("Can I get a home loan?", "loans"),
        ("I need a credit card", "cards"),
        ("Am I eligible for an NRI account?", "eligibility_or_approval"),
        ("Will my application be approved?", "eligibility_or_approval"),
    ],
)
def test_out_of_scope(text: str, subject: str) -> None:
    assert subject in understand(text).out_of_scope


def test_conversion_intent() -> None:
    u = understand("I have become an NRI. How do I convert my resident account to an NRO account?")
    assert u.intent is Intent.RESIDENT_TO_NRO_CONVERSION


def test_unsupported_customer_type_is_not_mapped() -> None:
    u = understand("I'm a foreign national and want a savings account")
    assert u.unsupported is Unsupported.FOREIGN_NATIONAL and CAT not in u.values and u.hints == ()


@pytest.mark.parametrize(
    ("text", "control"),
    [
        ("I'm not sure", Control.NOT_SURE),
        ("don't know", Control.NOT_SURE),
        ("no idea", Control.NOT_SURE),
        ("skip", Control.SKIP),
        ("what does this mean?", Control.EXPLAIN),
        ("I don't understand", Control.EXPLAIN),
        ("yes", Control.YES),
        ("Yep", Control.YES),
        ("no", Control.NO),
    ],
)
def test_controls(text: str, control: Control) -> None:
    assert understand(text).control is control


def test_not_sure_inside_a_real_question_is_not_a_control() -> None:
    u = understand("I'm not sure which documents I need for an NRE savings account")
    assert u.control is None and u.values[ACC] == A.SAVINGS


@pytest.mark.parametrize(
    "text", ["Actually it's for my company", "Sorry, I meant current", "No, a current account"]
)
def test_correction_cues(text: str) -> None:
    assert understand(text).correction_cue


def test_normalise() -> None:
    assert normalise("  Non-Resident  Indian’s A/C!! ") == "non resident indian's a/c"
    assert understand("   ").substantive is False
