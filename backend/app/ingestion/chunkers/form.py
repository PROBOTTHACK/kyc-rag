"""Form profile: headings, declarations, instructions and field labels, per page.

Form text shows what a form requests; it is never evidence of a regulatory obligation
on its own (docs/15). Chunks keep `document_type=form`, hence `supporting` eligibility.

Native pages: PyMuPDF block order is not reading order on these forms, so blocks are
sorted top-to-bottom, left-to-right. Vertical letter runs ("S","U","R",...) collapse
into one word; a line repeated on the same page (the same declaration printed for
each applicant) is kept once and the repeats are recorded as excluded. A heading is
a one-line block that is lettered ("P) SIGNATURE MISMATCH DECLARATION") or set in a
clearly larger font ("DECLARATION"). Prose blocks are packed into `form_text` chunks
under the current heading; short label blocks go into one `form_fields` chunk per
section and page.

OCR pages: each region (fold-out left/right half) is chunked on its own, so the two
printed pages of a sheet never mix. Low-confidence (< 40) and noise lines are left out
of the chunk text but stored on the chunk (`excluded_lines`), never dropped.
"""

import re
import statistics
from dataclasses import dataclass, field, replace

from app.ingestion.chunking import (
    Draft,
    Exclusion,
    ProfileOutput,
    SourceLine,
    native_lines,
    ocr_lines,
    usable,
)
from app.ingestion.cleaning import (
    collapse_vertical_letters,
    is_noise,
    is_non_latin,
    normalize_line,
    word_count,
)
from app.ingestion.ocr import LOW_CONFIDENCE_LINE
from app.ingestion.pages import ProcessedPage
from app.ingestion.structure import ExtractedDocument
from app.schemas.taxonomy import ContentKind, ExtractionMethod

FORM_WINDOW = 200  # words per form chunk
PROSE_MIN_WORDS = 12  # a block with fewer words is a field label
HEADING_MAX_WORDS = 12  # size-based headings
LETTERED_HEADING_MAX_WORDS = 30  # "S) CLOSE RELATIVE DECLARATION (To be filled by ...)"
CAPS_HEADING_MAX_WORDS = 16  # all-caps titles ("APPLICATION FOR ACCOUNT OPENING USING ...")
HEADING_SIZE_RATIO = 1.3  # font this much larger than the page median marks a heading
LETTERED_HEADING = re.compile(r"^[A-Z]{1,2}\)\s+\S")
NATIVE_MIN_RUN = 2  # native labels such as "ID" or "No" are real text
OCR_HEADING_MIN_CONFIDENCE = 60.0
OCR_LETTERED_GROUP = re.compile(r"\([A-Z]\)\s*$")  # "Proof of Address (C)"
MIN_CHUNK_WORDS = 5  # smaller OCR fragments join the neighbouring chunk
# OCR debris around a heading ("| NRI ... FOR", "... FORM °"); parentheses are kept.
HEADING_JUNK = re.compile(r"^[^\w(]+|[^\w)]+$")


@dataclass
class _Piece:
    text: str
    lines: list[SourceLine]


@dataclass
class _Section:
    title: str | None
    prose: list[_Piece] = field(default_factory=list)
    fields: list[_Piece] = field(default_factory=list)


def _windows(pieces: list[_Piece], limit: int) -> list[list[_Piece]]:
    out: list[list[_Piece]] = []
    current: list[_Piece] = []
    words = 0
    for piece in pieces:
        n = word_count(piece.text)
        if current and words + n > limit:
            out.append(current)
            current, words = [], 0
        current.append(piece)
        words += n
    if current:
        out.append(current)
    return out


def _section_drafts(sec: _Section) -> list[Draft]:
    drafts: list[Draft] = []
    for window in _windows(sec.prose, FORM_WINDOW):
        drafts.append(
            Draft(
                lines=tuple(ln for p in window for ln in p.lines),
                content="\n".join(p.text for p in window),
                section=sec.title,
                paragraph=None,
                content_kind=ContentKind.FORM_TEXT,
            )
        )
    for window in _windows(sec.fields, FORM_WINDOW):
        drafts.append(
            Draft(
                lines=tuple(ln for p in window for ln in p.lines),
                content="Fields: " + " | ".join(p.text for p in window),
                section=sec.title,
                paragraph=None,
                content_kind=ContentKind.FORM_FIELDS,
            )
        )
    return drafts


