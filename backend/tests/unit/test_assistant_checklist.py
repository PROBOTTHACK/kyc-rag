"""A4: evidence-backed checklist generation, without a database or a model.

A fake chunk source is built from the catalogue's own quotes (native text for the NRI and
conversion forms, OCR-uncertain for the scanned form), then corrupted on purpose to show
that missing, moved, foreign or unquoted evidence never reaches a checklist."""

import copy
import json
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.assistant import checklist as checklist_module
from app.assistant.catalogue import (
    CATALOGUE_FILE,
    Catalogue,
    catalogue,
    validate_against,
)
from app.assistant.checklist import (
    AnswerKind,
    CaveatCode,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistService,
    Classification,
    InvalidProfile,
    classify,
)
from app.assistant.chunks import StoredChunk
from app.assistant.coverage import coverage
from app.assistant.dialogue import ReadyProfile
from app.assistant.profile import Slot, slot_for_facet
from app.assistant.retrieval import AssistantRetriever
from app.assistant.taxonomy import (
    AccountType,
    CoverageState,
    CustomerCategory,
    Intent,
    SourceRole,
)
from app.retrieval.retrieve import RetrievalMode, RetrievalResult
from app.retrieval.search import SearchFilters, SearchHit

C, A = CustomerCategory, AccountType
ORGS = [C.SOLE_PROPRIETORSHIP, C.PARTNERSHIP_FIRM, C.LLP, C.COMPANY, C.TRUST, C.HUF,
        C.OTHER_ORGANISATION]  # fmt: skip
FALLBACK = (
    "I couldn't find this information in the available HDFC Bank documents. "
    "I don't want to give you an incorrect answer."
)
NI = "hdfc_non_individual_account_opening"
NRI = "hdfc_nri_account_opening"
CONV = "hdfc_resident_to_nro_conversion"


# --- fakes ---------------------------------------------------------------------------------------


def stored_from_catalogue() -> dict[str, StoredChunk]:
    cov, out = coverage(), {}
    for item in catalogue().items:
        for ev in item.evidence:
            prev = out.get(ev.chunk_id)
            content = f"{prev.content} | {ev.quote}" if prev else f"Fields: {ev.quote}"
            scanned = cov.documents[ev.document_key].scanned
            out[ev.chunk_id] = StoredChunk(
                chunk_id=ev.chunk_id, document_key=ev.document_key, bank="HDFC", page=ev.page,
                page_end=None, section="S", content=content,
                text_reliability="ocr_uncertain" if scanned else "native",
                extraction_method="ocr" if scanned else "text",
            )  # fmt: skip
    return out


class FakeChunks:
    def __init__(self, rows: dict[str, StoredChunk] | None = None, fail: bool = False) -> None:
        self.rows = stored_from_catalogue() if rows is None else rows
        self.fail = fail
        self.calls: list[list[str]] = []

    def get(self, chunk_ids: Iterable[str]) -> dict[str, StoredChunk]:
        ids = sorted(chunk_ids)
        self.calls.append(ids)
        if self.fail:
            raise ConnectionError("database down")
        return {i: self.rows[i] for i in ids if i in self.rows}


class FakeBase:
    """A base retriever returning fixed hits (payloads as stored)."""

    def __init__(self, rows: list[StoredChunk], fail: Exception | None = None) -> None:
        self.rows, self.fail = rows, fail
        self.queries: list[str] = []

    def retrieve(
        self,
        query: str,
        *,
        mode: RetrievalMode | str,
        top_k: int = 5,
        filters: SearchFilters | None = None,
        rerank: bool = False,
    ) -> RetrievalResult:
        self.queries.append(query)
        if self.fail:
            raise self.fail
        keys = set(filters.document_keys) if filters and filters.document_keys else set()
        hits = [
            SearchHit(rank=i + 1, score=1.0, chunk_id=r.chunk_id, payload={
                "bank": r.bank, "document_key": r.document_key, "document_type": "form",
                "page_number": r.page, "page_end": None, "section": r.section,
                "extraction_method": r.extraction_method, "text_reliability": r.text_reliability,
                "ocr_confidence": None, "content": r.content,
            })
            for i, r in enumerate(x for x in self.rows if not keys or x.document_key in keys)
        ]  # fmt: skip
        return RetrievalResult(RetrievalMode(mode), hits, {})


