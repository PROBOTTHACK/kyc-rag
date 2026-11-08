"""Question-heading profile (HDFC KYC/AML FAQ): one chunk per question and answer,
and one chunk per row of the two-column features/documents table.

- Headings are runs of bold lines at the left margin ("What is KYC?", "Introduction.").
  A heading with no text under it (the document title) is recorded as excluded.
- Table: a row of two bold header cells on one line ("Features" | "Documents*") opens
  it; lines left of the second header's x are the features column, lines right of it
  the documents column. A bold "N.0 ..." label opens a row; "N.M ..." opens a feature
  inside it. Document lines are grouped by vertical gaps and each group is paired with
  the feature it sits beside, so "1.1 Legal name" gets the identity documents and
  "1.2 Correct permanent address" the address documents. Rows may continue on the next
  page. The note under the table ("* Please note ... not an exhaustive list ...") is
  appended to every row so no row reads as a complete requirement.
"""

import re
from dataclasses import dataclass, field

from app.ingestion.chunking import (
    Draft,
    Exclusion,
    ProfileOutput,
    SourceLine,
    native_lines,
    split_long,
    usable,
)
from app.ingestion.cleaning import is_noise, is_non_latin, join_lines, word_count
from app.ingestion.pages import ProcessedPage
from app.ingestion.structure import ExtractedDocument
from app.schemas.taxonomy import ContentKind, EntityType

HEADING_MAX_WORDS = 25
ROW_START = re.compile(r"^(\d+)\.0\b")
FEATURE_START = re.compile(r"^\d+\.[1-9]\d*\b")
SAME_ROW = 3.0  # points: two lines on one visual row
COLUMN_GAP = 50.0  # points: header cells this far apart are separate columns
GROUP_GAP = 18.0  # points: a vertical gap this large starts a new document group
HEADING_WRAP_GAP = 16.0  # points: a bold line this close below continues the heading
NATIVE_MIN_RUN = 2  # "2.4" style labels are text, not decoration
ROW_ENTITIES: tuple[tuple[str, EntityType], ...] = (
    ("individual", EntityType.INDIVIDUAL),
    ("compan", EntityType.COMPANY),
    ("partnership", EntityType.PARTNERSHIP),
    ("trust", EntityType.TRUST),
)


@dataclass
class _Feature:
    lines: list[SourceLine]


@dataclass
class _Row:
    number: str
    label: list[SourceLine]
    features: list[_Feature] = field(default_factory=list)
    groups: list[list[SourceLine]] = field(default_factory=list)


def _row_draft(row: _Row, section: str | None, note: list[SourceLine]) -> Draft:
    # Pair each document group with the last feature at or above it (page, y).
    paired: dict[int, list[str]] = {}
    for group in row.groups:
        top = (group[0].page_number, group[0].y)
        target = 0
        for i, feat in enumerate(row.features):
            if (feat.lines[0].page_number, feat.lines[0].y) <= (top[0], top[1] + SAME_ROW):
                target = i
        paired.setdefault(target, []).append(join_lines([ln.text for ln in group]))
    label = join_lines([ln.text for ln in row.label])
    parts = [label]
    names = [join_lines([ln.text for ln in f.lines]) for f in row.features]
    if len(row.groups) > 1 and len(names) > 1:
        # Several document groups beside several features: pair them by position.
        for i, name in enumerate(names):
            docs = " ".join(paired.get(i, []))
            parts.append(f"{name} - documents: {docs}" if docs else name)
    else:
        # One document block for the row: it supports the row as a whole.
        parts.extend(names)
        docs = " ".join(join_lines([ln.text for ln in g]) for g in row.groups)
        if docs:
            parts.append(f"Documents: {docs}")
    note_text = join_lines([ln.text for ln in note])
    if note_text:
        parts.append(f"Note: {note_text}")
    lines = row.label + [ln for f in row.features for ln in f.lines]
    lines += [ln for g in row.groups for ln in g]
    types = tuple(t for key, t in ROW_ENTITIES if key in label.lower())
    return Draft(
        lines=tuple(lines),
        content="\n".join(p for p in parts if p),
        section=section,
        paragraph=f"Row {row.number}.0",
        content_kind=ContentKind.TABLE_ROW,
        entity_types=types or None,
    )