def _native_page(
    page: ProcessedPage, title: str | None, exclusions: list[Exclusion]
) -> tuple[list[Draft], str | None]:
    lines = native_lines(page)
    if not lines:
        return [], title
    median = statistics.median(ln.size for ln in lines)
    blocks: dict[int, list[SourceLine]] = {}
    for ln in lines:
        blocks.setdefault(ln.block, []).append(ln)

    def heading_line(ln: SourceLine) -> bool:
        text = ln.normalized
        words = word_count(text)
        if LETTERED_HEADING.match(text):
            return words <= LETTERED_HEADING_MAX_WORDS
        if 0 < words <= HEADING_MAX_WORDS and ln.size >= HEADING_SIZE_RATIO * median:
            return True
        # All-caps title, optionally followed by a parenthetical instruction:
        # "DORMANT ACTIVATION (for dormant account activation branch visit is mandatory)".
        # Two-word caps labels without one ("FIRST APPLICANT") stay field labels.
        # Needs two real words and a leading letter, so income bands ("250 - 500 L") and
        # fill-in lines ("___ ___ CO-OPERATIVE SOCIETY,") stay text.
        head = text.split("(")[0].split()
        letters = [c for c in " ".join(head) if c.isalpha()]
        real_words = [w for w in head if sum(c.isalpha() for c in w) >= 2]
        return (
            len(real_words) >= 2 and len(head) <= CAPS_HEADING_MAX_WORDS
            and head[0][0].isalpha() and all(c.isupper() for c in letters)
            and (len(head) >= 3 or "(" in text)
        )  # fmt: skip

    # A heading can sit inside a block of body text ("X) EXTENDED KYC" as the last line
    # of the mariners block, a 12pt "DORMANT ACTIVATION" above 8pt text): split there.
    segments: list[list[SourceLine]] = []
    for block in blocks.values():
        block.sort(key=lambda ln: (round(ln.y), ln.x))
        current: list[SourceLine] = []
        for ln in block:
            if heading_line(ln):  # the heading line stands alone
                if current:
                    segments.append(current)
                segments.append([ln])
                current = []
            else:
                current.append(ln)
        if current:
            segments.append(current)
    ordered = sorted(segments, key=lambda b: (round(min(ln.y for ln in b)), min(ln.x for ln in b)))
    seen: set[str] = set()
    sections: list[_Section] = [_Section(title)]
    for block in ordered:
        # Items keep line provenance; a vertical letter run is one item for its block.
        if any(len(ln.text.strip()) == 1 for ln in block):
            raw_items = [(t, block) for t in collapse_vertical_letters([ln.text for ln in block])]
        else:
            raw_items = [(ln.text, [ln]) for ln in block]
        items: list[_Piece] = []
        for raw, prov in raw_items:
            norm = normalize_line(raw)
            reason = (
                "empty" if not norm
                else "non_latin_script" if is_non_latin(norm)
                else "noise" if is_noise(norm, NATIVE_MIN_RUN)
                else "duplicate_line" if norm in seen
                else None
            )  # fmt: skip
            if reason:
                if norm:
                    exclusions.append(Exclusion(page.page_number, None, reason, raw))
                continue
            seen.add(norm)
            items.append(_Piece(norm, list(prov)))
        if not items:
            continue
        text = " ".join(p.text for p in items)
        whole = _Piece(text, list(dict.fromkeys(ln for p in items for ln in p.lines)))
        heading = len(block) == 1 and len(items) == 1 and heading_line(block[0])
        if heading:
            sections.append(_Section(text, prose=[whole]))
        elif word_count(text) > FORM_WINDOW:
            sections[-1].prose.extend(items)  # oversized block: split at line boundaries
        elif word_count(text) >= PROSE_MIN_WORDS:
            sections[-1].prose.append(whole)
        else:
            sections[-1].fields.append(whole)
    # A section holding only its heading (e.g. a title wrapped over two blocks) is not a
    # chunk of its own: its heading leads the next section's text instead.
    merged: list[_Section] = []
    carry: list[_Piece] = []
    for sec in sections:
        heading_only = sec.title is not None and len(sec.prose) == 1 and not sec.fields
        if heading_only and sec is not sections[-1]:
            carry.extend(sec.prose)
            continue
        sec.prose = carry + sec.prose
        carry = []
        merged.append(sec)
    drafts = [d for sec in merged for d in _section_drafts(sec)]
    return drafts, sections[-1].title