def profile(cat: CustomerCategory | None, acct: AccountType | None, *, topics: tuple[str, ...] = (),
            **facets: str | None) -> ReadyProfile:  # fmt: skip
    if cat is None:
        return ReadyProfile(intent=Intent.RESIDENT_TO_NRO_CONVERSION, customer_category=None,
                            account_type=None, coverage_state=CoverageState.CONVERSION_ONLY,
                            facets={}, topics=topics)  # fmt: skip
    assert acct is not None
    cell = coverage().cell(cat, acct)
    values = {slot_for_facet(f): facets.get(slot_for_facet(f).value) for f in cell.material_facets}
    return ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=cat, account_type=acct,
                        coverage_state=cell.state, facets=values, topics=topics)  # fmt: skip


def build(
    p: ReadyProfile, chunks: FakeChunks | None = None, retriever: Any = None
) -> ChecklistAnswer:
    return ChecklistService(chunks or FakeChunks(), retriever).build(p)


def items(a: ChecklistAnswer) -> list[ChecklistItem]:
    assert a.groups is not None
    return [*a.groups.documents, *a.groups.information, *a.groups.not_confirmed]


def by_id(a: ChecklistAnswer, item_id: str) -> ChecklistItem:
    return next(i for i in items(a) if i.id == item_id)


def ids(group: Iterable[ChecklistItem]) -> list[str]:
    return [i.id for i in group]


def covered_profiles() -> list[ReadyProfile]:
    cov = coverage()
    return [profile(c.category, c.account_type) for c in cov.cells] + [profile(None, None)]


# --- sections and combinations -------------------------------------------------------------------


def test_every_section_is_present_and_separated() -> None:
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, nri_scheme="NRE", holding="SINGLE",
                      minor="NO", topics=("visa_or_status_proof", "pan_card_copy")))  # fmt: skip
    assert a.kind is AnswerKind.CHECKLIST and a.groups is not None
    assert {i.kind for i in a.groups.documents} <= {"document", "declaration"}
    assert {i.kind for i in a.groups.information} == {"information"}
    assert ids(a.groups.not_confirmed) == ["nri_pan_card_copy", "unknown_visa_or_status_proof"]
    assert "nri_photo_first" in ids(a.groups.documents)
    assert "nri_pan_number" in ids(a.groups.information)


@pytest.mark.parametrize(
    "p", covered_profiles(), ids=lambda p: f"{p.customer_category}-{p.account_type}"
)
def test_every_supported_combination_builds_an_evidence_backed_checklist(p: ReadyProfile) -> None:
    a = build(p)
    assert a.kind is AnswerKind.CHECKLIST and a.diagnostics.dropped == ()
    assert a.groups is not None and a.groups.documents or a.groups.information  # type: ignore[union-attr]
    sources = {s.document_key for s in a.sources}
    for item in [*a.groups.documents, *a.groups.information]:  # type: ignore[union-attr]
        assert item.classification is Classification.FORM_INDICATED
        assert item.citations and all(c.document_key in sources for c in item.citations)
    assert a.notice and a.coverage_note and a.fallback is None


@pytest.mark.parametrize(
    ("cat", "acct", "phrase"),
    [
        (C.RESIDENT_INDIVIDUAL, A.SAVINGS, "resident individuals"),
        (C.RESIDENT_INDIVIDUAL, A.CURRENT, "resident individuals"),
        (C.NRI_INDIVIDUAL, A.SALARY, "salary account"),
        (C.LLP, A.SAVINGS, "savings account"),
        (C.COMPANY, A.FCNR_RFC, "NRI individual form"),
    ],
)
def test_unsupported_combinations_get_the_prescribed_fallback(
    cat: CustomerCategory, acct: AccountType, phrase: str
) -> None:
    chunks = FakeChunks()
    a = build(profile(cat, acct), chunks)
    assert a.kind is AnswerKind.FALLBACK and a.fallback == FALLBACK
    assert a.groups is None and a.sources == () and a.reason and phrase in a.reason
    assert a.coverage_note is None  # the note describes a checklist; there is none
    assert a.notice.startswith("These answers are based on HDFC Bank documents")
    assert chunks.calls == []  # no evidence is even read for an unsupported combination


# --- classification ------------------------------------------------------------------------------


def test_classification_follows_the_source_role_only() -> None:
    assert classify(SourceRole.POLICY) is Classification.POLICY_SUPPORTED
    assert classify(SourceRole.OPENING_FORM) is Classification.FORM_INDICATED
    assert classify(SourceRole.CONVERSION_FORM) is Classification.FORM_INDICATED
    assert classify(SourceRole.CONTEXT) is Classification.UNCONFIRMED
    assert classify(SourceRole.LEGACY_REFERENCE) is Classification.LEGACY_REFERENCE
    for role in (SourceRole.NOT_USED, None):
        with pytest.raises(ValueError):
            classify(role)


def test_no_checklist_ever_contains_policy_supported_or_legacy_items() -> None:
    for p in covered_profiles():
        for item in items(build(p)):
            assert item.classification not in (
                Classification.POLICY_SUPPORTED, Classification.LEGACY_REFERENCE,
            )  # fmt: skip


