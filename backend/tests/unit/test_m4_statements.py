"""M4 S2: deterministic modality, polarity/negation, subject, scope and anchor analysis.

Every obligation shape is tested in positive, negative and ambiguous forms. The analysis
is a NECESSARY condition only: no cue on its own establishes anything (see test_m4_decide).
"""

import pytest

from app.schemas.taxonomy import EntityType
from app.verification.statements import analyse, anchors, anchors_missing, subject_conflict

DEED = anchors("Trust deed")
E = EntityType


def read(text: str, spec=DEED, entity: EntityType | None = E.TRUST, section: str | None = None):  # type: ignore[no-untyped-def]
    return analyse(text, spec, entity=entity, section=section)


# (window text, modality, polarity)
OBLIGATIONS = [
    # positive
    ("For a trust, the trust deed shall be obtained.", "obligation", "affirmative"),
    ("The trust deed must be submitted by the trust.", "obligation", "affirmative"),
    ("The trust deed is required for a trust.", "obligation", "affirmative"),
    ("A trust is required to submit the trust deed.", "obligation", "affirmative"),
    ("Submission of the trust deed is mandatory for trusts.", "obligation", "affirmative"),
    ("Paragraph 35 requires the trust deed of the trust.", "obligation", "affirmative"),
    ("The trust has to provide the trust deed.", "obligation", "affirmative"),
    # negative
    ("The trust deed shall not be obtained for a trust.", "obligation", "negated"),
    ("The trust need not submit the trust deed.", "obligation", "negated"),
    ("The trust deed is not required for a trust.", "obligation", "negated"),
    ("The trust deed is not mandatory for trusts.", "obligation", "negated"),
    ("A trust is exempt from submitting the trust deed.", "obligation", "negated"),
    # a permitted exemption: never an affirmative obligation
    ("The trust deed may be dispensed with for a trust.", "permission", "negated"),
    ("The RE does not require the trust deed of a trust.", "obligation", "negated"),
    ("There is no need for the trust deed of a trust.", "obligation", "negated"),
    # ambiguous
    ("The trust deed shall be obtained except for small trusts.", "obligation", "ambiguous"),
    ("The trust deed, other than a copy, shall be obtained.", "obligation", "ambiguous"),
    ("A trust deed not older than a year shall be obtained.", "obligation", "ambiguous"),
    ("It is not that the trust deed shall not be obtained.", "obligation", "ambiguous"),
    ("The trust deed need not be obtained unless the trust is new.", "obligation", "ambiguous"),
    ("The trust deed shall be obtained without delay from the trust.", "obligation", "ambiguous"),
    # permission and conditions
    ("The trust deed may be obtained from a trust.", "permission", "affirmative"),
    ("The trust deed is optional for a trust.", "permission", "affirmative"),
    ("Where applicable, the trust deed shall be obtained.", "conditional", "affirmative"),
    ("If the trust is registered, the trust deed must be obtained.", "conditional", "affirmative"),
    ("Subject to para 9, the trust deed shall be obtained.", "conditional", "affirmative"),
    # no modality at all (a list, a table row, a form label)
    ("Trust: trust deed, PAN, registration certificate.", "none", "affirmative"),
]


@pytest.mark.parametrize(("text", "modality", "polarity"), OBLIGATIONS)
def test_modality_and_polarity(text: str, modality: str, polarity: str) -> None:
    a = read(text)
    assert (a.modality, a.polarity) == (modality, polarity), a.cues


@pytest.mark.parametrize(
    "text",
    [
        "Form No. 60 shall be obtained from the trust.",
        "Form No 60 shall be obtained from the trust.",
        "Form Nos. 60 and 61 shall be obtained from the trust.",
    ],
)
def test_a_document_number_is_never_a_negation(text: str) -> None:
    a = read(text, anchors("Form 60"))
    assert a.polarity == "affirmative" and a.object_match


