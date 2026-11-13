"""A2: the assistant's retrieval is HDFC-only and scoped. A fake retriever shows (1) the
filters sent to the search, and (2) that any RBI, unknown or out-of-scope chunk the search
might still return is refused (fail closed), never passed on."""

from typing import Any

import pytest

from app.assistant.retrieval import AssistantRetriever, AssistantScopeError
from app.assistant.taxonomy import AccountType, CustomerCategory, EvidenceScope, SourceRole
from app.retrieval.retrieve import RetrievalMode, RetrievalResult
from app.retrieval.search import SearchFilters, SearchHit

C, A = CustomerCategory, AccountType


def payload(key: str, bank: str = "HDFC", **extra: Any) -> dict[str, Any]:
    return {
        "bank": bank,
        "document_key": key,
        "document_type": "form",
        "page_number": 3,
        "page_end": None,
        "section": "H) NRE ACCOUNT OPENING DETAILS",
        "extraction_method": "text",
        "text_reliability": "native",
        "ocr_confidence": None,
        "content": "Fields: NRE Savings NRE Current",
        **extra,
    }


class FakeRetriever:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self,
        query: str,
        *,
        mode: RetrievalMode | str,
        top_k: int = 5,
        filters: SearchFilters | None = None,
        rerank: bool = False,
    ) -> RetrievalResult:
        self.calls.append({"query": query, "mode": mode, "top_k": top_k, "filters": filters})
        hits = [
            SearchHit(rank=i + 1, score=1.0 - i / 10, chunk_id=f"00000000-0000-4000-8000-{i:012d}",
                      payload=p)  # fmt: skip
            for i, p in enumerate(self.payloads)
        ]
        return RetrievalResult(RetrievalMode(mode), hits, {})


def test_opening_search_is_filtered_to_hdfc_and_the_cells_sources() -> None:
    fake = FakeRetriever(payload("hdfc_nri_account_opening"))
    r = AssistantRetriever(fake).opening("NRE savings", C.NRI_INDIVIDUAL, A.SAVINGS)
    flt = fake.calls[0]["filters"]
    assert flt.banks == ("HDFC",)
    assert flt.document_keys == ("hdfc_nri_account_opening",)
    assert not flt.include_context_only
    assert fake.calls[0]["mode"] is RetrievalMode.DENSE
    assert r.scope is EvidenceScope.OPENING and r.searched and len(r.hits) == 1


def test_an_unsupported_combination_runs_no_search_at_all() -> None:
    fake = FakeRetriever(payload("hdfc_nri_account_opening"))
    retriever = AssistantRetriever(fake)
    for cat, acct in [
        (C.RESIDENT_INDIVIDUAL, A.SAVINGS),
        (C.RESIDENT_INDIVIDUAL, A.CURRENT),
        (C.RESIDENT_INDIVIDUAL, A.SALARY),
        (C.NRI_INDIVIDUAL, A.SALARY),
        (C.LLP, A.SAVINGS),
    ]:
        r = retriever.opening("documents?", cat, acct)
        assert not r.searched and r.hits == () and r.document_keys == ()
    assert fake.calls == []


@pytest.mark.parametrize("bank", ["RBI", "SBI", "", None])
@pytest.mark.parametrize("scope", ["opening", "conversion", "context", "legacy"])
def test_a_non_hdfc_chunk_is_never_returned(bank: Any, scope: str) -> None:
    key = {
        "opening": "hdfc_nri_account_opening",
        "conversion": "hdfc_resident_to_nro_conversion",
        "context": "hdfc_nri_rekyc",
        "legacy": "hdfc_kyc_aml_faq",
    }[scope]
    retriever = AssistantRetriever(FakeRetriever(payload(key, bank=bank)))
    with pytest.raises(AssistantScopeError):
        run(retriever, scope)