def test_the_catalogue_carries_no_classification() -> None:
    raw = CATALOGUE_FILE.read_text(encoding="utf-8")
    for code in ("policy_supported", "form_indicated", "legacy_reference"):
        assert f": {code}" not in raw  # only as wording labels, never as an item value


# --- PAN number vs PAN card copy; Form 60/61 ------------------------------------------------------


def test_a_pan_number_field_never_becomes_a_pan_card_copy() -> None:
    for p in covered_profiles():
        a = build(p)
        assert a.groups is not None
        for item in [*a.groups.documents, *a.groups.information]:
            assert "card" not in item.title.lower(), item.id
    nri = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, topics=("pan_card_copy",)))
    assert by_id(nri, "nri_pan_number").kind == "information"
    copy_item = by_id(nri, "nri_pan_card_copy")
    assert copy_item.classification is Classification.UNCONFIRMED and copy_item.citations == ()
    assert [c.document_key for c in copy_item.related_citations] == [NRI]  # context, not a basis
    assert "does not establish" in copy_item.does_not_establish


def test_form_60_61_keeps_its_existing_customers_condition() -> None:
    for cat in ORGS:
        a = build(profile(cat, A.CURRENT))
        item = by_id(a, "org_form_60_61")
        assert item.condition == "for existing customers"
        assert item.kind == "document" and item.classification is Classification.FORM_INDICATED
        assert CaveatCode.CONDITIONAL in {c.code for c in item.caveats}


def test_nri_form_60_stays_conditional() -> None:
    item = by_id(build(profile(C.NRI_INDIVIDUAL, A.CURRENT)), "nri_form_60")
    assert item.condition == "If PAN not available"
    assert item.source_wording == "If PAN not available, kindly attach Form 60"


# --- OCR uncertainty and quotations --------------------------------------------------------------


def test_scanned_form_evidence_is_never_a_quotation() -> None:
    a = build(profile(C.LLP, A.CURRENT))
    for item in [*a.groups.documents, *a.groups.information]:  # type: ignore[union-attr]
        assert item.source_wording is None and item.approximate_reading
        assert (
            item.uncertainty_warning
            == "Text read automatically from a scanned page; it may contain errors"
        )
        assert CaveatCode.SCANNED_TEXT in {c.code for c in item.caveats}
        assert not any(c.quotable for c in item.citations)


def test_native_text_is_quoted_exactly() -> None:
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS))
    for item in [*a.groups.documents, *a.groups.information]:  # type: ignore[union-attr]
        assert item.source_wording and item.approximate_reading is None
        assert all(c.quotable for c in item.citations)
        assert any(item.source_wording in c.passage for c in item.citations)


def test_unreliable_stored_text_turns_a_quotation_into_an_approximate_reading() -> None:
    rows = stored_from_catalogue()
    cid = catalogue().item("nri_photo_first").evidence[0].chunk_id  # type: ignore[union-attr]
    rows[cid] = replace(rows[cid], text_reliability="ocr_uncertain")
    item = by_id(build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(rows)), "nri_photo_first")
    assert item.source_wording is None and item.approximate_reading and item.uncertainty_warning


# --- source exclusions ---------------------------------------------------------------------------


def raw_catalogue() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(CATALOGUE_FILE.read_text(encoding="utf-8")))


def check(data: dict[str, Any]) -> None:
    validate_against(Catalogue.model_validate(data), coverage())


@pytest.mark.parametrize(
    "document_key",
    ["hdfc_nri_rekyc", "hdfc_resident_rekyc_dormant", "hdfc_kyc_aml_faq",
     "hdfc_netbanking_corporate_application", CONV, "rbi_kyc_directions_2025"],  # fmt: skip
)
def test_the_catalogue_refuses_any_non_opening_source_for_opening_items(document_key: str) -> None:
    data = raw_catalogue()
    data["items"][0]["evidence"][0]["document_key"] = document_key
    with pytest.raises(ValueError):
        check(data)


def test_the_catalogue_refuses_another_category_s_form() -> None:
    data = raw_catalogue()
    item = next(i for i in data["items"] if i["id"] == "org_type_of_entity")
    item["categories"].append("NRI_INDIVIDUAL")
    with pytest.raises(ValueError):
        check(data)


def test_the_catalogue_refuses_an_opening_form_for_the_conversion() -> None:
    data = raw_catalogue()
    item = next(i for i in data["items"] if i["id"] == "conv_photo_first")
    item["evidence"][0]["document_key"] = NRI
    with pytest.raises(ValueError):
        check(data)


