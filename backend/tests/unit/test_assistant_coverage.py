"""A2: the coverage map reproduces the approved A0 matrix and fails closed on any map
that would create coverage without evidence or from a forbidden source."""

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.assistant.coverage import COVERAGE_FILE, Coverage, ItemRuling, coverage, load_coverage
from app.assistant.taxonomy import (
    AccountType,
    CoverageState,
    CustomerCategory,
    EvidenceScope,
    Facet,
    SourceRole,
)

C = CustomerCategory
A = AccountType
S = CoverageState
ORGS = [C.SOLE_PROPRIETORSHIP, C.PARTNERSHIP_FIRM, C.LLP, C.COMPANY, C.TRUST, C.HUF,
        C.OTHER_ORGANISATION]  # fmt: skip


def expected_state(cat: CustomerCategory, acct: AccountType) -> CoverageState:
    """The A0 §2 matrix, written independently of the YAML."""
    if cat is C.NRI_INDIVIDUAL:
        return S.NOT_ESTABLISHED if acct is A.SALARY else S.COVERED
    if cat in ORGS:
        if acct in (A.CURRENT, A.FIXED_DEPOSIT, A.RECURRING_DEPOSIT):
            return S.COVERED_OCR
        return S.NOT_ESTABLISHED
    return S.NOT_ESTABLISHED  # resident individuals: nothing established


def raw() -> dict[str, Any]:
    data = yaml.safe_load(COVERAGE_FILE.read_text(encoding="utf-8"))
    data.pop("_evidence", None)
    return copy.deepcopy(data)


def test_every_cell_matches_the_approved_matrix() -> None:
    cov = coverage()
    for cat in CustomerCategory:
        for acct in AccountType:
            assert cov.cell(cat, acct).state is expected_state(cat, acct), (cat, acct)
    assert len(cov.cells) == 5 + 3 * len(ORGS)


@pytest.mark.parametrize(
    ("cat", "acct", "phrase"),
    [
        (C.RESIDENT_INDIVIDUAL, A.SAVINGS, "resident individuals"),
        (C.RESIDENT_INDIVIDUAL, A.SALARY, "resident individuals"),
        (C.NRI_INDIVIDUAL, A.SALARY, "salary account"),
        (C.LLP, A.SAVINGS, "savings account"),
        (C.COMPANY, A.FCNR_RFC, "NRI individual form"),
    ],
)
def test_unsupported_cells_explain_why_and_have_no_sources(
    cat: CustomerCategory, acct: AccountType, phrase: str
) -> None:
    cell = coverage().cell(cat, acct)
    assert cell.state is S.NOT_ESTABLISHED
    assert not cell.sources and not cell.evidence and not cell.facets
    assert cell.reason and phrase in cell.reason


def test_covered_cells_cite_evidence_from_their_own_sources() -> None:
    for cell in coverage().cells:
        assert cell.evidence
        for ev in cell.evidence:
            assert ev.document_key in cell.sources
        assert set(cell.sources) <= {
            "hdfc_nri_account_opening",
            "hdfc_non_individual_account_opening",
        }


def test_material_facets_only_where_the_form_has_them() -> None:
    cov = coverage()
    nri = cov.cell(C.NRI_INDIVIDUAL, A.SAVINGS)
    assert set(nri.material_facets) == {Facet.NRI_SCHEME, Facet.HOLDING, Facet.MINOR}
    for cat in ORGS:
        assert cov.cell(cat, A.CURRENT).material_facets == ()


def test_scopes_keep_opening_context_and_legacy_apart() -> None:
    cov = coverage()
    nri_open = cov.documents_for(EvidenceScope.OPENING, C.NRI_INDIVIDUAL, A.SAVINGS)
    assert nri_open == ("hdfc_nri_account_opening",)
    assert cov.documents_for(EvidenceScope.OPENING, C.RESIDENT_INDIVIDUAL, A.SAVINGS) == ()
    assert cov.documents_for(EvidenceScope.CONVERSION) == ("hdfc_resident_to_nro_conversion",)
    assert cov.documents_for(EvidenceScope.CONTEXT, C.NRI_INDIVIDUAL) == ("hdfc_nri_rekyc",)
    assert cov.documents_for(EvidenceScope.CONTEXT, C.RESIDENT_INDIVIDUAL) == (
        "hdfc_resident_rekyc_dormant",
    )
    assert cov.documents_for(EvidenceScope.LEGACY) == ("hdfc_kyc_aml_faq",)
    every = {
        key
        for scope in EvidenceScope
        for cat in CustomerCategory
        for acct in AccountType
        for key in cov.documents_for(scope, cat, acct)
    }
    assert all(k.startswith("hdfc_") for k in every)  # no RBI or other source anywhere
    assert "hdfc_netbanking_corporate_application" not in every  # not used