@pytest.mark.parametrize(
    "key",
    [
        "rbi_kyc_directions_2025",  # RBI document, even if mislabelled HDFC
        "rbi_kyc_faqs",
        "hdfc_nri_rekyc",  # re-KYC context in the opening scope
        "hdfc_kyc_aml_faq",  # legacy reference in the opening scope
        "hdfc_resident_to_nro_conversion",  # conversion form in the opening scope
        "hdfc_netbanking_corporate_application",  # not used
        "external_website",
    ],
)
def test_an_out_of_scope_chunk_is_refused_in_the_opening_scope(key: str) -> None:
    retriever = AssistantRetriever(FakeRetriever(payload(key)))
    with pytest.raises(AssistantScopeError):
        retriever.opening("NRE savings", C.NRI_INDIVIDUAL, A.SAVINGS)


def test_opening_material_never_appears_in_the_context_or_legacy_scopes() -> None:
    for scope in ("context", "legacy"):
        retriever = AssistantRetriever(FakeRetriever(payload("hdfc_nri_account_opening")))
        with pytest.raises(AssistantScopeError):
            run(retriever, scope)


def test_each_scope_reads_only_its_documents() -> None:
    fake = FakeRetriever()
    retriever = AssistantRetriever(fake)
    retriever.conversion("convert to NRO")
    retriever.context("NRI status proof", C.NRI_INDIVIDUAL)
    retriever.legacy("company documents")
    retriever.opening("LLP current", C.LLP, A.CURRENT)
    keys = [c["filters"].document_keys for c in fake.calls]
    assert keys == [
        ("hdfc_resident_to_nro_conversion",),
        ("hdfc_nri_rekyc",),
        ("hdfc_kyc_aml_faq",),
        ("hdfc_non_individual_account_opening",),
    ]
    assert all(c["filters"].banks == ("HDFC",) for c in fake.calls)


def test_a_hit_without_a_page_is_refused() -> None:
    retriever = AssistantRetriever(
        FakeRetriever(payload("hdfc_nri_account_opening", page_number=None))
    )
    with pytest.raises(AssistantScopeError):
        retriever.opening("q", C.NRI_INDIVIDUAL, A.SAVINGS)


def test_hits_keep_their_source_metadata() -> None:
    ocr = payload(
        "hdfc_non_individual_account_opening",
        page_number=1,
        section=None,
        extraction_method="ocr",
        text_reliability="ocr_uncertain",
        ocr_confidence=0.59,
    )
    r = AssistantRetriever(FakeRetriever(ocr)).opening("LLP current", C.LLP, A.CURRENT)
    hit = r.hits[0]
    assert (hit.document_key, hit.page, hit.page_end, hit.section) == (
        "hdfc_non_individual_account_opening", 1, None, None,
    )  # fmt: skip
    assert hit.source_role is SourceRole.OPENING_FORM
    assert (hit.extraction_method, hit.text_reliability, hit.ocr_confidence) == (
        "ocr",
        "ocr_uncertain",
        0.59,
    )
    assert hit.document_title.startswith("Account Opening Form for Sole Proprietor")
    assert not hit.quotable  # A1 §8.2 rule 3a: OCR-uncertain text is never a verified quotation
    native = AssistantRetriever(FakeRetriever(payload("hdfc_nri_account_opening")))
    assert native.opening("q", C.NRI_INDIVIDUAL, A.SAVINGS).hits[0].quotable


def test_the_mode_is_configurable_but_dense_by_default() -> None:
    fake = FakeRetriever()
    AssistantRetriever(fake, mode=RetrievalMode.HYBRID).opening("q", C.NRI_INDIVIDUAL, A.CURRENT)
    AssistantRetriever(fake).opening("q", C.NRI_INDIVIDUAL, A.CURRENT)
    assert [c["mode"] for c in fake.calls] == [RetrievalMode.HYBRID, RetrievalMode.DENSE]


def run(retriever: AssistantRetriever, scope: str) -> Any:
    if scope == "opening":
        return retriever.opening("q", C.NRI_INDIVIDUAL, A.SAVINGS)
    if scope == "conversion":
        return retriever.conversion("q")
    if scope == "context":
        return retriever.context("q", C.NRI_INDIVIDUAL)
    return retriever.legacy("q")
