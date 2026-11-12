"""M4 S2: premises come from stored text with exact provenance; claims are typed and
derived from what the requirement asserts (never from the evidence)."""

import random

import pytest

from app.db.repository import EvidenceContext
from app.graph.state import AtomicClaim, ClaimType, EvidenceRole
from app.schemas.taxonomy import EntityType
from app.verification.claims import MAX_EXPLANATORY, TEMPLATES, TEMPLATES_VERSION, extract
from app.verification.premises import (
    MAX_WINDOW_CHARS,
    MAX_WINDOWS,
    PremiseMismatch,
    build_premise,
    windows,
)
from tests.unit.m4_fixtures import citation, dated, form, item, ocr_primary, premise, primary

LIST = (
    "35. For opening an account of a trust, certified copies of each of the following "
    "documents shall be obtained: (a) registration certificate; (b) trust deed; and (c) "
    "PAN or Form No. 60 of the trust. The RE may also obtain a list of trustees."
)

# --- premises -------------------------------------------------------------------------------


def test_every_window_is_an_exact_span_of_the_stored_text() -> None:
    p = premise(primary(LIST))
    assert p.windows and not p.windows_capped
    for w in p.windows:
        assert LIST[w.start : w.end] == w.text
        assert len(w.text) <= MAX_WINDOW_CHARS
    assert len({(w.start, w.end) for w in p.windows}) == len(p.windows)


def test_a_list_item_is_read_together_with_its_lead_in() -> None:
    texts = [w.text for w in premise(primary(LIST)).windows]
    lead_and_item = [t for t in texts if t.startswith("35. For opening") and "(b) trust deed" in t]
    assert lead_and_item, "the lead-in obligation must reach the item it governs"
    assert any(t == "(b) trust deed;" for t in texts)
    # the list's sentence ended: the next sentence is not joined to the lead-in
    assert not any(t.startswith("35.") and "list of trustees" in t for t in texts)


def test_provenance_is_copied_from_the_stored_chunk() -> None:
    r = primary(LIST)
    p = premise(r)
    m = r.metadata
    assert (p.evidence_id, p.document_key, p.page) == (m.chunk_id, m.document_key, m.page_number)
    assert p.evidence_role == EvidenceRole.PRIMARY_POLICY and p.can_support_mandatory
    assert p.window_chunk_id is None
    assert premise(form("Trust deed copy attached.")).evidence_role == EvidenceRole.FORM
    assert premise(dated("Q6. A trust deed is needed.")).evidence_role == (
        EvidenceRole.SUPPORTING_REFERENCE
    )
    ocr = premise(ocr_primary("The trust deed shall be obtained."))
    assert ocr.evidence_role == EvidenceRole.PRIMARY_POLICY and not ocr.can_support_mandatory


def test_a_child_chunk_is_read_in_its_parent_paragraph() -> None:
    child, parent = primary("(b) trust deed;"), primary(LIST)
    p = build_premise(citation(child), EvidenceContext(retrieved=child, context=parent))
    assert p.evidence_id == child.metadata.chunk_id
    assert p.window_chunk_id == parent.metadata.chunk_id  # offsets index the parent text
    assert all(LIST[w.start : w.end] == w.text for w in p.windows)


@pytest.mark.parametrize(
    "field",
    ["document_key", "evidence_eligibility", "text_reliability", "source_status",
     "can_support_mandatory"],
)  # fmt: skip
def test_a_citation_that_disagrees_with_the_stored_chunk_is_refused(field: str) -> None:
    r = primary(LIST)
    good = citation(r)
    wrong = {
        "document_key": "other_doc",
        "evidence_eligibility": "supporting",
        "text_reliability": "ocr_uncertain",
        "source_status": "dated",
        "can_support_mandatory": False,
    }[field]
    bad = good.model_copy(update={field: wrong})
    with pytest.raises(PremiseMismatch, match=field):
        build_premise(bad, EvidenceContext(retrieved=r, context=r))


def test_a_citation_of_another_chunk_is_refused() -> None:
    r, other = primary(LIST), primary("Other text.")
    with pytest.raises(PremiseMismatch, match="evidence_id"):
        build_premise(citation(other), EvidenceContext(retrieved=r, context=r))


def test_window_count_and_length_are_bounded() -> None:
    many = " ".join(f"Rule {i} shall apply." for i in range(200))
    wins, capped = windows(many)
    assert len(wins) == MAX_WINDOWS and capped
    long = "word " * 600 + "end."
    wins, _ = windows(long)
    assert wins and all(len(w.text) <= MAX_WINDOW_CHARS for w in wins)
    assert all(long[w.start : w.end] == w.text for w in wins)