def test_conversion_and_opening_items_never_mix() -> None:
    conv = build(profile(None, None))
    assert all(i.id.startswith("conv_") for i in items(conv))
    assert {c.document_key for i in items(conv) for c in i.citations} == {CONV}
    for p in covered_profiles()[:-1]:
        a = build(p)
        assert not any(i.id.startswith("conv_") for i in items(a))
        assert CONV not in {c.document_key for i in items(a) for c in i.citations}


def test_a_stored_chunk_from_a_forbidden_document_is_dropped() -> None:
    rows = stored_from_catalogue()
    cid = catalogue().item("nri_photo_first").evidence[0].chunk_id  # type: ignore[union-attr]
    rows[cid] = replace(rows[cid], document_key="hdfc_nri_rekyc")
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(rows))
    assert "nri_photo_first" not in ids(items(a)) and "nri_photo_second" not in ids(items(a))
    assert any(i == "nri_photo_first" and "permitted" in r for i, r in a.diagnostics.dropped)


def test_a_non_hdfc_chunk_is_dropped() -> None:
    rows = stored_from_catalogue()
    cid = catalogue().item("org_type_of_entity").evidence[0].chunk_id  # type: ignore[union-attr]
    rows[cid] = replace(rows[cid], bank="RBI")
    a = build(profile(C.LLP, A.CURRENT), FakeChunks(rows))
    assert "org_type_of_entity" not in ids(items(a))


def test_retrieval_cannot_add_out_of_scope_or_unrelated_references() -> None:
    form60 = "If PAN not available, kindly attach Form 60"
    rekyc = StoredChunk("11111111-1111-4111-8111-111111111111", "hdfc_nri_rekyc", "HDFC", 3,
                        None, None, form60, "ocr_reliable", "ocr")  # fmt: skip
    conv = StoredChunk("22222222-2222-4222-8222-222222222222", CONV, "HDFC", 1, None, None,
                       "If PAN not available, kindly attach Form 60", "native", "text")  # fmt: skip
    rows = {**stored_from_catalogue(), rekyc.chunk_id: rekyc, conv.chunk_id: conv}
    retriever = AssistantRetriever(FakeBase([rekyc, conv]))  # the scope filter drops them anyway
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(rows), retriever)
    keys = {c.document_key for i in items(a) for c in [*i.citations, *i.related_citations]}
    assert keys == {NRI}


class LeakyRetriever:
    """Simulates a defective retriever that skips its own scope guard."""

    def __init__(self, *chunk_ids: str) -> None:
        self.chunk_ids = chunk_ids

    def _result(self) -> Any:
        from app.assistant.retrieval import AssistantRetrieval

        hits = tuple(type("Hit", (), {"chunk_id": cid})() for cid in self.chunk_ids)
        return AssistantRetrieval(scope=None, document_keys=(), hits=hits, searched=True)  # type: ignore[arg-type]

    def opening(self, *args: Any, **kwargs: Any) -> Any:
        return self._result()

    def conversion(self, *args: Any, **kwargs: Any) -> Any:
        return self._result()


@pytest.mark.parametrize("document_key", [CONV, NI])
def test_the_service_itself_refuses_permitted_role_chunks_from_other_forms(
    document_key: str,
) -> None:
    """Even if retrieval leaked a conversion-form or other-category chunk carrying the same
    wording, the service's own source check keeps it out of an NRI opening checklist."""
    form60 = "If PAN not available, kindly attach Form 60"
    leaked = StoredChunk("66666666-6666-4666-8666-666666666666", document_key, "HDFC", 1,
                         None, None, form60, "native", "text")  # fmt: skip
    rows = {**stored_from_catalogue(), leaked.chunk_id: leaked}
    a = build(
        profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(rows), LeakyRetriever(leaked.chunk_id)
    )
    item = by_id(a, "nri_form_60")
    assert [c.document_key for c in item.citations] == [NRI]


# --- duplicates, missing and unreliable evidence -------------------------------------------------


def test_repeated_evidence_is_one_citation() -> None:
    item = by_id(build(profile(C.NRI_INDIVIDUAL, A.SAVINGS)), "nri_address_proof")
    assert len(item.citations) == 1  # two quotes from the same chunk


def test_retrieval_adds_distinctive_duplicates_without_losing_references() -> None:
    rows = stored_from_catalogue()
    form60 = "If PAN not available, kindly attach Form 60"
    second = StoredChunk("33333333-3333-4333-8333-333333333333", NRI, "HDFC", 2, None,
                         "D) SECOND APPLICANT'S DETAILS", form60, "native", "text")  # fmt: skip
    first_id = catalogue().item("nri_form_60").evidence[0].chunk_id  # type: ignore[union-attr]
    rows[second.chunk_id] = second
    retriever = AssistantRetriever(FakeBase([rows[first_id], second, rows[first_id]]))
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(rows), retriever)
    item = by_id(a, "nri_form_60")
    assert [(c.page, c.basis) for c in item.citations] == [(1, True), (2, False)]
    assert a.diagnostics.retrieved_citations >= 1


