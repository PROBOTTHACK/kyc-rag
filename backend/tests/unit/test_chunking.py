"""Stage C on synthetic PDFs: each profile, provenance, footnotes, OCR regions, determinism.

Every PDF is built in-test with PyMuPDF; nothing here comes from the real corpus.
OCR pages use a fake recogniser (monkeypatched `pages.run_ocr`).
"""

from pathlib import Path
from typing import Any

import pymupdf
import pytest

from app.config import Settings
from app.ingestion import pages as pages_mod
from app.ingestion.chunking import (
    ChunkingResult,
    OcrEvidencePolicy,
    chunk_document,
    split_long,
)
from app.ingestion.ocr import OcrLine, OcrText, TesseractSetup
from app.ingestion.pages import OcrCache, process_document
from app.schemas.corpus import DocumentMetadata, chunk_id
from app.schemas.taxonomy import (
    ChunkRole,
    ContentKind,
    EntityType,
    ExtractionMethod,
    PageStatus,
    TextReliability,
)

SHA = "e" * 64
GOTO = pymupdf.LINK_GOTO
FAKE_SETUP = TesseractSetup(
    cmd=Path("no-such-tesseract"), tessdata=Path("."), version="fake", languages=("eng",)
)


def doc_meta(profile: str, **overrides: Any) -> DocumentMetadata:
    data: dict[str, Any] = {
        "document_key": "synthetic_doc",
        "file": "x/synthetic.pdf",
        "sha256": SHA,
        "bank": "RBI",
        "document_type": "regulatory",
        "source_authority": "RBI",
        "attribution_basis": "document_text",
        "title": "Synthetic Directions",
        "source_status": "active",
        "entity_types": ["INDIVIDUAL", "COMPANY", "PARTNERSHIP"],
        "chunking_profile": profile,
    }
    data.update(overrides)
    return DocumentMetadata.model_validate(data)


def form_meta() -> DocumentMetadata:
    return doc_meta(
        "form", bank="HDFC", document_type="form", source_authority="HDFC",
        source_status="unknown", status_reason="synthetic form",
        entity_types=["INDIVIDUAL"],
    )  # fmt: skip


def two_pages(doc: pymupdf.Document) -> tuple[pymupdf.Page, pymupdf.Page]:
    # PyMuPDF orphans earlier Page handles when a page is added: create, then index.
    doc.new_page()
    doc.new_page()
    return doc[0], doc[1]


def put(page: pymupdf.Page, y: float, markup: str, size: int = 12, h: float = 18) -> None:
    page.insert_htmlbox(
        pymupdf.Rect(72, y, 540, y + h), f'<p style="font-size:{size}px">{markup}</p>'
    )


def run(tmp_path: Path, doc: pymupdf.Document, meta: DocumentMetadata, **kw: Any) -> ChunkingResult:
    path = tmp_path / "synthetic.pdf"
    doc.save(path)
    extracted, processed = process_document(
        path, pdf_sha256=SHA, setup=kw.get("setup"), cache=OcrCache(None),
        detect_tables=kw.get("detect_tables", False),
    )  # fmt: skip
    return chunk_document(meta, extracted, processed)


def by_para(result: ChunkingResult) -> dict[str | None, list[Any]]:
    out: dict[str | None, list[Any]] = {}
    for c in result.chunks:
        out.setdefault(c.metadata.paragraph, []).append(c)
    return out


# --- RBI Directions profile -----------------------------------------------------


