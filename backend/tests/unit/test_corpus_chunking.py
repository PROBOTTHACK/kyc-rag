"""Stage C golden checks on the real corpus (skipped without the gitignored PDFs/Tesseract).

Expected values were checked against the PDFs during M1 Phase 3 Stage C. The OCR cache
(data/processed/ocr_cache) keeps the scanned pages fast after the first run.
"""

from typing import Any

import pytest

from app.config import REPO_ROOT, get_settings
from app.ingestion.chunking import QUALIFIER, ChunkingResult, chunk_document
from app.ingestion.manifest import load_manifest
from app.ingestion.ocr import OcrSetupError, tesseract_setup
from app.ingestion.pages import OcrCache, process_document
from app.schemas.corpus import ChunkMetadata, chunk_id
from app.schemas.taxonomy import (
    ChunkingProfile,
    ChunkRole,
    ContentKind,
    EntityType,
    EvidenceEligibility,
    ExtractionMethod,
    PageStatus,
    TextReliability,
)

CORPUS = REPO_ROOT / "data" / "raw_pdfs"
pytestmark = pytest.mark.skipif(
    not (CORPUS / "regulatory" / "169MD.pdf").exists(), reason="source PDFs not present"
)
WORD_LIMIT = 300


def _chunk_all() -> dict[str, tuple[Any, ChunkingResult, int]]:
    settings = get_settings()
    try:
        setup = tesseract_setup(settings)
    except OcrSetupError as exc:
        pytest.skip(f"Tesseract not available: {exc}")
    cache = OcrCache(settings.OCR_CACHE_DIR)
    out: dict[str, tuple[Any, ChunkingResult, int]] = {}
    for entry in load_manifest(settings.CORPUS_MANIFEST):
        extracted, pages = process_document(
            CORPUS / entry.file, pdf_sha256=entry.sha256, setup=setup, cache=cache,
            detect_tables=entry.effective_chunking_profile is ChunkingProfile.RBI_DIRECTIONS,
        )  # fmt: skip
        out[entry.document_key] = (entry, chunk_document(entry, extracted, pages), len(pages))
    return out


@pytest.fixture(scope="module")
def corpus() -> dict[str, tuple[Any, ChunkingResult, int]]:
    return _chunk_all()


def metas(corpus: dict[str, Any], key: str) -> list[ChunkMetadata]:
    return [c.metadata for c in corpus[key][1].chunks]


def find(corpus: dict[str, Any], key: str, **attrs: Any) -> list[ChunkMetadata]:
    return [m for m in metas(corpus, key) if all(getattr(m, k) == v for k, v in attrs.items())]


# --- integrity across all documents ------------------------------------------------


def test_every_page_with_text_is_cited_and_ids_are_deterministic(corpus: dict[str, Any]) -> None:
    for key, (entry, result, n_pages) in corpus.items():
        cited: set[int] = set()
        for i, rec in enumerate(result.chunks):
            m = rec.metadata
            assert m.chunk_index == i and m.chunk_id == str(chunk_id(entry.sha256, i)), key
            assert 1 <= m.page_number <= n_pages, key
            assert m.page_end is None or m.page_number < m.page_end <= n_pages, key
            cited.update(range(m.page_number, (m.page_end or m.page_number) + 1))
            assert rec.extras["raw_text"].strip(), key
            # Document-level metadata is copied, never re-derived per chunk.
            assert (m.bank, m.document_type, m.source_status) == (
                entry.bank, entry.document_type, entry.source_status,
            ), key  # fmt: skip
            assert m.evidence_eligibility is entry.evidence_eligibility, key
            if rec.embeddable:
                assert len(m.content.split()) <= WORD_LIMIT, (key, i)
        expected = set(range(1, n_pages + 1)) - ({2} if key == "rbi_kyc_directions_2025" else set())
        assert cited >= expected, (key, sorted(expected - cited))  # RBI p2 = contents only


def test_chunking_is_repeatable(corpus: dict[str, Any]) -> None:
    again = _chunk_all()
    for key, (_, result, _) in corpus.items():
        first = [(c.metadata.to_qdrant_payload(), c.extras) for c in result.chunks]
        second = [(c.metadata.to_qdrant_payload(), c.extras) for c in again[key][1].chunks]
        assert first == second, key


