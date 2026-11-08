"""RBI Directions profile: bookmark sections, numbered paragraphs, footnotes, tables.

- A unit runs from a numbered paragraph start ("33. ...", at the left margin) to the
  next start or the next bookmark section, so paragraphs that cross pages stay whole
  (`page_end`). Text before the first paragraph of a section is a preamble unit.
- Bookmark headings are section metadata, not chunk content.
- Footnote markers stay on their lines; `finalize` attaches the footnote text to the
  chunk holding the marker (never to the body text).
- Ruled tables (found in Stage A) become one chunk per row; header rows become column
  names and an empty first cell inherits the group label above it.
- Entity types are narrowed only for paragraphs that open with "For opening an account
  ... of a <legal form>"; otherwise the chunk keeps the document's types.
"""

import re
from dataclasses import replace

from app.ingestion.chunking import (
    Draft,
    Exclusion,
    ProfileOutput,
    SourceLine,
    native_lines,
    split_long,
    usable,
)
from app.ingestion.cleaning import is_non_latin, join_lines, normalize_line, word_count
from app.ingestion.pages import ProcessedPage
from app.ingestion.structure import Bookmark, ExtractedDocument, SectionIndex, Table
from app.schemas.taxonomy import ContentKind, EntityType

PARA_START = re.compile(r"^(\d{1,3})\.\s+\S")
MARGIN_TOLERANCE = 6.0  # points right of the page's left margin that still count as margin
HEADING_ZONE = 30.0  # points below a bookmark target that may hold its (wrapped) heading
LEAD_WORDS = 40
TABLE_HEADER_MAX_WORDS = 3
OPENING_ACCOUNT = re.compile(r"\bopen(ing)?\s+(an\s+)?account\b", re.IGNORECASE)
ENTITY_LEADS: tuple[tuple[re.Pattern[str], tuple[EntityType, ...]], ...] = (
    (re.compile(r"\bsole propriet", re.I), (EntityType.SOLE_PROPRIETORSHIP,)),
    (re.compile(r"\bof a company\b", re.I), (EntityType.COMPANY,)),
    (re.compile(r"\bof a partnership firm\b", re.I), (EntityType.PARTNERSHIP,)),
    (re.compile(r"\bof a trust\b", re.I), (EntityType.TRUST,)),
    (re.compile(r"\bunincorporated association\b", re.I), (EntityType.UNINCORPORATED_ASSOCIATION,)),
    (re.compile(r"\bjuridical person\b", re.I),
     (EntityType.SOCIETY, EntityType.OTHER_JURIDICAL_PERSON)),
)  # fmt: skip


def lead_entity_types(text: str) -> tuple[EntityType, ...] | None:
    lead = " ".join(text.split()[:LEAD_WORDS])
    if not OPENING_ACCOUNT.search(lead):
        return None
    hits = [types for pattern, types in ENTITY_LEADS if pattern.search(lead)]
    return hits[0] if len(hits) == 1 else None


def _is_heading(line: SourceLine, marks: list[Bookmark]) -> bool:
    norm = line.normalized
    if len(norm) < 3:
        return False
    for mark in marks:
        if mark.y - 3 <= line.y <= mark.y + HEADING_ZONE and norm in normalize_line(mark.title):
            return True
    return False


def _in_table(line: SourceLine, tables: list[Table]) -> Table | None:
    for t in tables:
        x0, y0, x1, y1 = t.bbox
        if x0 - 1 <= line.x <= x1 + 1 and y0 - 1 <= line.y <= y1 + 1:
            return t
    return None