def test_roles_follow_the_source_hierarchy() -> None:
    roles = {k: d.role for k, d in coverage().documents.items()}
    assert roles["hdfc_kyc_aml_faq"] is SourceRole.LEGACY_REFERENCE
    assert roles["hdfc_nri_rekyc"] is SourceRole.CONTEXT
    assert roles["hdfc_resident_rekyc_dormant"] is SourceRole.CONTEXT
    assert SourceRole.POLICY not in roles.values()  # no tier-1 policy in today's corpus


# --- fail closed ---------------------------------------------------------------------------------


def nri_savings(data: dict[str, Any]) -> dict[str, Any]:
    return next(
        c for c in data["cells"]
        if c["category"] == "NRI_INDIVIDUAL" and c["account_type"] == "SAVINGS"
    )  # fmt: skip


def mutations() -> list[tuple[str, Any]]:
    def context_source(d: dict[str, Any]) -> None:
        nri_savings(d)["sources"] = ["hdfc_nri_rekyc"]
        for ev in nri_savings(d)["evidence"]:
            ev["document_key"] = "hdfc_nri_rekyc"
        nri_savings(d)["facets"] = []

    def legacy_source(d: dict[str, Any]) -> None:
        nri_savings(d)["sources"].append("hdfc_kyc_aml_faq")

    def conversion_as_opening(d: dict[str, Any]) -> None:
        nri_savings(d)["sources"].append("hdfc_resident_to_nro_conversion")

    def no_evidence(d: dict[str, Any]) -> None:
        nri_savings(d)["evidence"] = []

    def listed_not_established(d: dict[str, Any]) -> None:
        d["cells"].append({"category": "RESIDENT_INDIVIDUAL", "account_type": "SAVINGS",
                           "state": "NOT_ESTABLISHED"})  # fmt: skip

    def resident_coverage_by_assumption(d: dict[str, Any]) -> None:
        cell = copy.deepcopy(nri_savings(d))
        cell["category"] = "RESIDENT_INDIVIDUAL"  # the NRI form does not cover residents
        d["cells"].append(cell)

    def scanned_marked_covered(d: dict[str, Any]) -> None:
        cell = next(
            c for c in d["cells"] if c["category"] == "LLP" and c["account_type"] == "CURRENT"
        )
        cell["state"] = "COVERED"

    def foreign_evidence(d: dict[str, Any]) -> None:
        nri_savings(d)["evidence"][0]["document_key"] = "rbi_kyc_directions_2025"

    def unknown_document(d: dict[str, Any]) -> None:
        nri_savings(d)["sources"] = ["rbi_kyc_directions_2025"]

    def schema_without_corroboration(d: dict[str, Any]) -> None:
        cell = next(
            c for c in d["cells"] if c["category"] == "LLP" and c["account_type"] == "CURRENT"
        )
        for ev in cell["evidence"]:
            if ev["basis"] == "schema_field":
                ev.pop("corroborated")

    def conversion_with_opening_form(d: dict[str, Any]) -> None:
        d["conversion"]["sources"] = ["hdfc_nri_account_opening"]

    def duplicate_cell(d: dict[str, Any]) -> None:
        d["cells"].append(copy.deepcopy(nri_savings(d)))

    def bad_chunk_id(d: dict[str, Any]) -> None:
        nri_savings(d)["evidence"][0]["chunk_id"] = "not-a-uuid"

    return [(f.__name__, f) for f in (
        context_source, legacy_source, conversion_as_opening, no_evidence,
        listed_not_established, resident_coverage_by_assumption, scanned_marked_covered,
        foreign_evidence, unknown_document, schema_without_corroboration,
        conversion_with_opening_form, duplicate_cell, bad_chunk_id,
    )]  # fmt: skip


