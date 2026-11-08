"""Stage B: OCR trigger, page decisions, TSV parsing, failures, cache, fold-out regions.

Synthetic PDFs are built in-test. OCR is faked by monkeypatching `pages.run_ocr`,
except in the real-Tesseract test (skipped when Tesseract is not installed) and the
process-failure tests, which run real subprocesses.
"""

import sys
from pathlib import Path

import pymupdf
import pytest

from app.config import Settings
from app.ingestion import pages as pages_mod
from app.ingestion.ocr import (
    OcrLine,
    OcrRunError,
    OcrSetupError,
    OcrText,
    TesseractSetup,
    parse_tsv,
    run_ocr,
    tesseract_setup,
)
from app.ingestion.pages import OcrCache, process_document
from app.ingestion.pdf import PageMetrics
from app.ingestion.quality import is_fold_out, native_decision, needs_ocr, ocr_decision
from app.ingestion.structure import extract_document
from app.schemas.taxonomy import ExtractionMethod, PageStatus

SHA = "c" * 64
FAKE_SETUP = TesseractSetup(
    cmd=Path("no-such-tesseract"), tessdata=Path("."), version="fake 1.0", languages=("eng",)
)
TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def tsv(*rows: str) -> str:
    return "\n".join([TSV_HEADER, *rows]) + "\n"


def fake_text(*lines: tuple[str, float]) -> OcrText:
    built = tuple(
        OcrLine(text=t, confidence=c, word_count=len(t.split()),
                bbox=(0.0, 10.0 * i, 100.0, 10.0 * i + 8), block=1, paragraph=1, line=i + 1)
        for i, (t, c) in enumerate(lines)
    )  # fmt: skip
    confs = tuple(c for t, c in lines for _ in t.split())
    return OcrText(lines=built, word_confidences=confs)


def metrics(chars: int, coverage: float) -> PageMetrics:
    return PageMetrics(char_count=chars, alpha_ratio=0.8, image_coverage=coverage, n_images=1)


# --- trigger and decisions ---------------------------------------------------


@pytest.mark.parametrize(
    ("chars", "coverage", "expected"),
    [
        (0, 1.0, True),  # scanned page
        (49, 0.81, True),  # boundary: just under 50 chars, just over 80%
        (50, 1.0, False),  # enough native text: never OCR
        (0, 0.8, False),  # coverage must be > 80%
        (0, 0.0, False),  # blank page without images
        (150, 0.003, False),  # short native page with a small logo (RBI p55/p74)
    ],
)
def test_ocr_trigger(chars: int, coverage: float, expected: bool) -> None:
    assert needs_ocr(metrics(chars, coverage)) is expected


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(1432, 894, True), (596, 842, False), (842, 596, False), (792, 612, False)],
)
def test_fold_out_detection(width: float, height: float, expected: bool) -> None:
    # corpus fold-out sheet; A4 portrait; A4 landscape (1.41); Letter landscape (1.29)
    assert is_fold_out(width, height) is expected


def test_native_decision() -> None:
    assert native_decision(metrics(120, 0.0)).status is PageStatus.OK
    assert native_decision(metrics(120, 0.0)).extraction_method is ExtractionMethod.TEXT
    blank = native_decision(metrics(0, 0.0))
    assert blank.status is PageStatus.LOW_QUALITY and blank.error


@pytest.mark.parametrize(
    ("native", "ocr_chars", "conf", "errors", "status", "method"),
    [
        (0, 500, 93.0, [], PageStatus.OK, ExtractionMethod.OCR),
        (0, 500, 70.0, [], PageStatus.OK, ExtractionMethod.OCR),
        (0, 500, 69.9, [], PageStatus.OCR_LOW_CONFIDENCE, ExtractionMethod.OCR),
        (0, 0, 0.0, [], PageStatus.FAILED, None),
        (0, 500, 90.0, ["OCR failed for region left: boom"], PageStatus.FAILED, None),
        (30, 0, 0.0, [], PageStatus.LOW_QUALITY, ExtractionMethod.TEXT),
        (30, 25, 95.0, [], PageStatus.LOW_QUALITY, ExtractionMethod.TEXT),  # OCR no better
        (30, 300, 95.0, [], PageStatus.OK, ExtractionMethod.OCR),
    ],
)
def test_ocr_decision(
    native: int,
    ocr_chars: int,
    conf: float,
    errors: list[str],
    status: PageStatus,
    method: ExtractionMethod | None,
) -> None:
    d = ocr_decision(metrics(native, 1.0), ocr_chars=ocr_chars, ocr_confidence=conf, errors=errors)
    assert (d.status, d.extraction_method) == (status, method)
    if status in (PageStatus.FAILED, PageStatus.LOW_QUALITY):
        assert d.error