def test_cannot_is_a_negation_and_never_a_permission() -> None:
    a = read("A trust cannot open an account without the trust deed.")
    assert a.modality == "none" and a.polarity == "ambiguous"


def test_an_obligation_keyword_without_the_item_does_not_match_the_object() -> None:
    a = read("The PAN of the trust shall be obtained.")
    assert a.modality == "obligation" and a.polarity == "affirmative"
    assert not a.object_match


# --- anchors ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "text", "missing"),
    [
        ("PAN Number", "the PAN of the entity", []),
        ("PAN Number", "the permanent account number", ["pan"]),  # no invented synonyms
        ("Form 60/61", "Form No. 61 where PAN is absent", []),
        ("Form 60/61", "Form 16 is not relevant", ["60/61"]),
        ("Form 60/61 (existing customers)", "Form 62 for customers", ["60/61"]),
        ("Trust deed", "trust deeds of the trust", []),
        ("Trust deed", "the deed", []),  # the entity word says whose deed, not which item
        ("Trust deed", "the PAN of the trust", ["deed"]),
        ("Trust", "the trust", []),  # an entity word alone still names the item
        (
            "Registration certificate of the firm",
            "the firm's deed",
            ["certificate", "registration"],
        ),
        ("OVD", "an officially valid document", ["ovd"]),
    ],
)
def test_anchor_matching(item: str, text: str, missing: list[str]) -> None:
    assert anchors_missing(anchors(item), text) == missing


def test_an_item_without_anchor_terms_never_matches() -> None:
    a = read("The documents shall be obtained.", anchors("the documents"))
    assert not a.object_match


# --- subject and scope ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("The RE shall obtain the trust deed.", "regulated_entity"),
        ("Designated partners must be verified.", "designated_partner"),
        ("The partners must sign the trust deed.", "partner"),
        ("The trust deed shall be obtained.", None),  # never inferred
        ("The bank shall verify; the trustees shall sign the trust deed.", "regulated_entity"),
    ],
)
def test_the_subject_is_recorded_only_when_named(text: str, subject: str | None) -> None:
    assert read(text).subject == subject


def test_a_different_person_role_is_a_subject_conflict() -> None:
    window = read("Designated partners must be verified.", anchors("identity documents"))
    assert subject_conflict("Every employee of the LLP must submit identity documents.", window)
    assert not subject_conflict("Designated partners must be verified.", window)
    assert not subject_conflict("Identity must be verified.", window)  # no role claimed


@pytest.mark.parametrize(
    ("text", "entity", "section", "scope"),
    [
        ("For a trust, the trust deed shall be obtained.", E.TRUST, None, "match"),
        ("For a partnership firm, the deed shall be obtained.", E.TRUST, None, "mismatch"),
        ("For a limited liability partnership, the deed shall be obtained.", E.LLP, None, "match"),
        ("For a limited liability partnership, the deed shall be obtained.", E.PARTNERSHIP,
         None, "mismatch"),
        ("For a partnership firm, the deed shall be obtained.", E.LLP, None, "mismatch"),
        ("For companies and trusts, the deed shall be obtained.", E.TRUST, None, "match"),
        ("The deed shall be obtained.", E.TRUST, "Accounts of trusts", "match"),
        ("The deed shall be obtained.", E.TRUST, "Accounts of companies", "mismatch"),
        ("The deed shall be obtained.", E.TRUST, "C. CDD for legal entities", "unknown"),
        ("For a trust, the deed shall be obtained.", None, None, "unknown"),
    ],  # fmt: skip
)
def test_entity_scope(
    text: str, entity: EntityType | None, section: str | None, scope: str
) -> None:
    assert read(text, entity=entity, section=section).scope == scope


def test_the_analysis_is_deterministic_and_bounded() -> None:
    text = " ".join(t for t, _, _ in OBLIGATIONS)
    first, second = read(text), read(text)
    assert first == second and len(first.cues) <= 20