def test_parents_and_children(corpus: dict[str, Any]) -> None:
    for key in corpus:
        ids = {m.chunk_id: m for m in metas(corpus, key)}
        for m in ids.values():
            if m.chunk_role is ChunkRole.CHILD:
                parent = ids[m.parent_chunk_id or ""]
                assert parent.chunk_role is ChunkRole.PARENT and parent.paragraph == m.paragraph
            else:
                assert m.parent_chunk_id is None


# --- RBI Directions -----------------------------------------------------------------


def test_directions_paragraphs_sections_entities(corpus: dict[str, Any]) -> None:
    legal = (
        "Chapter VI – Customer Due Diligence (CDD) Procedure > C. CDD Measures for Legal Entities"
    )
    (p33,) = find(corpus, "rbi_kyc_directions_2025", paragraph="33")
    assert (p33.page_number, p33.page_end, p33.section) == (37, None, legal)
    assert p33.entity_types == [EntityType.COMPANY]
    assert p33.content.startswith("33. For opening an account of a company")
    (p34,) = find(corpus, "rbi_kyc_directions_2025", paragraph="34")
    assert (p34.page_number, p34.page_end) == (37, 38) and p34.entity_types == [
        EntityType.PARTNERSHIP
    ]
    (p30,) = find(corpus, "rbi_kyc_directions_2025", paragraph="30")
    assert p30.entity_types == [EntityType.SOLE_PROPRIETORSHIP]
    assert p30.section and p30.section.endswith("B. CDD Measures for Sole Proprietary firms")
    (p14,) = find(corpus, "rbi_kyc_directions_2025", paragraph="14")
    assert p14.section == "Chapter II – General > B. Other General Guidelines:"


def test_directions_footnotes(corpus: dict[str, Any]) -> None:
    cited = {
        (m.paragraph, m.chunk_role.value, f.page_number, f.marker)
        for m in metas(corpus, "rbi_kyc_directions_2025") for f in m.footnotes
    }  # fmt: skip
    assert cited == {
        ("5", "parent", 6, "1"), ("5", "child", 6, "1"),
        ("11", "single", 18, "2"),
        ("65", "parent", 62, "3"), ("65", "child", 62, "3"),
    }  # fmt: skip
    for m in metas(corpus, "rbi_kyc_directions_2025"):
        assert "Inserted with effect" not in m.content and "Substituted w.e.f." not in m.content
        for f in m.footnotes:
            assert "2026" in f.text or "2025" in f.text


def test_directions_long_paragraph_and_tables(corpus: dict[str, Any]) -> None:
    p42 = find(corpus, "rbi_kyc_directions_2025", paragraph="42")
    parent = [m for m in p42 if m.chunk_role is ChunkRole.PARENT]
    assert len(parent) == 1 and (parent[0].page_number, parent[0].page_end) == (41, 45)
    assert len([m for m in p42 if m.chunk_role is ChunkRole.CHILD]) >= 3
    rows = find(corpus, "rbi_kyc_directions_2025", content_kind=ContentKind.TABLE_ROW)
    assert len(rows) == 21 and {r.page_number for r in rows} == {49, 50, 51}
    assert rows[0].content.startswith("Document Type: Entity Level | Constitutive Documents")
    assert "Category I: Mandatory" in rows[0].content
    assert any(
        r.content.startswith("Category: I. | Eligible Foreign Investors: Government") for r in rows
    )


def test_directions_excludes_masthead_and_headings(corpus: dict[str, Any]) -> None:
    reasons = [x.reason for x in corpus["rbi_kyc_directions_2025"][1].exclusions]
    assert reasons.count("non_latin_script") == 1 and reasons.count("section_heading") == 30


# --- RBI FAQ ------------------------------------------------------------------------


def test_faq_one_chunk_per_question(corpus: dict[str, Any]) -> None:
    chunks = metas(corpus, "rbi_kyc_faqs")
    assert [m.paragraph for m in chunks] == [f"Q{n}" for n in range(1, 38)]
    assert all(
        m.content_kind is ContentKind.QA and m.chunk_role is ChunkRole.SINGLE for m in chunks
    )
    q = {m.paragraph: m for m in chunks}
    assert q["Q1"].section == "Q1: What is Know Your Customer (KYC)?"
    assert (q["Q17"].page_number, q["Q17"].page_end) == (4, 5)
    assert q["Q22"].page_number == 6 and "ten years for low-risk" in q["Q22"].content
    (note,) = q["Q36"].footnotes
    assert note.page_number == 9 and "Inoperative Accounts" in note.text