def directions_pdf() -> pymupdf.Document:
    doc = pymupdf.open()
    p1, p2 = two_pages(doc)
    put(p1, 72, "Chapter I – Preliminary")
    put(p1, 100, "A. Definitions")
    put(p1, 130, "1. In these Directions, a customer means a person who holds an account.")
    put(p1, 150, "Explanation: the term includes joint holders of the account.")
    put(
        p1,
        180,
        "2. The bank shall apply the Regulations, 2016<sup>1</sup>and the Rules to customers.",
    )
    put(p1, 400, "B. Customer Due Diligence")
    put(p1, 430, "3. For opening an account of a company, the bank shall obtain the documents.")
    put(p1, 450, "4. The bank shall maintain records of all documents obtained under")
    put(p1, 470, "these Directions for a period of five years after the relationship")
    put(
        p1,
        720,
        "<sup>1</sup>Inserted with effect from September 18, 2026 vide Amendment Directions.",
        size=9,
        h=14,
    )
    put(p2, 72, "ends, as required by the Rules.")
    put(p2, 92, "5. The bank shall report suspicious transactions to the authority.")

    def mark(level: int, title: str, y: float) -> list[Any]:
        return [level, title, 1, {"kind": GOTO, "page": 0, "to": pymupdf.Point(72, y)}]

    doc.set_toc(
        [
            mark(1, "Chapter I – Preliminary", 72),
            mark(2, "A. Definitions", 100),
            mark(2, "B. Customer Due Diligence", 400),
        ]
    )
    return doc


def test_directions_paragraphs_sections_and_pages(tmp_path: Path) -> None:
    result = run(tmp_path, directions_pdf(), doc_meta("rbi_directions"))
    paras = by_para(result)
    assert sorted(p for p in paras if p) == ["1", "2", "3", "4", "5"]
    one, three, four = paras["1"][0].metadata, paras["3"][0].metadata, paras["4"][0].metadata
    # Paragraph boundary: the explanation line stays with paragraph 1.
    assert "Explanation: the term includes joint holders" in one.content
    assert one.section == "Chapter I – Preliminary > A. Definitions"
    # Section transition within the page.
    assert three.section == "Chapter I – Preliminary > B. Customer Due Diligence"
    # Cross-page paragraph keeps its start page and records the end page.
    assert (four.page_number, four.page_end) == (1, 2)
    assert four.content.endswith("as required by the Rules.")
    # Headings are metadata, not content.
    assert all("Customer Due Diligence" not in c.metadata.content for c in result.chunks)
    assert {(e.reason, e.text.strip()) for e in result.exclusions} >= {
        ("section_heading", "B. Customer Due Diligence"),
    }


def test_directions_footnote_attached_not_in_body(tmp_path: Path) -> None:
    result = run(tmp_path, directions_pdf(), doc_meta("rbi_directions"))
    two = by_para(result)["2"][0].metadata
    assert [(f.marker, f.page_number) for f in two.footnotes] == [("1", 1)]
    assert two.footnotes[0].text.startswith("Inserted with effect from September 18, 2026")
    assert "Inserted with effect" not in two.content
    assert all(not c.metadata.footnotes for c in result.chunks if c.metadata.paragraph != "2")
    assert all("Inserted with effect" not in c.metadata.content for c in result.chunks)


def test_directions_entity_narrowing_only_on_opening_paragraphs(tmp_path: Path) -> None:
    paras = by_para(run(tmp_path, directions_pdf(), doc_meta("rbi_directions")))
    assert paras["3"][0].metadata.entity_types == [EntityType.COMPANY]
    assert len(paras["1"][0].metadata.entity_types) == 3  # general: the document's types


