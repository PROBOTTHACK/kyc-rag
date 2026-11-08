"""Page structure on top of raw extraction: sections, footnotes, page furniture.

- Sections come from PDF bookmarks, whose targets carry a y-position in the same
  top-left coordinates as text lines, so a section can start mid-page.
- Footnotes (e.g. RBI amendment notes "Inserted with effect from ...") are moved out
  of the body and kept as `Footnote`s; the superscript marker is removed from the body
  line and remembered in `Line.footnote_refs`, so the chunker can attach the note to
  the paragraph that cites it. A footnote is only recognised when the body carries a
  superscript marker with the same number.
- Running page numbers and contents-page leader lines are removed from the body.
Every removed line is counted in `PageContent.removed` by reason; nothing is silent.
"""

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import pymupdf

from app.ingestion.pdf import Line, PageContent, iter_pages, open_pdf

SECTION_Y_TOLERANCE = 3.0  # points; bookmark y is the heading's baseline area
FOOTNOTE_ZONE = 0.70  # footnotes live below this share of the page height
FOOTNOTE_SIZE_GAP = 1.5  # footnote text is at least this many points smaller than body
ROW_TOLERANCE = 3.0  # spans within this vertical distance belong to one visual row
PAGE_FURNITURE_BAND = 0.12  # top/bottom share of the page that holds page numbers
PAGE_LABEL_MAX_OFFSET = 2  # a printed page number lies within this distance of the PDF page

PAGE_NUMBER_LINE = re.compile(r"^\s*\d{1,3}\s*$")
TOC_LEADER = re.compile(r"\.{5,}\s*\d{1,3}\s*$")
TOC_HEADING = re.compile(r"^\s*(table of )?contents\s*$", re.IGNORECASE)
MIN_TOC_LEADERS = 3  # fewer leader lines than this is not a contents block
TABLE_MIN_RULING_SEGMENTS = 40  # cheap pre-filter: find_tables only on ruled pages
TABLE_MIN_ROWS, TABLE_MIN_COLS = 3, 2


@dataclass(frozen=True)
class Bookmark:
    level: int
    title: str
    page_number: int
    y: float


@dataclass(frozen=True)
class Footnote:
    marker: str
    text: str
    page_number: int


@dataclass(frozen=True)
class Table:
    """A ruled table found by PyMuPDF; cells as extracted (None = empty/merged cell)."""

    bbox: tuple[float, float, float, float]
    rows: tuple[tuple[str | None, ...], ...]


@dataclass
class StructuredPage:
    content: PageContent
    body: list[Line]
    footnotes: list[Footnote] = field(default_factory=list)
    page_label: str | None = None  # printed page number, when one was found
    # Ruled tables on this page. Their lines stay in `body`; a chunking profile may use
    # the cells instead (row chunks) and skip the lines inside `Table.bbox`.
    tables: list[Table] = field(default_factory=list)

    @property
    def page_number(self) -> int:
        return self.content.page_number


@dataclass
class ExtractedDocument:
    path: Path
    page_count: int
    bookmarks: list[Bookmark]
    pages: list[StructuredPage]


# --- bookmarks / sections ----------------------------------------------------


def read_bookmarks(doc: pymupdf.Document) -> list[Bookmark]:
    out: list[Bookmark] = []
    for entry in doc.get_toc(simple=False):
        level, page_number = int(entry[0]), int(entry[2])
        title = " ".join(str(entry[1]).split())
        if page_number < 1 or not title:
            continue  # bookmark without a usable target
        dest = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
        to = dest.get("to")
        y = float(to.y) if to is not None else 0.0
        out.append(Bookmark(level=level, title=title, page_number=page_number, y=y))
    return out


class SectionIndex:
    """Bookmark path (outermost first) in force at a given page position."""

    def __init__(self, bookmarks: list[Bookmark]) -> None:
        self._marks = sorted(
            enumerate(bookmarks), key=lambda iv: (iv[1].page_number, iv[1].y, iv[0])
        )

    def path_at(self, page_number: int, y: float) -> tuple[str, ...]:
        stack: list[Bookmark] = []
        for _, mark in self._marks:
            if (mark.page_number, mark.y) > (page_number, y + SECTION_Y_TOLERANCE):
                break
            while stack and stack[-1].level >= mark.level:
                stack.pop()
            stack.append(mark)
        return tuple(m.title for m in stack)


# --- footnotes ---------------------------------------------------------------


def _body_size(lines: list[Line]) -> float:
    weights: dict[float, int] = {}
    for line in lines:
        for s in line.spans:
            n = len(s.text.strip())
            if n:
                weights[s.size] = weights.get(s.size, 0) + n
    return max(weights, key=lambda k: weights[k]) if weights else 0.0


def _rows(lines: list[Line]) -> list[list[Line]]:
    """Group lines into visual rows (PyMuPDF may split one printed line in several)."""
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda ln: (ln.y0, ln.x0)):
        if rows and abs(rows[-1][0].y0 - line.y0) <= ROW_TOLERANCE:
            rows[-1].append(line)
        else:
            rows.append([line])
    return [sorted(row, key=lambda ln: ln.x0) for row in rows]


def _is_marker(text: str) -> bool:
    return text.strip().isdigit() and len(text.strip()) <= 2