def test_short_labels_found_elsewhere_are_not_added() -> None:
    rows = stored_from_catalogue()
    nominee_dob = StoredChunk("44444444-4444-4444-8444-444444444444", NRI, "HDFC", 3, None, None,
                              "Date of Birth of Nominee", "native", "text")  # fmt: skip
    rows[nominee_dob.chunk_id] = nominee_dob
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(rows),
              AssistantRetriever(FakeBase([nominee_dob])))  # fmt: skip
    assert [c.page for c in by_id(a, "nri_date_of_birth").citations] == [1]


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda r: None,  # the chunk is gone
        lambda r: replace(r, page=r.page + 1),  # moved to another page
        lambda r: replace(r, content="something else entirely"),  # the quote is not there
    ],
    ids=["missing", "page", "quote"],
)
def test_an_item_whose_citation_fails_is_dropped(corrupt: Any) -> None:
    rows = stored_from_catalogue()
    cid = catalogue().item("org_mobile").evidence[0].chunk_id  # type: ignore[union-attr]
    new = corrupt(rows[cid])
    if new is None:
        del rows[cid]
    else:
        rows[cid] = new
    a = build(profile(C.LLP, A.CURRENT), FakeChunks(rows))
    assert "org_mobile" not in ids(items(a))
    assert any(i == "org_mobile" for i, _ in a.diagnostics.dropped)


def test_an_asked_item_whose_evidence_fails_is_reported_as_not_found() -> None:
    rows = stored_from_catalogue()
    for ev in catalogue().item("nri_photo_first").evidence:  # type: ignore[union-attr]
        rows[ev.chunk_id] = replace(rows[ev.chunk_id], content="no photograph here")
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, topics=("photograph",)), FakeChunks(rows))
    unknown = by_id(a, "unknown_photograph")
    assert unknown.classification is Classification.UNKNOWN and unknown.fallback == FALLBACK
    assert unknown.citations == ()


def test_insufficient_evidence_gives_the_fallback() -> None:
    a = build(profile(C.HUF, A.CURRENT), FakeChunks({}))
    assert a.kind is AnswerKind.FALLBACK and a.fallback == FALLBACK and a.groups is None


def test_a_storage_failure_is_a_technical_error_not_a_fallback() -> None:
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), FakeChunks(fail=True))
    assert a.kind is AnswerKind.TECHNICAL_ERROR and a.fallback is None
    assert (
        a.technical_message
        == "I couldn't check the HDFC Bank documents just now. Please try again in a moment."
    )


def test_a_retrieval_failure_keeps_the_cited_evidence() -> None:
    retriever = AssistantRetriever(FakeBase([], fail=ConnectionError("qdrant down")))
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), retriever=retriever)
    assert a.kind is AnswerKind.CHECKLIST and a.diagnostics.retrieval_degraded


def test_an_out_of_scope_retrieval_result_disables_retrieval_for_the_build() -> None:
    rbi = StoredChunk(
        "55555555-5555-4555-8555-555555555555", NRI, "RBI", 1, None, None, "x", "native", "text"
    )
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS), retriever=AssistantRetriever(FakeBase([rbi])))
    assert a.kind is AnswerKind.CHECKLIST and a.diagnostics.retrieval_degraded
    assert a.diagnostics.retrieved_citations == 0


# --- topics, facets, on-request items ------------------------------------------------------------


def test_unknown_facets_show_the_section_with_an_applies_if_note() -> None:
    unknown = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS))
    assert by_id(unknown, "nri_photo_second").applies_if == (
        "Applies if the account is held jointly.",
    )
    single = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, holding="SINGLE"))
    assert "nri_photo_second" not in ids(items(single))
    joint = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, holding="JOINT"))
    assert by_id(joint, "nri_photo_second").applies_if == ()


def test_the_nri_scheme_selects_its_section() -> None:
    nre = build(profile(C.NRI_INDIVIDUAL, A.CURRENT, nri_scheme="NRE"))
    assert "nri_nre_account_choice" in ids(items(nre)) and "nri_nro_account_choice" not in ids(
        items(nre)
    )


def test_on_request_items_appear_only_when_asked() -> None:
    assert "nri_aadhaar_number" not in ids(items(build(profile(C.NRI_INDIVIDUAL, A.SAVINGS))))
    asked = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, topics=("aadhaar",)))
    item = by_id(asked, "nri_aadhaar_number")
    assert item.asked and item.note and "does not establish" in item.note