def _is_ocr_heading(line: SourceLine) -> bool:
    text = line.normalized
    if (line.confidence or 0) < OCR_HEADING_MIN_CONFIDENCE or word_count(text) > 10:
        return False
    letters = [c for c in text if c.isalpha()]
    upper = len(text.split()) >= 2 and bool(letters) and all(c.isupper() for c in letters)
    return upper or bool(OCR_LETTERED_GROUP.search(text))


def _ocr_region(lines: list[SourceLine], exclusions: list[Exclusion]) -> list[Draft]:
    drafts: list[Draft] = []
    title: str | None = None
    kept: list[SourceLine] = []
    dropped: list[SourceLine] = []

    def flush(final: bool = False) -> None:
        nonlocal kept, dropped
        if not kept:
            return
        if sum(word_count(ln.text) for ln in kept) < MIN_CHUNK_WORDS:
            if not final:
                return  # tiny head (logo, perforation mark): carried into the next chunk
            if drafts:  # tiny tail: joins the region's previous chunk
                last = drafts[-1]
                drafts[-1] = replace(
                    last,
                    lines=last.lines + tuple(kept),
                    excluded_lines=last.excluded_lines + tuple(dropped),
                )
                kept, dropped = [], []
                return
        drafts.append(
            Draft(
                lines=tuple(kept), section=title, paragraph=None,
                content_kind=ContentKind.FORM_TEXT, excluded_lines=tuple(dropped),
                line_separator="\n",
            )  # fmt: skip
        )
        kept, dropped = [], []

    for line in lines:
        norm = line.normalized
        low = line.confidence is not None and line.confidence < LOW_CONFIDENCE_LINE
        if not norm or low or is_noise(norm):
            dropped.append(line)
            continue
        if _is_ocr_heading(line):
            flush()
            title = HEADING_JUNK.sub("", norm) or norm
        elif kept and sum(word_count(ln.text) for ln in kept) + word_count(norm) > FORM_WINDOW:
            flush()
        kept.append(line)
    flush(final=True)
    if dropped:  # trailing low-confidence lines: keep them with the region's last chunk
        if drafts:
            last = drafts[-1]
            drafts[-1] = Draft(
                lines=last.lines, section=last.section, paragraph=None,
                content_kind=last.content_kind, line_separator="\n",
                excluded_lines=last.excluded_lines + tuple(dropped),
            )  # fmt: skip
        else:
            exclusions.extend(
                Exclusion(ln.page_number, ln.region, "low_confidence_or_noise_ocr", ln.text)
                for ln in dropped
            )
    return drafts


def chunk_form(extracted: ExtractedDocument, pages: list[ProcessedPage]) -> ProfileOutput:
    drafts: list[Draft] = []
    exclusions: list[Exclusion] = []
    title: str | None = None  # a native heading carries over to the next page
    for page in pages:
        if not usable(page):
            continue
        if page.extraction_method is ExtractionMethod.OCR:
            for _, region_lines in ocr_lines(page):
                drafts.extend(_ocr_region(region_lines, exclusions))
        else:
            page_drafts, title = _native_page(page, title, exclusions)
            drafts.extend(page_drafts)
    return ProfileOutput(drafts=drafts, exclusions=exclusions)