# --- HDFC policy --------------------------------------------------------------------


def test_policy_rows_keep_feature_document_pairs(corpus: dict[str, Any]) -> None:
    rows = find(corpus, "hdfc_kyc_aml_faq", content_kind=ContentKind.TABLE_ROW)
    assert [r.paragraph for r in rows] == ["Row 1.0", "Row 2.0", "Row 3.0", "Row 4.0"]
    assert [r.entity_types for r in rows] == [
        [EntityType.INDIVIDUAL], [EntityType.COMPANY], [EntityType.PARTNERSHIP], [EntityType.TRUST],
    ]  # fmt: skip
    one = rows[0].content
    assert "1.1 Legal name and any other names used - documents: (i) Passport (ii) PAN Card" in one
    assert "1.2 Correct permanent address - documents: (i) Telephone bill" in one
    assert "2.4 Telephone / Fax number" in rows[1].content
    assert rows[1].content.index("Documents: (i) Certificate of incorporation") > 0
    assert all("not an exhaustive list and is only indicative" in r.content for r in rows)
    assert all(r.evidence_eligibility is EvidenceEligibility.SUPPORTING for r in rows)  # dated
    qa = find(corpus, "hdfc_kyc_aml_faq", content_kind=ContentKind.QA)
    assert "What is KYC Policy?" in [m.section for m in qa]


# --- forms --------------------------------------------------------------------------


def test_nri_rekyc_document_list_chunks(corpus: dict[str, Any]) -> None:
    p3 = find(corpus, "hdfc_nri_rekyc", page_number=3)
    by_section = {m.section: m for m in p3}
    for section, phrase in (
        ("Proof of Identity (A)", "Photocopy of valid Indian Passport"),
        ("Proof of NRI/ PIO Status (B)", "OCI (Overseas Citizen if India) card"),
        ("Proof of Address (C)", "Overseas Driving License"),
        ("Additional Document (D)", "Photocopy of PAN Card"),
    ):
        assert phrase in "\n".join(m.content for m in p3 if m.section == section), section
        assert by_section[section].extraction_method is ExtractionMethod.OCR
        assert (by_section[section].ocr_confidence or 0) >= 0.9
    assert all(m.page_status is PageStatus.OK and m.region is None for m in p3)


def test_fold_out_regions_and_uncertainty(corpus: dict[str, Any]) -> None:
    result = corpus["hdfc_non_individual_account_opening"][1]
    for rec in result.chunks:
        m = rec.metadata
        assert m.region in ("left", "right")
        assert m.page_status is PageStatus.OCR_LOW_CONFIDENCE
        assert m.ocr_confidence is not None and m.printed_page is None
    excluded = [x for rec in result.chunks for x in rec.extras.get("excluded_lines", [])]
    assert excluded and all(x["region"] in ("left", "right") for x in excluded)
    assert not result.exclusions  # low-confidence lines stay with their chunks
    regions = {
        (m.page_number, m.region) for m in metas(corpus, "hdfc_non_individual_account_opening")
    }
    assert regions == {(1, "left"), (1, "right"), (2, "left"), (2, "right")}


CLOSE_RELATIVE = "CLOSE RELATIVE DECLARATION (To be filled by the applicant if he/she does not have any address proof)"  # noqa: E501
NRE = "hdfc_nri_account_opening"


@pytest.mark.parametrize(
    ("key", "phrase", "section"),
    [
        (NRE, "does not hold a documentary address", f"S) {CLOSE_RELATIVE}"),
        (NRE, "Certificate of Loss of Nationality", "X) EXTENDED KYC"),
        (NRE, "IQAMA", "U) DECLARATION OF SAUDI ARABIA RESIDENTS ON VISIT TO INDIA"),
        (NRE, "Politically exposed persons are individuals", "E) PEP DECLARATION"),
        ("hdfc_resident_to_nro_conversion", "does not hold a documentary address",
         f"P) {CLOSE_RELATIVE}"),
        ("hdfc_resident_rekyc_dormant", "branch visit is mandatory",
         "DORMANT ACTIVATION (for dormant account activation branch visit is mandatory)"),
        ("hdfc_netbanking_corporate_application", "RESOLVED THAT",
         None),
    ],
)  # fmt: skip
def test_form_text_under_its_own_heading(
    corpus: dict[str, Any], key: str, phrase: str, section: str | None
) -> None:
    hits = [m for m in metas(corpus, key) if phrase.lower() in m.content.lower()]
    assert hits, phrase
    if section is not None:
        assert hits[0].section == section
    else:
        assert hits[0].section and hits[0].section.startswith(
            "CERTIFIED TRUE COPY OF THE RESOLUTION"
        )
    assert all(m.content_kind in (ContentKind.FORM_TEXT, ContentKind.FORM_FIELDS) for m in hits)