def test_asked_topics_are_marked_and_unanswered_ones_are_not_found() -> None:
    a = build(profile(C.COMPANY, A.CURRENT, topics=("pan", "passport", "documents")))
    assert by_id(a, "org_signatory_pan_number").asked
    assert ids(a.groups.not_confirmed)[-1] == "unknown_passport"  # type: ignore[union-attr]
    assert "unknown_documents" not in ids(items(a))


# --- A2 rulings (regression) ---------------------------------------------------------------------


@pytest.mark.parametrize("cat", ORGS)
@pytest.mark.parametrize("acct", [A.CURRENT, A.FIXED_DEPOSIT, A.RECURRING_DEPOSIT])
def test_a2_rulings_hold_for_every_organisation_checklist(
    cat: CustomerCategory, acct: AccountType
) -> None:
    a = build(profile(cat, acct, topics=("photograph", "pan_card_copy")))
    assert a.groups is not None
    a_b = [*a.groups.documents, *a.groups.information]
    assert not any("photo" in i.title.lower() for i in a_b)  # no signatory photographs
    photo = by_id(a, "org_signatory_photograph")
    assert (
        photo.classification is Classification.UNCONFIRMED and photo.asked and photo.citations == ()
    )
    card = by_id(a, "org_entity_pan_card_copy")
    assert card.classification is Classification.UNCONFIRMED and card.citations == ()
    pan = by_id(a, "org_signatory_pan_number")
    assert pan.classification is Classification.FORM_INDICATED and pan.kind == "information"
    assert {c.chunk_id[:8] for c in pan.citations} == {"f51508ae", "600f4dd7"}
    form60 = by_id(a, "org_form_60_61")
    assert form60.condition == "for existing customers" and form60.kind == "document"
    assert [c.chunk_id[:8] for c in form60.citations] == ["1c8150b4"]


def ruling_mutations() -> list[tuple[str, Any]]:
    def item(d: dict[str, Any], i: str) -> dict[str, Any]:
        return next(x for x in [*d["items"], *d["not_confirmed"]] if x["id"] == i)

    return [
        ("drop_condition", lambda d: item(d, "org_form_60_61").pop("condition")),
        ("other_condition", lambda d: item(d, "org_form_60_61").update(condition="Form 60/61")),
        ("pan_as_document", lambda d: item(d, "org_signatory_pan_number").update(kind="document")),
        ("pan_one_chunk", lambda d: item(d, "org_signatory_pan_number")["evidence"].pop()),
        ("photo_unreferenced", lambda d: d["not_confirmed"].pop(0)),
        ("photo_as_item", lambda d: d["items"].append({
            "id": "org_photo", "ruling": "org_signatory_photograph", "title": "Photographs",
            "kind": "document", "scope": "opening", "categories": ["LLP"],
            "evidence": [{"chunk_id": "1c8150b4-c49f-5b26-9c14-a1a2c5535102",
                          "document_key": NI, "page": 1, "quote": "Mobile No."}]})),
        ("pan_card_as_item", lambda d: d["items"].append({
            "id": "org_pan_card", "title": "PAN card copy", "kind": "document", "scope": "opening",
            "categories": ["LLP"], "ruling": "org_entity_pan_card_copy",
            "evidence": [{"chunk_id": "f51508ae-d9ec-5594-bd70-c0cada97e60a",
                          "document_key": NI, "page": 2, "quote": "PAN No."}]})),
    ]  # fmt: skip


@pytest.mark.parametrize(("name", "mutate"), ruling_mutations())
def test_the_catalogue_cannot_contradict_an_a2_ruling(name: str, mutate: Any) -> None:
    data = raw_catalogue()
    mutate(data)
    with pytest.raises(ValueError):
        check(data)