@pytest.mark.parametrize(("name", "mutate"), mutations())
def test_a_map_that_would_create_unsupported_coverage_fails_to_load(name: str, mutate: Any) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError):
        Coverage.model_validate(data)


def test_the_shipped_file_loads_through_the_public_loader() -> None:
    assert load_coverage().version == coverage().version


# --- item rulings (A1 §5.4; owner decisions at the A2 review) ------------------------------------


def test_signatory_photographs_are_not_established_for_any_organisation() -> None:
    r = coverage().ruling("org_signatory_photograph")
    assert r is not None and r.status == "not_established" and r.kind == "document"
    assert set(r.categories) == set(ORGS) and r.evidence == () and r.reason
    for cat in ORGS:
        assert r in coverage().rulings_for(cat)
    # and nothing anywhere in the map cites a photograph for organisations
    for cell in coverage().cells:
        if cell.category in ORGS:
            assert not any("photo" in ev.shows.lower() for ev in cell.evidence)


def test_signatory_pan_number_is_evidenced_information_and_no_entity_pan_card_copy() -> None:
    cov = coverage()
    pan = cov.ruling("org_signatory_pan_number")
    assert pan is not None and pan.status == "evidenced" and pan.kind == "information"
    assert {e.chunk_id[:8] for e in pan.evidence} == {"f51508ae", "600f4dd7"}
    form60 = cov.ruling("org_form_60_61")
    assert form60 is not None and form60.kind == "document" and form60.status == "evidenced"
    assert [e.chunk_id[:8] for e in form60.evidence] == ["1c8150b4"]
    assert form60.condition == "for existing customers"
    card = cov.ruling("org_entity_pan_card_copy")
    assert card is not None and card.status == "not_established" and card.evidence == ()
    # no ruling establishes a PAN card copy for anyone
    assert not any(
        r.status == "evidenced" and "card" in r.subject.lower() for r in cov.item_rulings
    )


def test_rulings_carry_no_classification() -> None:
    fields = set(ItemRuling.model_fields)
    assert not fields & {"classification", "evidence_class", "mandatory", "policy_supported"}


def rulings_mutations() -> list[tuple[str, Any]]:
    def evidence_on_not_established(d: dict[str, Any]) -> None:
        ruling(d, "org_signatory_photograph")["evidence"] = ruling(d, "org_signatory_pan_number")[
            "evidence"
        ]

    def evidenced_without_evidence(d: dict[str, Any]) -> None:
        ruling(d, "org_signatory_pan_number")["evidence"] = []

    def rekyc_source(d: dict[str, Any]) -> None:
        r = ruling(d, "org_signatory_pan_number")
        r["sources"] = ["hdfc_nri_rekyc"]

    def foreign_category(d: dict[str, Any]) -> None:
        ruling(d, "org_form_60_61")["categories"].append("RESIDENT_INDIVIDUAL")

    def classification_field(d: dict[str, Any]) -> None:
        ruling(d, "org_form_60_61")["classification"] = "policy_supported"

    def evidence_from_other_document(d: dict[str, Any]) -> None:
        ruling(d, "org_form_60_61")["evidence"][0]["document_key"] = "hdfc_nri_account_opening"

    def duplicate(d: dict[str, Any]) -> None:
        d["item_rulings"].append(copy.deepcopy(ruling(d, "org_form_60_61")))

    return [(f.__name__, f) for f in (
        evidence_on_not_established, evidenced_without_evidence, rekyc_source, foreign_category,
        classification_field, evidence_from_other_document, duplicate,
    )]  # fmt: skip


def ruling(d: dict[str, Any], rid: str) -> dict[str, Any]:
    return next(r for r in d["item_rulings"] if r["id"] == rid)


@pytest.mark.parametrize(("name", "mutate"), rulings_mutations())
def test_an_invalid_item_ruling_fails_to_load(name: str, mutate: Any) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError):
        Coverage.model_validate(data)