def test_long_paragraph_becomes_parent_and_children(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    put(page, 60, "7. The bank shall obtain the following documents from the customer:")
    clause = " ".join(["word"] * 55)
    for i in range(7):
        put(page, 90 + i * 100, f"({i + 1}) {clause}.", h=95)
    result = run(tmp_path, doc, doc_meta("rbi_directions"))
    chunks = [c.metadata for c in result.chunks]
    parent = chunks[0]
    assert parent.chunk_role is ChunkRole.PARENT and parent.paragraph == "7"
    children = [c for c in chunks if c.chunk_role is ChunkRole.CHILD]
    assert len(children) >= 2
    assert all(c.parent_chunk_id == parent.chunk_id for c in children)
    assert all(len(c.content.split()) <= 300 for c in children)
    # Children split at clause starts and together hold all of the parent's words.
    assert all(c.content.startswith(("7.", "(")) for c in children)
    assert sum(len(c.content.split()) for c in children) == len(parent.content.split())
    assert [r.embeddable for r in result.chunks] == [False] + [True] * len(children)


def test_split_long_keeps_short_drafts() -> None:
    from app.ingestion.chunking import Draft, SourceLine

    line = SourceLine(text="Short paragraph.", page_number=1, y=0, x=0, size=12)
    draft = Draft(lines=(line,), section=None, paragraph="1", content_kind=ContentKind.PARAGRAPH)
    assert split_long(draft) is draft


def test_table_rows_carry_headers_and_group_labels(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    put(page, 60, "9. Documents for foreign portfolio investors are as follows:")
    cols = [72, 190, 330, 450, 540]
    rows = [
        ["", "", "Category I", "Category II"],
        ["Entity Level", "Constitutive documents of the entity", "Mandatory", "Mandatory"],
        ["", "Proof of address of the entity", "Mandatory", "Not required"],
    ]
    y = 100.0
    for row in rows:
        for c, text in enumerate(row):
            page.insert_textbox(
                pymupdf.Rect(cols[c] + 3, y + 3, cols[c + 1] - 3, y + 37), text, fontsize=9
            )
        y += 40
    for r in range(3):  # one ruled rectangle per cell, as in the RBI tables
        for c in range(4):
            page.draw_rect(pymupdf.Rect(cols[c], 100 + 40 * r, cols[c + 1], 140 + 40 * r))
    result = run(tmp_path, doc, doc_meta("rbi_directions"), detect_tables=True)
    rows_out = [
        c.metadata for c in result.chunks if c.metadata.content_kind is ContentKind.TABLE_ROW
    ]
    assert len(rows_out) == 2
    assert rows_out[0].content.startswith("Entity Level | Constitutive documents")
    assert "Category I: Mandatory" in rows_out[0].content
    # Empty first cell inherits the group label; column names repeat per row.
    assert rows_out[1].content.startswith("Entity Level | Proof of address")
    assert "Category II: Not required" in rows_out[1].content
    assert all(r.paragraph == "9" for r in rows_out)


# --- RBI FAQ profile --------------------------------------------------------------


def test_faq_question_boundaries_and_cross_page_answers(tmp_path: Path) -> None:
    doc = pymupdf.open()
    p1, p2 = two_pages(doc)
    put(p1, 60, "FAQs on Synthetic Directions")
    put(p1, 100, "Q 1.")
    p1.insert_htmlbox(
        pymupdf.Rect(110, 100, 540, 118), '<p style="font-size:12px">What is KYC?</p>'
    )
    put(p1, 125, "Ans. KYC is a process of identifying the customer.")
    put(p1, 160, "Q 2. Is Aadhaar mandatory? When is it required?")
    put(p1, 180, "Ans. No. It is required only for benefits under section 7 of the Act")
    put(p2, 72, "and may otherwise be provided voluntarily.")
    put(p2, 100, "***")
    result = run(tmp_path, doc, doc_meta("rbi_faq"))
    chunks = [c.metadata for c in result.chunks]
    assert [c.paragraph for c in chunks] == ["Q1", "Q2"]
    assert chunks[0].section == "Q1: What is KYC?"
    assert chunks[1].section == "Q2: Is Aadhaar mandatory?"
    assert (chunks[1].page_number, chunks[1].page_end) == (1, 2)
    assert chunks[1].content.endswith("provided voluntarily.")
    assert all(c.content_kind is ContentKind.QA for c in chunks)
    reasons = {e.reason for e in result.exclusions}
    assert reasons == {"document_title", "decoration"}  # title-only preamble, "***"


# --- question-heading profile (two-column table) ---------------------------------


def policy_pdf() -> pymupdf.Document:
    doc = pymupdf.open()
    p1, p2 = two_pages(doc)

    def bold(page: pymupdf.Page, x: float, y: float, text: str) -> None:
        page.insert_text((x, y), text, fontsize=11, fontname="hebo")

    def plain(page: pymupdf.Page, x: float, y: float, text: str) -> None:
        page.insert_text((x, y), text, fontsize=11, fontname="helv")

    bold(p1, 90, 80, "Synthetic KYC FAQ")
    bold(p1, 90, 110, "What is KYC?")
    plain(p1, 90, 125, "KYC is the customer identification process.")
    bold(p1, 90, 160, "What documents are required?")
    plain(p1, 90, 175, "The documents are furnished below:")
    bold(p1, 90, 200, "Features")
    bold(p1, 240, 200, "Documents*")
    bold(p1, 90, 225, "1.0 Accounts of")
    bold(p1, 90, 238, "individuals")
    plain(p1, 90, 251, "1.1 Legal name")
    plain(p1, 90, 290, "1.2 Permanent address")
    plain(p1, 240, 225, "(i) Passport (ii) Voter card")
    plain(p1, 240, 238, "(iii) Driving License")
    plain(p1, 240, 290, "(i) Telephone bill (ii) Electricity bill")
    bold(p2, 90, 80, "2.0 Accounts of Companies")
    plain(p2, 90, 95, "2.1 Name of the Company")
    plain(p2, 90, 108, "2.2 T e l e p h o n e / F a x number")
    plain(p2, 240, 80, "(i) Certificate of incorporation (ii) Board resolution")
    bold(p2, 90, 140, "* Please note that this is not an exhaustive list.")
    bold(p2, 90, 170, "When does KYC apply?")
    plain(p2, 90, 185, "KYC applies when opening a new account.")
    return doc


def test_policy_qa_and_table_rows(tmp_path: Path) -> None:
    meta = doc_meta(
        "question_headings", bank="HDFC", document_type="policy", source_authority=None,
        attribution_basis="filename_only", source_status="dated", status_reason="synthetic",
        entity_types=["INDIVIDUAL", "COMPANY"],
    )  # fmt: skip
    result = run(tmp_path, policy_pdf(), meta)
    qa = [c.metadata for c in result.chunks if c.metadata.content_kind is ContentKind.QA]
    rows = [c.metadata for c in result.chunks if c.metadata.content_kind is ContentKind.TABLE_ROW]
    assert [q.section for q in qa] == [
        "What is KYC?", "What documents are required?", "When does KYC apply?",
    ]  # fmt: skip
    assert qa[1].content == "The documents are furnished below:"
    assert [r.paragraph for r in rows] == ["Row 1.0", "Row 2.0"]
    one, two = rows[0].content, rows[1].content
    # Two document groups beside two features: paired by position.
    assert "1.1 Legal name - documents: (i) Passport (ii) Voter card (iii) Driving License" in one
    assert "1.2 Permanent address - documents: (i) Telephone bill" in one
    # One document block for the row: it belongs to the whole row.
    assert "2.2 Telephone / Fax number" in two
    assert "Documents: (i) Certificate of incorporation" in two
    # The table note qualifies every row; rows cross pages and keep their own page.
    assert all(
        "Note: * Please note that this is not an exhaustive list." in r.content for r in rows
    )
    assert (rows[0].page_number, rows[1].page_number) == (1, 2)
    assert rows[0].entity_types == [EntityType.INDIVIDUAL]
    assert rows[1].entity_types == [EntityType.COMPANY]
    assert {e.reason for e in result.exclusions} >= {"heading_without_text", "table_header"}


# --- form profile: native pages ----------------------------------------------------


def test_form_headings_declarations_fields_and_duplicates(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60.0
    for text in ("PERSONAL DETAILS (to be filled by the applicant)", "Full Name", "Mobile"):
        page.insert_text((40, y), text, fontsize=8)
        y += 14
    for i, letter in enumerate("SURNAME"):
        page.insert_text((400, 60 + i * 9), letter, fontsize=8)
    declaration = (
        "I hereby declare that the signature on the passport is different from my signature "
        "on this form and request the bank to record the new signature."
    )
    page.insert_text((40, 200), "P) SIGNATURE MISMATCH DECLARATION", fontsize=8)
    page.insert_textbox(pymupdf.Rect(40, 210, 560, 250), declaration, fontsize=8)
    page.insert_textbox(pymupdf.Rect(40, 260, 560, 300), declaration, fontsize=8)  # 2nd applicant
    result = run(tmp_path, doc, form_meta())
    chunks = [c.metadata for c in result.chunks]
    sections = [c.section for c in chunks]
    assert "PERSONAL DETAILS (to be filled by the applicant)" in sections
    decl = [c for c in chunks if c.section == "P) SIGNATURE MISMATCH DECLARATION"]
    assert decl and decl[0].content_kind is ContentKind.FORM_TEXT
    assert decl[0].content.startswith("P) SIGNATURE MISMATCH DECLARATION")
    assert decl[0].content.count("I hereby declare") == 1  # repeated line kept once
    fields = [c for c in chunks if c.content_kind is ContentKind.FORM_FIELDS]
    assert any("SURNAME" in f.content and "Full Name" in f.content for f in fields)
    assert any(e.reason == "duplicate_line" for e in result.exclusions)
    assert all(c.evidence_eligibility.value == "supporting" for c in chunks)


# --- form profile: OCR pages and fold-out regions -----------------------------------


def scanned_pdf(size: tuple[float, float]) -> pymupdf.Document:
    src = pymupdf.open()
    sp = src.new_page(width=size[0], height=size[1])
    sp.insert_text((40, 120), "SCAN", fontsize=28)
    pix = sp.get_pixmap(dpi=72)
    doc = pymupdf.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_image(page.rect, pixmap=pix)
    return doc


def ocr_text(*lines: tuple[str, float]) -> OcrText:
    built = tuple(
        OcrLine(text=t, confidence=c, word_count=len(t.split()),
                bbox=(10.0, 20.0 * i, 400.0, 20.0 * i + 14), block=1, paragraph=1, line=i + 1)
        for i, (t, c) in enumerate(lines)
    )  # fmt: skip
    return OcrText(lines=built, word_confidences=tuple(c for t, c in lines for _ in t.split()))


def test_ocr_fold_out_regions_never_mix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    halves = iter(
        [
            ocr_text(
                ("NOMINATION FORM DETAILS", 80.0),
                ("Name of nominee and address", 66.0),
                ("ee | fi", 20.0),
                ("Relationship with depositor", 64.0),
            ),
            ocr_text(
                ("ACCOUNT OPENING FORM", 70.0),
                ("Type of Entity: Partnership HUF Trust", 58.0),
            ),
        ]
    )
    monkeypatch.setattr(pages_mod, "run_ocr", lambda setup, png, dpi: next(halves))
    result = run(tmp_path, scanned_pdf((1432, 894)), form_meta(), setup=FAKE_SETUP)
    chunks = result.chunks
    assert [c.metadata.region for c in chunks] == ["left", "right"]
    left, right = chunks[0].metadata, chunks[1].metadata
    assert "Type of Entity" not in left.content and "nominee" not in right.content
    assert left.section == "NOMINATION FORM DETAILS"
    # Low-confidence OCR line: out of the text, kept with the chunk.
    assert "ee | fi" not in left.content
    assert [x["text"] for x in chunks[0].extras["excluded_lines"]] == ["ee | fi"]
    # Quality propagates: page status, method, and confidence of the lines kept.
    assert left.page_status is PageStatus.OCR_LOW_CONFIDENCE
    assert left.extraction_method is ExtractionMethod.OCR
    assert left.ocr_confidence == pytest.approx((80 + 66 + 64) / 3 / 100, abs=1e-4)
    assert left.page_number == right.page_number == 1


def test_ocr_tiny_fragments_join_neighbours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = ocr_text(
        ("i} HDFC |", 75.0),
        ("PROOF OF ADDRESS (C)", 92.0),
        ("Valid passport or overseas driving license", 93.0),
        ("ok bye", 90.0),
    )
    monkeypatch.setattr(pages_mod, "run_ocr", lambda setup, png, dpi: text)
    result = run(tmp_path, scanned_pdf((595, 842)), form_meta(), setup=FAKE_SETUP)
    (chunk,) = result.chunks
    assert chunk.metadata.content.splitlines() == [
        "i} HDFC |", "PROOF OF ADDRESS (C)", "Valid passport or overseas driving license", "ok bye",
    ]  # fmt: skip
    assert chunk.metadata.page_status is PageStatus.OK


def test_failed_page_yields_no_chunks_but_others_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "P) DECLARATION OF THE APPLICANT", fontsize=8)
    declaration = "I declare that all the details given in this form are true and correct."
    doc[0].insert_textbox(pymupdf.Rect(72, 110, 540, 160), declaration, fontsize=8)
    doc.insert_pdf(scanned_pdf((595, 842)))
    result = run(tmp_path, doc, form_meta(), setup=None)  # Tesseract missing -> p2 failed
    assert {c.metadata.page_number for c in result.chunks} == {1}


# --- determinism and citation integrity --------------------------------------------


def test_chunking_is_deterministic_with_stable_ids(tmp_path: Path) -> None:
    meta = doc_meta("rbi_directions")
    a = run(tmp_path, directions_pdf(), meta)
    b = run(tmp_path, directions_pdf(), meta)
    assert [c.metadata.to_qdrant_payload() for c in a.chunks] == [
        c.metadata.to_qdrant_payload() for c in b.chunks
    ]
    assert [c.extras for c in a.chunks] == [c.extras for c in b.chunks]
    for i, c in enumerate(a.chunks):
        assert c.metadata.chunk_index == i
        assert c.metadata.chunk_id == str(chunk_id(SHA, i))


def test_citations_point_at_real_pages_and_raw_text(tmp_path: Path) -> None:
    result = run(tmp_path, directions_pdf(), doc_meta("rbi_directions"))
    for c in result.chunks:
        m = c.metadata
        assert 1 <= m.page_number <= 2 and (m.page_end is None or m.page_number < m.page_end <= 2)
        assert c.extras["raw_text"].strip()
        assert m.document_key == "synthetic_doc" and m.evidence_eligibility.value == "primary"


# --- OCR evidence policy ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "status", "confidence", "expected"),
    [
        (ExtractionMethod.TEXT, PageStatus.OK, None, TextReliability.NATIVE),
        (ExtractionMethod.OCR, PageStatus.OK, 0.93, TextReliability.OCR_RELIABLE),
        (ExtractionMethod.OCR, PageStatus.OK, 0.80, TextReliability.OCR_RELIABLE),
        (ExtractionMethod.OCR, PageStatus.OK, 0.79, TextReliability.OCR_UNCERTAIN),
        # A flagged page stays uncertain however confident one chunk looks.
        (ExtractionMethod.OCR, PageStatus.OCR_LOW_CONFIDENCE, 0.95, TextReliability.OCR_UNCERTAIN),
    ],
)
def test_ocr_policy_reliability(
    method: ExtractionMethod,
    status: PageStatus,
    confidence: float | None,
    expected: TextReliability,
) -> None:
    assert OcrEvidencePolicy(0.80).reliability(method, status, confidence) is expected


def test_ocr_policy_defaults_and_manifest_overrides() -> None:
    settings = Settings(_env_file=None, OCR_RELIABLE_MIN_CONFIDENCE=0.7)  # type: ignore[call-arg]
    assert OcrEvidencePolicy.for_document(form_meta(), settings) == OcrEvidencePolicy(0.7, True)
    strict = doc_meta(
        "form", bank="HDFC", document_type="form", source_authority="HDFC",
        source_status="unknown", status_reason="synthetic", entity_types=["INDIVIDUAL"],
        ocr_reliable_min_confidence=0.9, embed_uncertain_ocr=False,
    )  # fmt: skip
    assert OcrEvidencePolicy.for_document(strict, settings) == OcrEvidencePolicy(0.9, False)


def test_uncertain_ocr_is_preserved_flagged_and_never_mandatory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = ocr_text(("PROOF OF ADDRESS (C)", 66.0), ("Valid passport or utility bill", 62.0))
    monkeypatch.setattr(pages_mod, "run_ocr", lambda setup, png, dpi: text)
    # A *primary* document, so only text reliability can block MANDATORY support.
    meta = doc_meta("form")
    embed = run(tmp_path, scanned_pdf((595, 842)), meta, setup=FAKE_SETUP)
    (rec,) = embed.chunks
    m = rec.metadata
    assert m.evidence_eligibility.value == "primary"
    assert m.text_reliability is TextReliability.OCR_UNCERTAIN
    assert m.can_support_mandatory is False
    assert m.embedding_eligible is True and rec.embeddable  # retrievable, flagged
    assert m.to_qdrant_payload()["can_support_mandatory"] is False
    assert "Valid passport or utility bill" in m.content  # text preserved
    path = tmp_path / "synthetic.pdf"
    extracted, processed = process_document(
        path, pdf_sha256=SHA, setup=FAKE_SETUP, cache=OcrCache(None)
    )
    strict = chunk_document(meta, extracted, processed, OcrEvidencePolicy(0.80, False))
    assert strict.chunks[0].metadata.embedding_eligible is False
    assert strict.chunks[0].metadata.content == m.content  # still kept, just not embedded


def test_native_primary_text_can_support_mandatory(tmp_path: Path) -> None:
    result = run(tmp_path, directions_pdf(), doc_meta("rbi_directions"))
    assert all(c.metadata.text_reliability is TextReliability.NATIVE for c in result.chunks)
    assert all(c.metadata.can_support_mandatory for c in result.chunks)


# --- parent/child integrity ----------------------------------------------------------


def long_paragraph_pdf(clauses: list[str]) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page()
    put(page, 40, "8. The bank shall ensure the following for every customer account:")
    for i, clause in enumerate(clauses):
        put(page, 70 + i * 110, clause, h=105)
    return doc


def test_children_reassemble_parent_and_expand(tmp_path: Path) -> None:
    clause = " ".join(["word"] * 60)
    result = run(
        tmp_path,
        long_paragraph_pdf([f"({n}) {clause}." for n in range(1, 7)]),
        doc_meta("rbi_directions"),
    )
    parent = result.chunks[0]
    children = result.children_of(parent)
    assert parent.metadata.chunk_role is ChunkRole.PARENT and len(children) >= 2
    assert all(result.parent_of(c) is parent for c in children)
    assert result.parent_of(parent) is None
    # The parent holds the complete paragraph: children reassemble it exactly.
    squash = lambda t: "".join(t.split())  # noqa: E731
    assert squash(parent.metadata.content) == "".join(squash(c.metadata.content) for c in children)
    assert parent.extras["raw_text"] == "\n".join(c.extras["raw_text"] for c in children)
    # Children after the first carry the paragraph lead as context, outside content.
    assert children[0].metadata.context is None
    lead = "8. The bank shall ensure the following for every customer account:"
    assert all(c.metadata.context == lead for c in children[1:])
    assert all(c.metadata.paragraph == "8" for c in children)
    assert not parent.embeddable and all(c.embeddable for c in children)


def test_proviso_stays_with_its_requirement(tmp_path: Path) -> None:
    # One oversized clause: packing must not cut before "Provided that ...".
    filler = " ".join(["word"] * 18)
    lines = [f"(1) The bank shall verify the identity {filler} of the customer."]
    lines += [f"{filler} {filler}." for _ in range(6)]
    lines += [f"The bank shall also verify the address {filler}."]
    lines += [f"Provided that the bank may accept a deemed OVD {filler}."]
    lines += [f"{filler}." for _ in range(4)]
    doc = pymupdf.open()
    page = doc.new_page()
    put(page, 30, "9. Verification of customers")
    for i, text in enumerate(lines):
        put(page, 50 + i * 55, text, h=52)
    result = run(tmp_path, doc, doc_meta("rbi_directions"))
    children = [c.metadata for c in result.chunks if c.metadata.chunk_role is ChunkRole.CHILD]
    assert len(children) >= 2
    for child in children:
        assert not child.content.startswith("Provided")
    holder = next(c for c in children if "Provided that the bank may accept" in c.content)
    assert "The bank shall also verify the address" in holder.content
    assert all(len(c.content.split()) <= 300 for c in children)


def test_open_lead_moves_to_the_list_it_introduces(tmp_path: Path) -> None:
    clause = " ".join(["word"] * 70)
    clauses = [f"(1) {clause}.", f"(2) {clause}.", f"(3) {clause}.",
               "(4) The bank shall ensure that:", f"(i) {clause}.", f"(ii) {clause}."]  # fmt: skip
    result = run(tmp_path, long_paragraph_pdf(clauses), doc_meta("rbi_directions"))
    children = [c.metadata for c in result.chunks if c.metadata.chunk_role is ChunkRole.CHILD]
    holder = next(c for c in children if "(4) The bank shall ensure that:" in c.content)
    assert "(i)" in holder.content  # the lead travels with its list
    assert not any(c.content.rstrip().endswith("ensure that:") for c in children)
