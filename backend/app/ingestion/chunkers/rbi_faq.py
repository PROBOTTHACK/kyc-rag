"""RBI FAQ profile: one chunk per question and its answer.

A unit runs from "Q n." at the start of a line to the next question; answers that
cross pages stay whole (`page_end`). The section is "Q<n>: <question>", the paragraph
"Q<n>". Text before the first question (the FAQ's title block) becomes a preamble
chunk only if it carries real content; a title-only block is recorded as excluded.
"""

import re

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
from app.schemas.taxonomy import ContentKind

Q_START = re.compile(r"^Q\s?(\d{1,3})\.\s*(.*)$")
PREAMBLE_MIN_WORDS = 15
QUESTION_MAX_CHARS = 200


def _question(text: str) -> str:
    body = Q_START.sub(r"\2", text, count=1).strip()
    end = body.find("?")
    question = body[: end + 1] if end != -1 else body
    return question[:QUESTION_MAX_CHARS].strip()


def chunk_rbi_faq(extracted: ExtractedDocument, pages: list[ProcessedPage]) -> ProfileOutput:
    drafts: list[Draft] = []
    exclusions: list[Exclusion] = []
    unit: list[SourceLine] = []
    number: str | None = None

    def flush() -> None:
        if not unit:
            return
        if number is None:
            draft = Draft(
                lines=tuple(unit), section=None, paragraph=None,
                content_kind=ContentKind.PREAMBLE,
            )  # fmt: skip
            if word_count(draft.text()) < PREAMBLE_MIN_WORDS:
                exclusions.extend(
                    Exclusion(ln.page_number, None, "document_title", ln.text) for ln in unit
                )
                unit.clear()
                return
        else:
            question = _question(join_lines([ln.text for ln in unit]))
            draft = Draft(
                lines=tuple(unit), section=f"Q{number}: {question}", paragraph=f"Q{number}",
                content_kind=ContentKind.QA,
            )  # fmt: skip
        drafts.append(split_long(draft))
        unit.clear()

    for page in pages:
        if not usable(page):
            continue
        for line in native_lines(page):
            norm = line.normalized
            if not norm:
                continue
            start = Q_START.match(norm)  # before the noise check: "Q 1." alone is short
            if not start and (is_non_latin(norm) or is_noise(norm)):
                reason = "non_latin_script" if is_non_latin(norm) else "decoration"
                exclusions.append(Exclusion(page.page_number, None, reason, line.text))
                continue
            if start:
                flush()
                number = start.group(1)
            unit.append(line)
    flush()
    return ProfileOutput(drafts=drafts, exclusions=exclusions)