def test_form_eligibility(corpus: dict[str, Any]) -> None:
    for m in metas(corpus, "hdfc_netbanking_corporate_application"):
        assert m.evidence_eligibility is EvidenceEligibility.CONTEXT_ONLY
    for key in ("hdfc_nri_account_opening", "hdfc_nri_rekyc", "hdfc_resident_rekyc_dormant"):
        assert all(
            m.evidence_eligibility is EvidenceEligibility.SUPPORTING for m in metas(corpus, key)
        )


# --- parent/child integrity (all 27 parents) -------------------------------------


def test_every_parent_is_reassembled_by_its_children(corpus: dict[str, Any]) -> None:
    def squash(text: str) -> str:
        return "".join(text.split())

    parents = 0
    for key, (_, result, _) in corpus.items():
        for rec in result.chunks:
            m = rec.metadata
            if m.chunk_role is not ChunkRole.PARENT:
                continue
            parents += 1
            kids = result.children_of(rec)
            assert len(kids) >= 2, (key, m.paragraph)
            assert all(result.parent_of(k) is rec for k in kids)
            km = [k.metadata for k in kids]
            # Complete paragraph: children reassemble the parent's text and raw text.
            assert squash(m.content) == "".join(squash(k.content) for k in km), m.paragraph
            assert rec.extras["raw_text"] == "\n".join(k.extras["raw_text"] for k in kids)
            # Traceability: same document, section, label; pages and footnotes cover all.
            assert {(k.document_id, k.section, k.paragraph) for k in km} == {
                (m.document_id, m.section, m.paragraph)
            }
            assert m.page_number == km[0].page_number
            assert (m.page_end or m.page_number) == max(k.page_end or k.page_number for k in km)
            child_notes = {(f.page_number, f.marker) for k in km for f in k.footnotes}
            assert child_notes == {(f.page_number, f.marker) for f in m.footnotes}
            # Children never start with a qualifier; later children carry context.
            assert not any(QUALIFIER.match(k.content) for k in km), m.paragraph
            assert km[0].context is None and all(k.context for k in km[1:]), m.paragraph
            assert not m.embedding_eligible and all(k.embedding_eligible for k in km)
    assert parents == 27


def test_no_child_starts_mid_sentence(corpus: dict[str, Any]) -> None:
    for key in corpus:
        for m in metas(corpus, key):
            if m.chunk_role is ChunkRole.CHILD:
                assert not m.content[:1].islower(), (key, m.paragraph, m.content[:40])


# --- OCR evidence policy outcomes ----------------------------------------------------


def test_ocr_reliability_by_document_and_page(corpus: dict[str, Any]) -> None:
    for key in corpus:
        if key in ("hdfc_nri_rekyc", "hdfc_non_individual_account_opening"):
            continue
        assert all(m.text_reliability is TextReliability.NATIVE for m in metas(corpus, key))
    # NRI re-KYC: p3 (the document list) and p2 are reliable; p1 (field labels, ~0.78) is not.
    by_page: dict[int, set[TextReliability]] = {}
    for m in metas(corpus, "hdfc_nri_rekyc"):
        by_page.setdefault(m.page_number, set()).add(m.text_reliability)
    assert by_page == {
        1: {TextReliability.OCR_UNCERTAIN},
        2: {TextReliability.OCR_RELIABLE},
        3: {TextReliability.OCR_RELIABLE},
    }
    # Fold-out: every chunk uncertain (page flagged), kept and embedded, never MANDATORY.
    for m in metas(corpus, "hdfc_non_individual_account_opening"):
        assert m.text_reliability is TextReliability.OCR_UNCERTAIN
        assert m.embedding_eligible and not m.can_support_mandatory


def test_only_native_rbi_directions_can_support_mandatory(corpus: dict[str, Any]) -> None:
    for key in corpus:
        support = {m.can_support_mandatory for m in metas(corpus, key)}
        assert support == ({True} if key == "rbi_kyc_directions_2025" else {False}), key