# --- TSV parsing and real subprocess failures ---------------------------------


def test_parse_tsv_groups_words_into_lines() -> None:
    out = parse_tsv(
        tsv(
            "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
            "5\t1\t1\t1\t1\t1\t10\t20\t40\t10\t96.5\tProof",
            "5\t1\t1\t1\t1\t2\t55\t20\t20\t10\t93.5\tof",
            "5\t1\t1\t1\t1\t3\t80\t21\t50\t10\t90\tAddress",
            "5\t1\t1\t1\t2\t1\t10\t40\t30\t10\t20\tzq#",
            "5\t1\t1\t1\t2\t2\t45\t40\t30\t10\t-1\t ",
        )
    )
    assert [ln.text for ln in out.lines] == ["Proof of Address", "zq#"]
    assert out.lines[0].bbox == (10.0, 20.0, 130.0, 31.0)
    assert out.lines[0].confidence == pytest.approx(93.33, abs=0.01)
    assert out.lines[1].low_confidence and not out.lines[0].low_confidence
    assert out.word_count == 4  # blank word skipped
    assert out.mean_confidence == pytest.approx((96.5 + 93.5 + 90 + 20) / 4)
    assert out.text() == "Proof of Address\nzq#"


def test_parse_tsv_rejects_non_tsv() -> None:
    with pytest.raises(OcrRunError, match="not TSV"):
        parse_tsv("Error: something went wrong\n")


def test_run_ocr_missing_executable() -> None:
    with pytest.raises(OcrRunError, match="cannot run tesseract"):
        run_ocr(FAKE_SETUP, b"png", 300)


def test_run_ocr_nonzero_exit() -> None:
    # Python as a stand-in: it exits with status 2 on tesseract's arguments.
    impostor = TesseractSetup(cmd=Path(sys.executable), tessdata=Path("."), version="x",
                              languages=("eng",))  # fmt: skip
    with pytest.raises(OcrRunError, match="exited with"):
        run_ocr(impostor, b"png", 300)


# --- cache -------------------------------------------------------------------


def test_cache_round_trip_and_keying(tmp_path: Path) -> None:
    cache = OcrCache(tmp_path)
    text = fake_text(("Proof of Address (C)", 93.0), ("noise", 12.0))
    key = OcrCache.key(SHA, 3, "full", (0, 0, 595, 842), "v5.4")
    assert cache.get(key) is None
    cache.put(key, text)
    assert cache.get(key) == text
    # Any change of input identity gives a different key.
    for other in (
        OcrCache.key("d" * 64, 3, "full", (0, 0, 595, 842), "v5.4"),
        OcrCache.key(SHA, 2, "full", (0, 0, 595, 842), "v5.4"),
        OcrCache.key(SHA, 3, "left", (0, 0, 595, 842), "v5.4"),
        OcrCache.key(SHA, 3, "full", (0, 0, 595, 842), "v5.5"),
    ):
        assert other != key


def test_cache_disabled_and_corrupt_entries(tmp_path: Path) -> None:
    off = OcrCache(None)
    off.put("k" * 64, fake_text(("x", 90.0)))
    assert off.get("k" * 64) is None
    cache = OcrCache(tmp_path)
    key = "ab" + "0" * 62
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / f"{key}.json").write_text("{not json", encoding="utf-8")
    assert cache.get(key) is None  # ignored, recomputed by the caller


# --- process_document --------------------------------------------------------