def table_drafts(
    table: Table,
    page_number: int,
    section: str | None,
    paragraph: str | None,
    headers: dict[int, list[str]],
) -> list[Draft]:
    """One draft per data row. `headers` carries column names to continuation pages."""
    rows = [[join_lines((cell or "").split("\n")) for cell in row] for row in table.rows]
    ncols = len(rows[0])
    n_header = 0
    for row in rows:
        if all(word_count(c) <= TABLE_HEADER_MAX_WORDS for c in row if c):
            n_header += 1
        else:
            break
    if n_header:
        headers[ncols] = [
            " ".join(rows[i][c] for i in range(n_header) if rows[i][c]) for c in range(ncols)
        ]
    names = headers.get(ncols, [""] * ncols)
    drafts: list[Draft] = []
    group = ""
    for row in rows[n_header:]:
        if not any(row):
            continue
        group = row[0] or group  # merged first-column cell: inherit the label above
        cells = [group, *row[1:]]
        parts = [f"{names[c]}: {v}" if names[c] else v for c, v in enumerate(cells) if v]
        raw = " | ".join(cell for cell in row if cell)
        line = SourceLine(
            text=raw, page_number=page_number, y=table.bbox[1], x=table.bbox[0], size=0.0
        )
        drafts.append(
            Draft(
                lines=(line,),
                content=" | ".join(parts),
                section=section,
                paragraph=paragraph,
                content_kind=ContentKind.TABLE_ROW,
            )
        )
    return drafts


def chunk_rbi_directions(extracted: ExtractedDocument, pages: list[ProcessedPage]) -> ProfileOutput:
    sections = SectionIndex(extracted.bookmarks)
    marks: dict[int, list[Bookmark]] = {}
    for mark in extracted.bookmarks:
        marks.setdefault(mark.page_number, []).append(mark)
    tables = {sp.page_number: sp.tables for sp in extracted.pages}

    drafts: list[Draft] = []
    exclusions: list[Exclusion] = []
    headers: dict[int, list[str]] = {}
    unit: list[SourceLine] = []
    section: str | None = None
    paragraph: str | None = None

    def flush() -> None:
        if not unit:
            return
        kind = ContentKind.PARAGRAPH if paragraph else ContentKind.PREAMBLE
        draft = Draft(lines=tuple(unit), section=section, paragraph=paragraph, content_kind=kind)
        if paragraph:
            draft = replace(draft, entity_types=lead_entity_types(draft.text()))
        drafts.append(split_long(draft))
        unit.clear()

    for page in pages:
        if not usable(page):
            continue
        lines = native_lines(page)
        left = min((ln.x for ln in lines), default=0.0)
        page_tables = tables.get(page.page_number, [])
        done_tables: set[int] = set()
        for line in lines:
            norm = line.normalized
            if not norm:
                continue
            if is_non_latin(norm):
                exclusions.append(Exclusion(page.page_number, None, "non_latin_script", line.text))
                continue
            table = _in_table(line, page_tables)
            if table is not None:
                if id(table) not in done_tables:
                    done_tables.add(id(table))
                    flush()  # the paragraph resumes (same number) after the table
                    path = sections.path_at(page.page_number, table.bbox[1])
                    drafts.extend(
                        table_drafts(
                            table, page.page_number, " > ".join(path) or None, paragraph, headers
                        )
                    )
                exclusions.append(Exclusion(page.page_number, None, "table_cell_line", line.text))
                continue
            if _is_heading(line, marks.get(page.page_number, [])):
                exclusions.append(Exclusion(page.page_number, None, "section_heading", line.text))
                continue
            path = sections.path_at(page.page_number, line.y)
            current = " > ".join(path) or None
            if current != section:
                flush()
                section, paragraph = current, None
            start = PARA_START.match(norm)
            if start and line.x <= left + MARGIN_TOLERANCE:
                flush()
                # Annexes restart their numbering: "Annex – I 14" vs the Directions' "14".
                annex = path[0] if path and path[0].lower().startswith("annex") else None
                paragraph = f"{annex} {start.group(1)}" if annex else start.group(1)
            unit.append(line)
    flush()
    return ProfileOutput(drafts=drafts, exclusions=exclusions)