# --- invalid profiles ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "p",
    [
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.LLP, account_type=None,
                     coverage_state=CoverageState.COVERED_OCR, facets={}, topics=()),
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=None, account_type=A.CURRENT,
                     coverage_state=CoverageState.COVERED_OCR, facets={}, topics=()),
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.LLP, account_type=A.CURRENT,
                     coverage_state=CoverageState.COVERED, facets={}, topics=()),  # wrong state
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.RESIDENT_INDIVIDUAL,
                     account_type=A.SAVINGS, coverage_state=CoverageState.COVERED, facets={},
                     topics=()),  # claims coverage the map does not give
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.LLP, account_type=A.CURRENT,
                     coverage_state=CoverageState.COVERED_OCR, facets={Slot.HOLDING: "JOINT"},
                     topics=()),  # an immaterial facet
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.NRI_INDIVIDUAL,
                     account_type=A.FCNR_RFC, coverage_state=CoverageState.COVERED, facets={},
                     topics=()),  # a material facet missing
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.NRI_INDIVIDUAL,
                     account_type=A.FCNR_RFC, coverage_state=CoverageState.COVERED,
                     facets={Slot.HOLDING: "TRIPLE"}, topics=()),
        ReadyProfile(intent=Intent.OPEN_ACCOUNT, customer_category=C.LLP, account_type=A.CURRENT,
                     coverage_state=CoverageState.COVERED_OCR, facets={}, topics=("loans",)),
        ReadyProfile(intent=Intent.RESIDENT_TO_NRO_CONVERSION, customer_category=None,
                     account_type=None, coverage_state=CoverageState.COVERED, facets={}, topics=()),
    ],
)  # fmt: skip
def test_invalid_or_incomplete_profiles_are_refused(p: ReadyProfile) -> None:
    chunks = FakeChunks()
    with pytest.raises(InvalidProfile):
        build(p, chunks)
    assert chunks.calls == []


# --- output --------------------------------------------------------------------------------------


def test_output_is_deterministic_without_any_llm() -> None:
    assert not hasattr(checklist_module, "LLMProvider")
    src = checklist_module.__file__
    assert "app.llm" not in open(src, encoding="utf-8").read()  # noqa: PTH123, SIM115
    for p in covered_profiles():
        assert build(p).model_dump_json() == build(p).model_dump_json()


def test_serialised_output_hides_internal_ids_and_has_no_approval_language() -> None:
    a = build(profile(C.NRI_INDIVIDUAL, A.SAVINGS, topics=("pan_card_copy",)))
    data = json.loads(a.model_dump_json())
    text = json.dumps(data).lower()
    assert "chunk_id" not in text and "diagnostics" not in text
    for word in ("approved", "eligible", "compliant", "rejected"):
        assert word not in text
    assert data["notice"] and data["coverage_note"]
    first = data["groups"]["documents"][0]
    assert {
        "document_key",
        "document_title",
        "page",
        "text_reliability",
        "date_basis",
        "passage",
    } <= set(first["citations"][0])


def test_every_item_has_its_classification_label_and_explanation() -> None:
    for p in covered_profiles():
        for item in items(build(p)):
            assert item.classification_label and item.does_not_establish
            if item.classification is Classification.FORM_INDICATED:
                assert item.citations and item.establishes
                assert item.caveats[0].code is CaveatCode.DATED_SOURCE
            else:
                assert item.citations == ()  # group C never cites a basis


def test_the_shipped_catalogue_is_valid_and_complete() -> None:
    cat = catalogue()
    validate_against(cat, coverage())
    rulings = {r.id for r in coverage().item_rulings}
    used = {i.ruling for i in cat.items if i.ruling} | {
        e.ruling for e in cat.not_confirmed if e.ruling
    }
    assert used == rulings


def test_an_extra_field_in_the_catalogue_is_refused() -> None:
    data = raw_catalogue()
    data["items"][0]["classification"] = "policy_supported"
    with pytest.raises(ValidationError):
        Catalogue.model_validate(data)


# --- Group C visibility (owner decision at the A4 review) ----------------------------------------


def all_not_confirmed(a: ChecklistAnswer) -> list[str]:
    assert a.groups is not None
    return ids(a.groups.not_confirmed)


@pytest.mark.parametrize(
    "p", covered_profiles(), ids=lambda p: f"{p.customer_category}-{p.account_type}"
)
def test_a_general_checklist_shows_no_could_not_be_confirmed_items(p: ReadyProfile) -> None:
    assert all_not_confirmed(build(p)) == []


@pytest.mark.parametrize("cat", ORGS)
def test_a_general_company_checklist_never_mentions_photographs_or_pan_card_copies(
    cat: CustomerCategory,
) -> None:
    a = build(profile(cat, A.CURRENT, topics=("documents",)))  # "what do I need?"
    assert all_not_confirmed(a) == []
    titles = [i.title.lower() for i in items(a)]
    assert not any("photograph" in t or "card" in t for t in titles)
    text = a.model_dump_json().lower()
    assert "photograph" not in text and "pan card" not in text and "card copy" not in text


@pytest.mark.parametrize(
    "p", covered_profiles(), ids=lambda p: f"{p.customer_category}-{p.account_type}"
)
def test_no_general_checklist_mentions_a_pan_card(p: ReadyProfile) -> None:
    """Owner decision at the A4 content review: the PAN-number items say only what the form
    asks for; a card copy is discussed only in Group C, and only when asked."""
    for topics in ((), ("documents",), ("pan",)):
        a = build(p.model_copy(update={"topics": topics}))
        assert "pan card" not in a.model_dump_json().lower(), topics
    pan = [i for i in items(build(p)) if i.id.endswith("pan_number")]
    assert len(pan) == 1 and pan[0].kind == "information"
    assert pan[0].note in (
        "The form has a field for the PAN number.",
        "The form has PAN number fields for signatories and partners.",
    )