def scanned_pdf(tmp_path: Path, size: tuple[float, float] = (595, 842), text: str = "SCAN") -> Path:
    src = pymupdf.open()
    sp = src.new_page(width=size[0], height=size[1])
    sp.insert_text((40, 120), text, fontsize=28)
    pix = sp.get_pixmap(dpi=120)
    doc = pymupdf.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_image(page.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    return path


def mixed_pdf(tmp_path: Path) -> Path:
    """p1 native text, p2 scanned image, p3 native text."""
    scan = pymupdf.open(scanned_pdf(tmp_path))
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Native page one with enough text to need no OCR.")
    doc.insert_pdf(scan)
    doc.new_page().insert_text((72, 100), "Native page three with enough text to need no OCR.")
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    return path


class FakeOcr:
    def __init__(self, text: OcrText | None = None, fail: bool = False) -> None:
        self.calls = 0
        self.text = text or fake_text(
            ("Proof of Address", 92.0), ("Overseas Driving License", 88.0)
        )
        self.fail = fail

    def __call__(self, setup: TesseractSetup, png: bytes, dpi: int) -> OcrText:
        self.calls += 1
        assert png.startswith(b"\x89PNG") and dpi == 300
        if self.fail:
            raise OcrRunError("tesseract exited with 1: simulated")
        return self.text


def test_only_scanned_pages_are_ocred_and_page_numbers_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOcr()
    monkeypatch.setattr(pages_mod, "run_ocr", fake)
    path = mixed_pdf(tmp_path)
    extracted, pages = process_document(
        path, pdf_sha256=SHA, setup=FAKE_SETUP, cache=OcrCache(tmp_path / "c")
    )
    assert [p.page_number for p in pages] == [1, 2, 3]
    assert fake.calls == 1
    assert [p.ocr_triggered for p in pages] == [False, True, False]
    assert [p.extraction_method for p in pages] == [
        ExtractionMethod.TEXT, ExtractionMethod.OCR, ExtractionMethod.TEXT,
    ]  # fmt: skip
    assert all(p.status is PageStatus.OK for p in pages)
    scanned = pages[1]
    assert scanned.ocr_regions[0].label == "full"
    assert scanned.ocr_confidence == pytest.approx(0.90)  # 3 words at 92, 3 at 88
    # Stage A output untouched, OCR kept separately.
    fresh = extract_document(path)
    for p, sp in zip(pages, fresh.pages, strict=True):
        assert [ln.text for ln in p.structured.body] == [ln.text for ln in sp.body]
    assert scanned.structured.body == []
    meta = scanned.metadata()
    assert meta["ocr"]["regions"][0]["words"] == 6 and meta["ocr"]["dpi"] == 300


def test_repeat_runs_use_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOcr()
    monkeypatch.setattr(pages_mod, "run_ocr", fake)
    path, cache = scanned_pdf(tmp_path), OcrCache(tmp_path / "c")
    _, first = process_document(path, pdf_sha256=SHA, setup=FAKE_SETUP, cache=cache)
    _, second = process_document(path, pdf_sha256=SHA, setup=FAKE_SETUP, cache=cache)
    assert fake.calls == 1
    assert second[0].ocr_regions[0].from_cache and not first[0].ocr_regions[0].from_cache
    assert second[0].ocr_regions[0].text == first[0].ocr_regions[0].text
    assert second[0].status is first[0].status


def test_ocr_failure_keeps_page_with_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOcr(fail=True)
    monkeypatch.setattr(pages_mod, "run_ocr", fake)
    cache = OcrCache(tmp_path / "c")
    _, pages = process_document(
        scanned_pdf(tmp_path), pdf_sha256=SHA, setup=FAKE_SETUP, cache=cache
    )
    (page,) = pages
    assert page.page_number == 1 and page.status is PageStatus.FAILED
    assert page.extraction_method is None
    assert page.error and "simulated" in page.error
    assert not (tmp_path / "c").exists()  # failures are never cached


def test_missing_tesseract_fails_page_explicitly(tmp_path: Path) -> None:
    _, pages = process_document(
        scanned_pdf(tmp_path), pdf_sha256=SHA, setup=None,
        setup_error="Tesseract not found", cache=OcrCache(None),
    )  # fmt: skip
    (page,) = pages
    assert page.status is PageStatus.FAILED and page.ocr_triggered
    assert page.error == "OCR needed but Tesseract is unavailable: Tesseract not found"


def test_empty_ocr_result_fails_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pages_mod, "run_ocr", FakeOcr(OcrText(lines=(), word_confidences=())))
    _, pages = process_document(
        scanned_pdf(tmp_path), pdf_sha256=SHA, setup=FAKE_SETUP, cache=OcrCache(None)
    )
    assert pages[0].status is PageStatus.FAILED and pages[0].error == "OCR produced no text"