def chunk_question_headings(
    extracted: ExtractedDocument, pages: list[ProcessedPage]
) -> ProfileOutput:
    drafts: list[Draft] = []
    exclusions: list[Exclusion] = []
    stream = [ln for p in pages if usable(p) for ln in native_lines(p)]
    left = min((ln.x for ln in stream), default=0.0)

    heading: list[SourceLine] = []
    section: str | None = None
    answer: list[SourceLine] = []
    rows: list[_Row] = []
    note: list[SourceLine] = []
    split_x: float | None = None  # set while inside the table
    in_note = False

    def flush_answer() -> None:
        if answer:
            drafts.append(
                split_long(
                    Draft(lines=tuple(answer), section=section, paragraph=None,
                          content_kind=ContentKind.QA)  # fmt: skip
                )
            )
        elif heading and section is not None:
            exclusions.extend(
                Exclusion(ln.page_number, None, "heading_without_text", ln.text) for ln in heading
            )
        answer.clear()

    def flush_table() -> None:
        drafts.extend(_row_draft(row, section, note) for row in rows)
        rows.clear()
        note.clear()

    for i, line in enumerate(stream):
        norm = line.normalized
        if not norm:
            continue
        if is_non_latin(norm) or is_noise(norm, NATIVE_MIN_RUN):
            reason = "non_latin_script" if is_non_latin(norm) else "decoration"
            exclusions.append(Exclusion(line.page_number, None, reason, line.text))
            continue
        nxt = stream[i + 1] if i + 1 < len(stream) else None
        # Table header: two bold cells on one row, far apart.
        if (
            split_x is None and line.bold and nxt is not None and nxt.bold
            and nxt.page_number == line.page_number and abs(nxt.y - line.y) <= SAME_ROW
            and nxt.x - line.x > COLUMN_GAP
        ):  # fmt: skip
            split_x = nxt.x - 5
            flush_answer()  # the text introducing the table is its own Q&A chunk
            heading = []  # consumed; rows still use `section`
            exclusions.append(Exclusion(line.page_number, None, "table_header", line.text))
            continue
        if split_x is not None:
            if line.x >= split_x:
                if not rows:
                    exclusions.append(Exclusion(line.page_number, None, "table_header", line.text))
                    continue
                groups = rows[-1].groups
                prev = groups[-1][-1] if groups else None
                if (
                    prev is None
                    or line.page_number != prev.page_number
                    or line.y - prev.y > GROUP_GAP
                ):
                    groups.append([line])
                else:
                    groups[-1].append(line)
                continue
            if norm.startswith("*"):
                split_x, in_note = None, True
                note.append(line)
                continue
            start = ROW_START.match(norm)
            if line.bold and start:
                rows.append(_Row(number=start.group(1), label=[line]))
            elif line.bold and rows and not rows[-1].features:
                rows[-1].label.append(line)  # wrapped row label ("individuals")
            elif rows and FEATURE_START.match(norm):
                rows[-1].features.append(_Feature(lines=[line]))
            elif rows and rows[-1].features:
                rows[-1].features[-1].lines.append(line)
            continue
        is_heading_line = line.bold and line.x <= left + 6
        if in_note and not is_heading_line:
            note.append(line)
            continue
        if in_note:
            in_note = False
            flush_table()
        if is_heading_line:
            last = heading[-1] if heading else None
            if (
                last is not None and not answer
                and not last.normalized.endswith(("?", "."))
                and last.page_number == line.page_number
                and 0 < line.y - last.y <= HEADING_WRAP_GAP
            ):  # fmt: skip
                heading.append(line)  # heading wraps onto a second line
            else:
                flush_answer()
                heading = [line]
            text = join_lines([ln.text for ln in heading])
            section = text if word_count(text) <= HEADING_MAX_WORDS else text[:200]
            continue
        answer.append(line)
    if rows or note:
        flush_table()
    flush_answer()
    return ProfileOutput(drafts=drafts, exclusions=exclusions)