def test_asking_about_a_card_copy_still_explains_it_is_not_established() -> None:
    for p, entry in (
        (profile(C.NRI_INDIVIDUAL, A.SAVINGS, topics=("pan_card_copy",)), "nri_pan_card_copy"),
        (profile(C.COMPANY, A.CURRENT, topics=("pan_card_copy",)), "org_entity_pan_card_copy"),
        (profile(None, None, topics=("pan_card_copy",)), "conv_pan_card_copy"),
    ):
        item = by_id(build(p), entry)
        assert item.classification is Classification.UNCONFIRMED and item.citations == ()
        assert "does not establish" in item.does_not_establish or "No applicable" in (
            item.does_not_establish
        )


def test_asking_about_signatory_photographs_shows_the_unconfirmed_result() -> None:
    a = build(profile(C.COMPANY, A.CURRENT, topics=("photograph",)))
    assert all_not_confirmed(a) == ["org_signatory_photograph"]
    item = by_id(a, "org_signatory_photograph")
    assert item.asked and item.classification is Classification.UNCONFIRMED
    assert item.classification_label == "Could not be confirmed"
    assert item.citations == () and item.related_citations == () and item.fallback is None
    assert "No stored text or form-schema field" in item.does_not_establish
    assert not any("photo" in i.title.lower() for i in [*a.groups.documents, *a.groups.information])  # type: ignore[union-attr]


def test_asking_about_a_pan_card_copy_shows_the_unconfirmed_result_only() -> None:
    a = build(profile(C.LLP, A.CURRENT, topics=("pan_card_copy",)))
    assert all_not_confirmed(a) == ["org_entity_pan_card_copy"]
    card = by_id(a, "org_entity_pan_card_copy")
    assert card.citations == ()  # the PAN No. field is never its evidence
    assert {c.chunk_id[:8] for c in card.related_citations} == {"f51508ae", "600f4dd7"}
    assert by_id(a, "org_signatory_pan_number").classification is Classification.FORM_INDICATED


def test_asking_about_pan_does_not_raise_a_pan_card_copy() -> None:
    for p in (profile(C.COMPANY, A.CURRENT, topics=("pan",)),
              profile(C.NRI_INDIVIDUAL, A.SAVINGS, topics=("pan",)),
              profile(None, None, topics=("pan",))):  # fmt: skip
        a = build(p)
        assert all_not_confirmed(a) == []
        assert any(i.asked and i.kind == "information" for i in a.groups.information)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("p", "expected"),
    [
        (profile(C.NRI_INDIVIDUAL, A.CURRENT, topics=("pan_card_copy",)), "nri_pan_card_copy"),
        (profile(None, None, topics=("pan_card_copy",)), "conv_pan_card_copy"),
    ],
)
def test_pan_card_copy_assumptions_follow_the_same_rule(p: ReadyProfile, expected: str) -> None:
    a = build(p)
    assert all_not_confirmed(a) == [expected]
    assert by_id(a, expected).citations == ()


def test_the_rule_works_from_the_customer_s_words_through_the_dialogue() -> None:
    from app.assistant.dialogue import StepKind, TurnInput, respond_deterministic
    from app.assistant.profile import DialogueState

    def ready(*messages: str) -> ReadyProfile:
        state = DialogueState()
        for m in messages:
            state, r = respond_deterministic(state, TurnInput(message=m))
        assert r.step is StepKind.READY and r.ready is not None
        return r.ready

    general = build(ready("What documents does my company need for a current account?"))
    assert all_not_confirmed(general) == []
    asked = build(ready("current account for my company", "Do the signatories need photographs?"))
    assert all_not_confirmed(asked) == ["org_signatory_photograph"]
    card = build(ready("Does my LLP need a PAN card for a current account?"))
    assert all_not_confirmed(card) == ["org_entity_pan_card_copy"]


def test_visibility_does_not_change_the_rulings_or_the_catalogue() -> None:
    cov = coverage()
    assert {r.id: r.status for r in cov.item_rulings} == {
        "org_signatory_photograph": "not_established",
        "org_entity_pan_card_copy": "not_established",
        "org_signatory_pan_number": "evidenced",
        "org_form_60_61": "evidenced",
    }
    assert cov.version == "a2-2" and catalogue().version == "a4-1"
    assert len(catalogue().items) == 47 and len(catalogue().not_confirmed) == 4
