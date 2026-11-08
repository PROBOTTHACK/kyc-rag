"""Stage A on synthetic PDFs built in-test: validation, metrics, footnotes, furniture, sections."""

from pathlib import Path

import pymupdf
import pytest

from app.ingestion.pdf import PdfValidationError, open_pdf, read_page, text_metrics
from app.ingestion.structure import (
    Bookmark,
    SectionIndex,
    extract_document,
    structure_page,
)


def save(doc: pymupdf.Document, path: Path, **kw: object) -> Path:
    doc.save(path, **kw)
    doc.close()
    return path


def html(page: pymupdf.Page, y: float, markup: str, size: int = 12, h: float = 30) -> None:
    page.insert_htmlbox(
        pymupdf.Rect(72, y, 520, y + h), f'<p style="font-size:{size}px">{markup}</p>'
    )


# --- validation --------------------------------------------------------------


def test_missing_and_non_pdf_files_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(PdfValidationError, match="not a file"):
        open_pdf(tmp_path / "absent.pdf")
    bogus = tmp_path / "bogus.pdf"
    bogus.write_bytes(b"hello, not a pdf")
    with pytest.raises(PdfValidationError, match="missing %PDF header"):
        open_pdf(bogus)


def test_corrupt_pdf_is_rejected(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    with pytest.raises(PdfValidationError):
        open_pdf(corrupt)


def test_encrypted_pdf_is_rejected(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    path = save(
        doc, tmp_path / "enc.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o"
    )
    with pytest.raises(PdfValidationError, match="encrypted"):
        open_pdf(path)


# --- pages and metrics -------------------------------------------------------


def test_text_metrics() -> None:
    assert text_metrics("") == (0, 0.0)
    assert text_metrics(" \n ") == (0, 0.0)
    assert text_metrics("ab 12") == (4, 0.5)


def test_every_page_gets_a_record_with_physical_numbers(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "First page text.")
    doc.new_page()  # blank page must still be recorded
    doc.new_page().insert_text((72, 100), "Third page text.")
    extracted = extract_document(save(doc, tmp_path / "three.pdf"))
    assert extracted.page_count == 3
    assert [p.page_number for p in extracted.pages] == [1, 2, 3]
    assert extracted.pages[1].body == []
    assert extracted.pages[1].content.metrics.char_count == 0


def test_text_page_metrics(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Know Your Customer 2025")
    page = extract_document(save(doc, tmp_path / "t.pdf")).pages[0].content
    assert page.metrics.char_count == len("KnowYourCustomer2025")
    assert page.metrics.alpha_ratio == pytest.approx(16 / 20)
    assert page.metrics.image_coverage == 0.0 and page.metrics.n_images == 0


def test_image_only_page_metrics(tmp_path: Path) -> None:
    src = pymupdf.open()
    sp = src.new_page(width=300, height=200)
    sp.insert_text((20, 100), "SCANNED TEXT", fontsize=20)
    pix = sp.get_pixmap(dpi=100)
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_image(page.rect, pixmap=pix)
    content = extract_document(save(doc, tmp_path / "scan.pdf")).pages[0].content
    assert content.metrics.char_count == 0
    assert content.metrics.image_coverage == pytest.approx(1.0)
    assert content.metrics.n_images == 1
    assert content.lines == []


# --- footnotes ---------------------------------------------------------------


def footnote_page(tmp_path: Path, body_marker: bool = True) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()  # 595 x 842
    marker = "<sup>1</sup>" if body_marker else ""
    html(page, 100, f"Regulations, 2016{marker}and Foreign Portfolio Investors")
    html(page, 140, "The bank may alternatively obtain the original certified copy.")
    html(page, 720, "<sup>1</sup>Inserted with effect from September 18, 2026 vide", size=9, h=16)
    html(page, 734, "Amendment Directions, 2026.", size=9, h=16)
    return save(doc, tmp_path / "fn.pdf")


def test_footnote_is_separated_and_marker_removed(tmp_path: Path) -> None:
    page = extract_document(footnote_page(tmp_path)).pages[0]
    assert len(page.footnotes) == 1
    note = page.footnotes[0]
    assert note.marker == "1" and note.page_number == 1
    assert note.text.startswith("Inserted with")
    assert "Amendment Directions, 2026." in note.text
    body = "\n".join(line.text for line in page.body)
    assert "2016and" in body  # marker gone; Stage C joins words
    assert "Inserted" not in body
    refs = [line.footnote_refs for line in page.body if line.footnote_refs]
    assert refs == [("1",)]
    assert page.content.removed["footnote"] >= 2


def test_small_text_without_body_marker_stays_in_body(tmp_path: Path) -> None:
    page = extract_document(footnote_page(tmp_path, body_marker=False)).pages[0]
    assert page.footnotes == []
    assert any("Inserted with" in line.text for line in page.body)


# --- page furniture ----------------------------------------------------------


def test_page_numbers_removed_but_form_codes_kept(tmp_path: Path) -> None:
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((300, 40), "055")  # form code in the top band: not a page number
    p1.insert_text((72, 300), "Body one.")
    p2 = doc.new_page()
    p2.insert_text((300, 40), "2")  # running page number
    p2.insert_text((72, 300), "Body two.")
    p2.insert_text((72, 400), "7")  # mid-page digit: content
    pages = extract_document(save(doc, tmp_path / "pn.pdf")).pages
    assert [ln.text.strip() for ln in pages[0].body] == ["055", "Body one."]
    assert pages[0].page_label is None
    assert [ln.text.strip() for ln in pages[1].body] == ["Body two.", "7"]
    assert pages[1].page_label == "2"
    assert pages[1].content.removed == {"page_number": 1}


def test_contents_block_removed_including_wrapped_entries(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        "Synthetic Directions, 2025",
        "Table of Contents",
        "Chapter I - Preliminary ........................ 4",
        "Chapter II - General ........................... 16",
        "Chapter IX - Requirements under International Agreements -",
        "Communications from International Agencies ...... 56",
        "Chapter XI - Repeal ............................ 73",
    ]
    for i, text in enumerate(lines):
        page.insert_text((72, 120 + 20 * i), text, fontsize=10)
    structured = extract_document(save(doc, tmp_path / "toc.pdf")).pages[0]
    assert [ln.text.strip() for ln in structured.body] == ["Synthetic Directions, 2025"]
    assert structured.content.removed == {"toc": 6}


def test_few_dot_leaders_are_not_a_contents_block(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 300), "Signature ............ 12")
    page.insert_text((72, 330), "Ordinary text line.")
    structured = extract_document(save(doc, tmp_path / "x.pdf")).pages[0]
    # A lone leader line (e.g. on a form) is content, not a contents block.
    assert [ln.text.strip() for ln in structured.body] == [
        "Signature ............ 12",
        "Ordinary text line.",
    ]
    assert structured.content.removed == {}


def test_structure_page_is_idempotent_on_plain_page(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 300), "Plain body.")
    path = save(doc, tmp_path / "p.pdf")
    with pymupdf.open(path) as d:
        content = read_page(d[0], 1)
    structured = structure_page(content)
    assert [ln.text for ln in structured.body] == ["Plain body."]
    assert structured.footnotes == [] and structured.content.removed == {}


# --- bookmarks and sections --------------------------------------------------


def test_bookmarks_keep_mid_page_positions(tmp_path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page()
    goto = pymupdf.LINK_GOTO
    doc.set_toc(
        [
            [
                1,
                "Chapter  I – Preliminary",
                1,
                {"kind": goto, "page": 0, "to": pymupdf.Point(72, 72)},
            ],
            [2, "A. Title", 1, {"kind": goto, "page": 0, "to": pymupdf.Point(72, 100)}],
            [2, "B. Applicability", 1, {"kind": goto, "page": 0, "to": pymupdf.Point(72, 400)}],
            [1, "Chapter II – General", 2, {"kind": goto, "page": 1, "to": pymupdf.Point(72, 72)}],
        ]
    )
    marks = extract_document(save(doc, tmp_path / "bm.pdf")).bookmarks
    assert marks[0].title == "Chapter I – Preliminary"  # whitespace normalised
    assert [(m.page_number, round(m.y)) for m in marks] == [(1, 72), (1, 100), (1, 400), (2, 72)]


def test_section_index_paths() -> None:
    idx = SectionIndex(
        [
            Bookmark(1, "Ch I", 1, 72),
            Bookmark(2, "A", 1, 100),
            Bookmark(2, "B", 1, 400),
            Bookmark(1, "Ch II", 3, 72),
            Bookmark(2, "A", 3, 300),
            Bookmark(1, "Annex – I", 5, 72),
        ]
    )
    assert idx.path_at(1, 50) == ()  # before the first bookmark
    assert idx.path_at(1, 200) == ("Ch I", "A")
    assert idx.path_at(1, 398) == ("Ch I", "B")  # within tolerance of the heading
    assert idx.path_at(2, 10) == ("Ch I", "B")  # carries over to the next page
    assert idx.path_at(3, 100) == ("Ch II",)
    assert idx.path_at(3, 500) == ("Ch II", "A")
    assert idx.path_at(9, 0) == ("Annex – I",)