def test_windows_are_exact_for_random_texts() -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test inputs, not security
    parts = LIST.split(" ") + ["shall", "not", "(d)", ";", ":", "Q7.", "No.", "60."]
    for _ in range(300):
        text = " ".join(rng.choice(parts) for _ in range(rng.randint(1, 60)))
        wins, _ = windows(text)
        assert all(text[w.start : w.end] == w.text and w.text for w in wins)


# --- claims ---------------------------------------------------------------------------------


def claims_for(applicability: str, roles: set[str], explanation: str = "") -> list[AtomicClaim]:
    req = item("Trust deed", [primary(LIST)], applicability=applicability, explanation=explanation)
    return list(extract(req, entity=EntityType.TRUST, roles=roles).claims)


PRIMARY_ROLE, FORM_ROLE, SUPP_ROLE = "primary_policy", "form", "supporting_reference"


@pytest.mark.parametrize(
    ("applicability", "roles", "expected"),
    [
        ("MANDATORY", {PRIMARY_ROLE}, [("policy_obligation", "unconditional")]),
        ("CONDITIONAL", {PRIMARY_ROLE}, [("policy_obligation", "conditional")]),
        # the asserted obligation is kept even with no primary source: it must then fail
        ("MANDATORY", {FORM_ROLE}, [("policy_obligation", "unconditional"),
                                    ("form_request", "none")]),
        ("MANDATORY", {SUPP_ROLE}, [("policy_obligation", "unconditional"),
                                    ("supporting_statement", "none")]),
        ("OPTIONAL", {SUPP_ROLE, FORM_ROLE}, [("form_request", "none"),
                                              ("supporting_statement", "none")]),
        ("OPTIONAL", {PRIMARY_ROLE}, []),
        ("UNCLEAR", {PRIMARY_ROLE, FORM_ROLE, SUPP_ROLE}, [("form_request", "none")]),
    ],  # fmt: skip
)
def test_typed_claims_follow_what_the_item_asserts(
    applicability: str, roles: set[str], expected: list[tuple[str, str]]
) -> None:
    got = [(c.claim_type.value, c.asserted_strength) for c in claims_for(applicability, roles)]
    assert got == expected


def test_templates_assert_one_thing_each_and_are_versioned() -> None:
    claims = claims_for("MANDATORY", {PRIMARY_ROLE, FORM_ROLE, SUPP_ROLE})
    text = {c.claim_type: c.text for c in claims}
    assert text[ClaimType.POLICY_OBLIGATION] == "Trust deed must be obtained for a trust customer."
    assert text[ClaimType.FORM_REQUEST] == "The form asks for Trust deed."
    assert all(c.template_version == TEMPLATES_VERSION for c in claims)
    for template in TEMPLATES.values():
        assert " or " not in template and "request" not in template.split("asks")[0]
    form_claim = next(c for c in claims if c.claim_type == ClaimType.FORM_REQUEST)
    assert form_claim.asserted_strength == "none"  # a form request never asserts obligation


def test_explanation_sentences_are_checked_verbatim_and_classified() -> None:
    explanation = (
        "Paragraph 35 requires the trust deed. The form also asks for it. "
        "Where applicable, the deed shall be obtained."
    )
    claims = [c for c in claims_for("CONDITIONAL", {PRIMARY_ROLE}, explanation)
              if c.claim_type == ClaimType.EXPLANATORY_CLAIM]  # fmt: skip
    assert [c.text for c in claims] == [
        "Paragraph 35 requires the trust deed.",
        "The form also asks for it.",
        "Where applicable, the deed shall be obtained.",
    ]
    assert [c.asserted_strength for c in claims] == ["unconditional", "none", "conditional"]
    assert [c.source_sentence_index for c in claims] == [0, 1, 2]
    assert all(c.template_version is None for c in claims)


def test_explanatory_claims_are_bounded_and_overlong_sentences_counted() -> None:
    long = "x" * 320 + "."
    explanation = " ".join([f"Sentence {i} is here." for i in range(9)] + [long])
    req = item("Trust deed", [primary(LIST)], explanation=explanation[:600])
    out = extract(req, entity=EntityType.TRUST, roles={PRIMARY_ROLE})
    expl = [c for c in out.claims if c.claim_type == ClaimType.EXPLANATORY_CLAIM]
    assert len(expl) == MAX_EXPLANATORY
    assert out.skipped >= 1
    ids = [c.claim_id for c in out.claims]
    assert len(set(ids)) == len(ids) and all(i.startswith("REQ_001-C") for i in ids)


def test_unknown_entity_uses_a_neutral_customer_phrase() -> None:
    req = item("Trust deed", [primary(LIST)])
    claims = extract(req, entity=None, roles={PRIMARY_ROLE}).claims
    assert claims[0].text == "Trust deed must be obtained for the customer."
