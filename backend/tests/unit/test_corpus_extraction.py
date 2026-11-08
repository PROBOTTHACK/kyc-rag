"""Stage A golden checks on the real corpus (skipped when the gitignored PDFs are absent).

Expected values come from data/processed/corpus_report.md and the PDFs themselves.
"""

from pathlib import Path

import pytest

from app.config import REPO_ROOT
from app.ingestion.manifest import load_manifest
from app.ingestion.structure import ExtractedDocument, SectionIndex, extract_document

CORPUS = REPO_ROOT / "data" / "raw_pdfs"
pytestmark = pytest.mark.skipif(
    not (CORPUS / "regulatory" / "169MD.pdf").exists(), reason="source PDFs not present"
)


@pytest.fixture(scope="module")
def directions() -> ExtractedDocument:
    return extract_document(CORPUS / "regulatory" / "169MD.pdf")


def test_all_manifest_pages_are_recorded() -> None:
    total = 0
    for entry in load_manifest(CORPUS / "manifest.yaml"):
        doc = extract_document(CORPUS / entry.file)
        assert [p.page_number for p in doc.pages] == list(range(1, doc.page_count + 1))
        total += doc.page_count
    assert total == 135


def test_directions_footnotes(directions: ExtractedDocument) -> None:
    notes = {(f.page_number, f.marker): f.text for p in directions.pages for f in p.footnotes}
    assert set(notes) == {(6, "1"), (18, "2"), (62, "3")}
    assert "September 18, 2026" in notes[(6, "1")]
    assert "October 1, 2026" in notes[(18, "2")]
    assert "December 29, 2025" in notes[(62, "3")]
    refs = {
        (p.page_number, line.footnote_refs)
        for p in directions.pages
        for line in p.body
        if line.footnote_refs
    }
    assert refs == {(6, ("1",)), (18, ("2",)), (62, ("3",))}


def test_directions_body_is_clean(directions: ExtractedDocument) -> None:
    body = "\n".join(line.text for p in directions.pages for line in p.body)
    assert "1and Foreign" not in body and "[3Explanation" not in body
    assert "Inserted with effect" not in body  # footnote text left the body
    assert "......" not in body  # contents leaders removed
    # Running page numbers: printed label == physical page everywhere except the cover.
    assert all(p.page_label == str(p.page_number) for p in directions.pages[1:])
    assert directions.pages[0].page_label is None
    assert [p.page_number for p in directions.pages if not p.body] == [2]  # contents-only


def test_directions_sections_follow_bookmarks(directions: ExtractedDocument) -> None:
    idx = SectionIndex(directions.bookmarks)
    page37 = directions.pages[36]
    para33 = next(ln for ln in page37.body if ln.text.startswith("33. "))
    para32 = next(ln for ln in page37.body if ln.text.startswith("32. "))
    assert idx.path_at(37, para33.y0)[-1] == "C. CDD Measures for Legal Entities"
    assert idx.path_at(37, para32.y0)[-1] == "B. CDD Measures for Sole Proprietary firms"
    page18 = directions.pages[17]
    para14 = next(ln for ln in page18.body if ln.text.startswith("14. "))
    assert idx.path_at(18, para14.y0) == ("Chapter II – General", "B. Other General Guidelines:")


def test_faq_footnote_and_page_numbers() -> None:
    faq = extract_document(CORPUS / "regulatory" / "rbi_kyc_faqs_2025-06-09.pdf")
    notes = [(p.page_number, f.marker) for p in faq.pages for f in p.footnotes]
    assert notes == [(9, "1")]
    assert sum(p.content.removed.get("page_number", 0) for p in faq.pages) == 9


def test_scanned_forms_have_records_but_no_text_layer() -> None:
    for name, pages in (("hdfc_nri_rekyc.pdf", 3), ("hdfc_non_individual_account_opening.pdf", 2)):
        doc = extract_document(CORPUS / "forms" / name)
        assert len(doc.pages) == pages
        for p in doc.pages:
            assert p.body == []
            assert p.content.metrics.char_count == 0
            assert p.content.metrics.image_coverage == pytest.approx(1.0)


def test_form_codes_are_not_mistaken_for_page_numbers() -> None:
    doc = extract_document(CORPUS / "forms" / "hdfc_nri_nre_nro_account_opening.pdf")
    assert all(p.page_label is None for p in doc.pages)
    assert any(line.text.strip() == "055" for line in doc.pages[0].body)


def test_extraction_is_deterministic() -> None:
    path: Path = CORPUS / "policy" / "hdfc_kyc_norms_aml.pdf"
    a, b = extract_document(path), extract_document(path)
    assert [[ln.text for ln in p.body] for p in a.pages] == [
        [ln.text for ln in p.body] for p in b.pages
    ]
