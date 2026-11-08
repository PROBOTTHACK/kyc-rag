"""Stage B on the real corpus (skipped without the gitignored PDFs or Tesseract).

Uses the configured OCR cache (data/processed/ocr_cache): the first run OCRs the five
scanned pages (~40 s), later runs read the cache. Expected text and statuses were
checked against the rendered pages (data/processed/corpus_report.md).
"""

import pytest

from app.config import REPO_ROOT, get_settings
from app.ingestion.manifest import load_manifest
from app.ingestion.ocr import OcrSetupError, TesseractSetup, tesseract_setup
from app.ingestion.pages import OcrCache, ProcessedPage, process_document
from app.schemas.taxonomy import ExtractionMethod, PageStatus

CORPUS = REPO_ROOT / "data" / "raw_pdfs"
pytestmark = pytest.mark.skipif(
    not (CORPUS / "forms" / "hdfc_nri_rekyc.pdf").exists(), reason="source PDFs not present"
)


@pytest.fixture(scope="module")
def setup() -> TesseractSetup:
    try:
        return tesseract_setup(get_settings())
    except OcrSetupError as exc:
        pytest.skip(f"Tesseract not available: {exc}")


@pytest.fixture(scope="module")
def corpus(setup: TesseractSetup) -> dict[str, list[ProcessedPage]]:
    settings = get_settings()
    cache = OcrCache(settings.OCR_CACHE_DIR)
    out: dict[str, list[ProcessedPage]] = {}
    for entry in load_manifest(settings.CORPUS_MANIFEST):
        _, pages = process_document(
            CORPUS / entry.file, pdf_sha256=entry.sha256, setup=setup, cache=cache
        )
        out[entry.document_key] = pages
    return out


def text(page: ProcessedPage) -> str:
    return "\n".join(r.text.text() for r in page.ocr_regions)


def test_exactly_the_five_scanned_pages_are_ocred(corpus: dict[str, list[ProcessedPage]]) -> None:
    triggered = sorted(
        (key, p.page_number) for key, pages in corpus.items() for p in pages if p.ocr_triggered
    )
    assert triggered == [
        ("hdfc_non_individual_account_opening", 1),
        ("hdfc_non_individual_account_opening", 2),
        ("hdfc_nri_rekyc", 1),
        ("hdfc_nri_rekyc", 2),
        ("hdfc_nri_rekyc", 3),
    ]
    for key, pages in corpus.items():
        for p in pages:
            if p.ocr_triggered:  # processed or explicitly flagged; never silently empty
                assert p.status in (PageStatus.OK, PageStatus.OCR_LOW_CONFIDENCE), (key, p.error)
                assert p.extraction_method is ExtractionMethod.OCR
                assert p.ocr_confidence is not None


def test_native_pages_keep_their_text_layer(corpus: dict[str, list[ProcessedPage]]) -> None:
    total = 0
    for pages in corpus.values():
        assert [p.page_number for p in pages] == list(range(1, len(pages) + 1))
        total += len(pages)
        for p in pages:
            if not p.ocr_triggered:
                assert p.status is PageStatus.OK
                assert p.extraction_method is ExtractionMethod.TEXT
                assert p.ocr_regions == []
    assert total == 135
    assert not [p for pages in corpus.values() for p in pages if p.status is PageStatus.FAILED]


def test_nri_rekyc_document_list(corpus: dict[str, list[ProcessedPage]]) -> None:
    p1, p2, p3 = corpus["hdfc_nri_rekyc"]
    assert all(p.status is PageStatus.OK for p in (p1, p2, p3))
    assert p3.ocr_confidence is not None and p3.ocr_confidence >= 0.90
    t3 = text(p3)
    for expected in (
        "Comprehensive list of acceptable documents for proof of identity & proof of address",
        "Mandatory document",
        "Copy of Permanent Account Number (PAN) / Form 60 (in absence of PAN)",
        "Proof of Identity (A)",
        "Photocopy of valid Indian Passport",
        "Proof of NRI/ PIO Status (B)",
        "Proof of Address (C)",
        "Overseas Driving License",
        "Indian Address (one of the following)",
        "Voter's ID Card issued by the Election Commission of India",
        "Additional Document (D)",
        "Photocopy of PAN Card",
    ):
        assert expected in t3, expected
    assert "You have to attach self attested Indian passport, visa" in text(p2)
    assert "93316/05.08.2020" in text(p1)  # print code from the manifest, confirmed
    assert [r.printed_page for p in (p1, p2, p3) for r in p.ocr_regions] == [1, 2, 3]


def test_fold_out_regions_and_low_confidence(corpus: dict[str, list[ProcessedPage]]) -> None:
    for page in corpus["hdfc_non_individual_account_opening"]:
        assert [r.label for r in page.ocr_regions] == ["left", "right"]
        assert page.status is PageStatus.OCR_LOW_CONFIDENCE
        assert page.ocr_confidence is not None and 0.5 <= page.ocr_confidence < 0.7
        # Footers are garbled by OCR; printed page numbers are not guessed.
        assert all(r.printed_page in (None, 4) for r in page.ocr_regions)
    p2_left = corpus["hdfc_non_individual_account_opening"][1].ocr_regions[0]
    assert "HUF" in p2_left.text.text()  # the HUF declaration survives, if noisily