def separate_footnotes(page: PageContent) -> tuple[list[Line], list[Footnote]]:
    body_size = _body_size(page.lines)
    zone_top = page.height * FOOTNOTE_ZONE
    small = body_size - FOOTNOTE_SIZE_GAP

    def in_zone(line: Line) -> bool:
        return line.y0 >= zone_top and 0 < line.size <= small

    body_markers = {
        s.text.strip()
        for line in page.lines
        if not in_zone(line)
        for s in line.spans
        if s.is_superscript and _is_marker(s.text) and s.size < body_size
    }
    if not body_markers:
        return list(page.lines), []

    zone = [line for line in page.lines if in_zone(line)]
    notes: dict[str, list[str]] = {}
    used: set[int] = set()
    current: str | None = None
    for row in _rows(zone):
        for line in row:
            texts = [s.text for s in line.spans if s.text.strip()]
            if texts and _is_marker(texts[0]) and texts[0].strip() in body_markers:
                current = texts[0].strip()
                notes.setdefault(current, [])
                texts = texts[1:]
            if current is None:
                continue  # small text above any marker: not a footnote, stays in body
            if texts:
                notes[current].append(" ".join(t.strip() for t in texts))
            used.add(id(line))

    footnotes = [
        Footnote(
            marker=m,
            text=re.sub(r"\s+([.,;:])", r"\1", " ".join(parts)).strip(),
            page_number=page.page_number,
        )
        for m, parts in notes.items()
        if parts
    ]
    found = {f.marker for f in footnotes}
    body: list[Line] = []
    for line in page.lines:
        if id(line) in used:
            continue
        keep = tuple(
            s
            for s in line.spans
            if not (s.is_superscript and _is_marker(s.text) and s.text.strip() in found)
        )
        refs = tuple(
            s.text.strip()
            for s in line.spans
            if s.is_superscript and _is_marker(s.text) and s.text.strip() in found
        )
        if refs:
            line = replace(line, spans=keep, footnote_refs=refs)
        if any(s.text.strip() for s in line.spans):
            body.append(line)
    removed = len(used)
    if removed:
        page.removed["footnote"] = page.removed.get("footnote", 0) + removed
    return body, footnotes


# --- page furniture ----------------------------------------------------------


def _is_page_label(text: str, page_number: int) -> bool:
    """A bare page number, not a form code such as "055"."""
    value = text.strip()
    if not PAGE_NUMBER_LINE.match(value) or value != str(int(value)):
        return False
    return abs(int(value) - page_number) <= PAGE_LABEL_MAX_OFFSET


def strip_page_furniture(page: PageContent, lines: list[Line]) -> tuple[list[Line], str | None]:
    top, bottom = page.height * PAGE_FURNITURE_BAND, page.height * (1 - PAGE_FURNITURE_BAND)
    label: str | None = None
    leaders = [i for i, line in enumerate(lines) if TOC_LEADER.search(line.text)]
    # Contents block = first..last leader line, so entries wrapped over two lines go too.
    toc_span = range(leaders[0], leaders[-1] + 1) if len(leaders) >= MIN_TOC_LEADERS else range(0)
    kept: list[Line] = []
    for i, line in enumerate(lines):
        text = line.text
        if _is_page_label(text, page.page_number) and (line.y0 < top or line.y0 > bottom):
            label = label or text.strip()
            page.removed["page_number"] = page.removed.get("page_number", 0) + 1
        elif i in toc_span or (toc_span and TOC_HEADING.match(text)):
            page.removed["toc"] = page.removed.get("toc", 0) + 1
        else:
            kept.append(line)
    return kept, label


def _ruling_segments(page: pymupdf.Page) -> int:
    segments = 0
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            segments += 4 if item[0] == "re" else 1 if item[0] == "l" else 0
    return segments


def _cell_text(words: list[tuple[float, ...]], cell: tuple[float, ...] | None) -> str | None:
    """Words whose centre lies in the cell, in reading order.

    `Table.extract()` drops spaces in text drawn glyph by glyph ("GovernmentandGovernment")
    and `get_textbox` bleeds into neighbouring rows; word centres avoid both.
    """
    if cell is None:
        return None  # merged cell
    x0, y0, x1, y1 = cell[:4]
    inside = [
        w for w in words
        if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1
    ]  # fmt: skip
    inside.sort(key=lambda w: (w[5], w[6], w[7]))  # block, line, word number
    return " ".join(str(w[4]) for w in inside)


def find_tables(page: pymupdf.Page) -> list[Table]:
    if _ruling_segments(page) < TABLE_MIN_RULING_SEGMENTS:
        return []
    words = page.get_text("words")
    out: list[Table] = []
    for t in page.find_tables().tables:
        if t.row_count >= TABLE_MIN_ROWS and t.col_count >= TABLE_MIN_COLS:
            rows = tuple(tuple(_cell_text(words, cell) for cell in row.cells) for row in t.rows)
            x0, y0, x1, y1 = (float(v) for v in t.bbox)
            out.append(Table(bbox=(x0, y0, x1, y1), rows=rows))
    return out


def structure_page(page: PageContent) -> StructuredPage:
    body, footnotes = separate_footnotes(page)
    body, label = strip_page_furniture(page, body)
    return StructuredPage(content=page, body=body, footnotes=footnotes, page_label=label)


def extract_document(path: Path, *, detect_tables: bool = False) -> ExtractedDocument:
    """Open, validate and structure every page. Raises PdfValidationError.

    `detect_tables` is opt-in: forms are full of ruled field boxes that look like tables
    and would cost seconds per page for nothing.
    """
    doc = open_pdf(path)
    try:
        bookmarks = read_bookmarks(doc)
        pages = [structure_page(p) for p in iter_pages(doc)]
        if detect_tables:
            for sp in pages:
                sp.tables = find_tables(doc[sp.page_number - 1])
        return ExtractedDocument(
            path=path, page_count=doc.page_count, bookmarks=bookmarks, pages=pages
        )
    finally:
        doc.close()