def test_low_confidence_ocr_is_flagged_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    noisy = fake_text(("fledde marked MANDATORY", 55.0), ("Aeoount", 30.0))
    monkeypatch.setattr(pages_mod, "run_ocr", FakeOcr(noisy))
    _, pages = process_document(
        scanned_pdf(tmp_path), pdf_sha256=SHA, setup=FAKE_SETUP, cache=OcrCache(None)
    )
    (page,) = pages
    assert page.status is PageStatus.OCR_LOW_CONFIDENCE
    assert page.extraction_method is ExtractionMethod.OCR
    assert page.ocr_regions[0].low_confidence_lines == 1
    assert page.metadata()["ocr"]["regions"][0]["low_confidence_lines"] == 1


def test_fold_out_page_is_ocred_as_two_regions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOcr(fake_text(("Page 4 of 4", 91.0), ("Nomination form", 90.0)))
    monkeypatch.setattr(pages_mod, "run_ocr", fake)
    _, pages = process_document(
        scanned_pdf(tmp_path, size=(1432, 894)), pdf_sha256=SHA, setup=FAKE_SETUP,
        cache=OcrCache(None),
    )  # fmt: skip
    (page,) = pages
    assert fake.calls == 2
    assert [r.label for r in page.ocr_regions] == ["left", "right"]
    assert page.ocr_regions[0].clip == (0.0, 0.0, 716.0, 894.0)
    assert page.ocr_regions[1].clip == (716.0, 0.0, 1432.0, 894.0)
    assert page.ocr_regions[0].printed_page == 4


def test_a4_landscape_scan_is_not_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOcr()
    monkeypatch.setattr(pages_mod, "run_ocr", fake)
    _, pages = process_document(
        scanned_pdf(tmp_path, size=(842, 596)), pdf_sha256=SHA, setup=FAKE_SETUP,
        cache=OcrCache(None),
    )  # fmt: skip
    assert fake.calls == 1 and [r.label for r in pages[0].ocr_regions] == ["full"]


def test_short_native_text_is_not_replaced_by_worse_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Image-covered page with a short native label: OCR finds less, native text wins.
    path = scanned_pdf(tmp_path)
    doc = pymupdf.open(path)
    doc[0].insert_text((40, 800), "Form J3520 rev 7", fontsize=8)
    native = tmp_path / "native_label.pdf"
    doc.save(native)
    monkeypatch.setattr(pages_mod, "run_ocr", FakeOcr(fake_text(("J3520", 80.0))))
    _, pages = process_document(native, pdf_sha256=SHA, setup=FAKE_SETUP, cache=OcrCache(None))
    (page,) = pages
    assert page.ocr_triggered
    assert page.status is PageStatus.LOW_QUALITY
    assert page.extraction_method is ExtractionMethod.TEXT
    assert [ln.text.strip() for ln in page.structured.body] == ["Form J3520 rev 7"]


# --- real Tesseract on a synthetic scan ---------------------------------------


@pytest.fixture
def real_setup() -> TesseractSetup:
    try:
        return tesseract_setup(Settings(_env_file=None))  # type: ignore[call-arg]
    except OcrSetupError as exc:
        pytest.skip(f"Tesseract is not installed on this machine: {exc}")


def test_real_tesseract_on_synthetic_scan(tmp_path: Path, real_setup: TesseractSetup) -> None:
    path = scanned_pdf(tmp_path, text="PROOF OF ADDRESS")
    cache = OcrCache(tmp_path / "c")
    _, first = process_document(path, pdf_sha256=SHA, setup=real_setup, cache=cache)
    _, second = process_document(path, pdf_sha256=SHA, setup=real_setup, cache=cache)
    page = first[0]
    assert page.status is PageStatus.OK and page.extraction_method is ExtractionMethod.OCR
    assert "PROOF OF ADDRESS" in page.ocr_regions[0].text.text().upper()
    assert page.ocr_confidence is not None and page.ocr_confidence > 0.7
    assert second[0].ocr_regions[0].from_cache
    assert second[0].ocr_regions[0].text == page.ocr_regions[0].text
