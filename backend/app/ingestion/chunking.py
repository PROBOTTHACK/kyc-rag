"""Stage C: document-specific, section-aware chunking (docs/04, docs/15).

Profiles (manifest `chunking_profile`) live in `app/ingestion/chunkers/`; each turns
Stage A/B pages into `Draft`s. This module holds what they share:

- `SourceLine`: one line of native text or OCR output, with its provenance
  (PDF page, position, region, OCR confidence, footnote markers).
- `Draft`: a chunk before ids exist. It keeps its source lines, so raw text, pages,
  region and OCR quality are derived from them and never re-typed by hand.
- `finalize`: assigns deterministic indices/ids in document order (a parent is
  emitted before its children) and builds `ChunkMetadata` via `for_document`.
- `split_long`: paragraphs over `WORD_LIMIT` become a parent (Postgres only, not
  embedded) plus children split at sub-clause starts "(1)", "(i)", "(a)", falling
  back to whole lines, so every child keeps exact line provenance.

Lines that do not become chunk content are returned as `Exclusion`s (with reason and
text) or kept on the chunk itself (`excluded_lines`, e.g. low-confidence OCR lines);
nothing is dropped silently.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from app.config import Settings, get_settings
from app.ingestion.cleaning import join_lines, normalize_line, word_count
from app.ingestion.pages import ProcessedPage
from app.ingestion.structure import ExtractedDocument, Footnote
from app.schemas.corpus import ChunkMetadata, DocumentMetadata, FootnoteRef
from app.schemas.taxonomy import (
    ChunkingProfile,
    ChunkRole,
    ContentKind,
    EntityType,
    ExtractionMethod,
    PageStatus,
    TextReliability,
)

WORD_LIMIT = 300  # single/child chunk budget (BGE-small: 512 tokens; checked in Stage D)
SUBCLAUSE_START = re.compile(r"^\((\d{1,2}|[ivxlc]{1,6}|[a-z])\)\s")
SENTENCE_END = (".", ";", ":")
# A line that completes its clause: ends "." or ";" (optionally "; or" / "; and").
CLOSED = re.compile(r"[.;](\s+(or|and))?\s*$", re.IGNORECASE)
CONTEXT_MAX_WORDS = 40  # cap for a child's context (paragraph lead, open lead)
# Lines that qualify the text before them; a child never starts with one.
QUALIFIER = re.compile(
    r"^(Provided|Explanation|Note|However|Further|Notwithstanding|Except|Unless)\b",
    re.IGNORECASE,
)
BOLD_FLAG = 16  # PyMuPDF span flag bit 4

# Worst-first ordering for propagating page quality into a chunk.
_STATUS_RANK = {
    PageStatus.OK: 0,
    PageStatus.OCR_LOW_CONFIDENCE: 1,
    PageStatus.LOW_QUALITY: 2,
    PageStatus.FAILED: 3,
}


@dataclass(frozen=True)
class SourceLine:
    text: str  # raw, as extracted (native) or recognised (OCR)
    page_number: int
    y: float  # top; PDF points for native lines, pixels within the region for OCR
    x: float
    size: float  # font size (native) or line height in pixels (OCR)
    bold: bool = False
    block: int = 0
    region: str | None = None
    printed_page: int | None = None
    confidence: float | None = None  # OCR mean word confidence, 0-100
    footnote_refs: tuple[str, ...] = ()

    @property
    def normalized(self) -> str:
        return normalize_line(self.text)


@dataclass(frozen=True)
class Draft:
    lines: tuple[SourceLine, ...]
    section: str | None
    paragraph: str | None
    content_kind: ContentKind
    content: str = ""  # normalised text; derived from `lines` when left empty
    entity_types: tuple[EntityType, ...] | None = None  # None = document's types
    role: ChunkRole = ChunkRole.SINGLE
    children: tuple["Draft", ...] = ()
    excluded_lines: tuple[SourceLine, ...] = ()  # kept with the chunk, not in its content
    line_separator: str = " "  # forms keep one item per line
    context: str | None = None  # children: parent lead / open lead they continue

    def text(self) -> str:
        if self.content:
            return self.content
        if self.line_separator == " ":
            return join_lines([ln.text for ln in self.lines])
        return self.line_separator.join(n for n in (ln.normalized for ln in self.lines) if n)


@dataclass(frozen=True)
class Exclusion:
    page_number: int
    region: str | None
    reason: str
    text: str


@dataclass(frozen=True)
class ChunkRecord:
    metadata: ChunkMetadata
    extras: dict[str, Any]  # Postgres-only: raw text, excluded lines

    @property
    def embeddable(self) -> bool:
        return self.metadata.embedding_eligible


@dataclass(frozen=True)
class OcrEvidencePolicy:
    """How OCR quality maps to `text_reliability` and embedding eligibility (docs/15).

    `reliable_min_confidence` is a mean line confidence on 0-1; a chunk on any page whose
    status is not `ok` is uncertain whatever its confidence. Defaults come from settings
    (`OCR_RELIABLE_MIN_CONFIDENCE`, `OCR_EMBED_UNCERTAIN`); a manifest entry may override.
    """

    reliable_min_confidence: float = 0.80
    embed_uncertain: bool = True

    @classmethod
    def for_document(cls, doc: DocumentMetadata, settings: Settings) -> "OcrEvidencePolicy":
        return cls(
            reliable_min_confidence=(
                doc.ocr_reliable_min_confidence
                if doc.ocr_reliable_min_confidence is not None
                else settings.OCR_RELIABLE_MIN_CONFIDENCE
            ),
            embed_uncertain=(
                doc.embed_uncertain_ocr
                if doc.embed_uncertain_ocr is not None
                else settings.OCR_EMBED_UNCERTAIN
            ),
        )

    def reliability(
        self, method: ExtractionMethod, page_status: PageStatus, confidence: float | None
    ) -> TextReliability:
        if method is ExtractionMethod.TEXT:
            return TextReliability.NATIVE
        if page_status is PageStatus.OK and (confidence or 0) >= self.reliable_min_confidence:
            return TextReliability.OCR_RELIABLE
        return TextReliability.OCR_UNCERTAIN


@dataclass
class ChunkingResult:
    document_key: str
    profile: ChunkingProfile
    chunks: list[ChunkRecord]
    exclusions: list[Exclusion] = field(default_factory=list)
    policy: OcrEvidencePolicy = field(default_factory=OcrEvidencePolicy)

    def parent_of(self, record: ChunkRecord) -> ChunkRecord | None:
        """Expand a retrieved child to its full paragraph (synthesis and citation)."""
        pid = record.metadata.parent_chunk_id
        return next((c for c in self.chunks if c.metadata.chunk_id == pid), None) if pid else None

    def children_of(self, record: ChunkRecord) -> list[ChunkRecord]:
        cid = record.metadata.chunk_id
        return [c for c in self.chunks if c.metadata.parent_chunk_id == cid]


@dataclass
class ProfileOutput:
    drafts: list[Draft]
    exclusions: list[Exclusion] = field(default_factory=list)


Profile = Callable[[ExtractedDocument, list[ProcessedPage]], ProfileOutput]


# --- building source lines -----------------------------------------------------


def native_lines(page: ProcessedPage) -> list[SourceLine]:
    return [
        SourceLine(
            text=line.text,
            page_number=page.page_number,
            y=line.y0,
            x=line.x0,
            size=line.size,
            bold=any(s.flags & BOLD_FLAG for s in line.spans if s.text.strip()),
            block=line.block,
            footnote_refs=line.footnote_refs,
        )
        for line in page.structured.body
    ]


def ocr_lines(page: ProcessedPage) -> list[tuple[str, list[SourceLine]]]:
    """OCR lines per region, in region order (left before right)."""
    out: list[tuple[str, list[SourceLine]]] = []
    for region in page.ocr_regions:
        label = None if region.label == "full" else region.label
        out.append(
            (
                region.label,
                [
                    SourceLine(
                        text=ln.text,
                        page_number=page.page_number,
                        y=ln.bbox[1],
                        x=ln.bbox[0],
                        size=ln.bbox[3] - ln.bbox[1],
                        block=ln.block,
                        region=label,
                        printed_page=region.printed_page,
                        confidence=ln.confidence,
                    )
                    for ln in region.text.lines
                ],
            )
        )
    return out


def usable(page: ProcessedPage) -> bool:
    """Pages whose selected extraction can feed chunks (failed pages cannot)."""
    return page.extraction_method is not None


# --- long paragraphs -----------------------------------------------------------


def _split_point(current: list[SourceLine], following: SourceLine) -> int:
    """Index after which `current` may end: the last line that closes a sentence or
    clause and is not followed by a qualifier ("Provided that ...", "Explanation: ...")
    that belongs to it. 0 means no safe point (cut where the budget ran out)."""
    nxt = [*current[1:], following]
    for i in range(len(current) - 1, -1, -1):
        if current[i].normalized.endswith(SENTENCE_END) and not QUALIFIER.match(nxt[i].normalized):
            return i + 1
    return 0


def _closed_cut(current: list[SourceLine]) -> int:
    """Index just after the last completed clause ("." / ";") before an open trailing
    lead, never directly before a qualifier. 0 = no such point."""
    for i in range(len(current) - 2, -1, -1):
        if CLOSED.search(current[i].normalized) and not QUALIFIER.match(current[i + 1].normalized):
            return i + 1
    return 0


def _pack(lines: list[SourceLine], limit: int) -> list[list[SourceLine]]:
    """Pack lines into groups of at most `limit` words, ending groups at sentence or
    clause ends where possible so a condition stays with the requirement it qualifies."""
    groups: list[list[SourceLine]] = []
    current: list[SourceLine] = []
    for line in lines:
        n = word_count(line.text)
        while current and sum(word_count(ln.text) for ln in current) + n > limit:
            cut = _split_point(current, line) or len(current)
            groups.append(current[:cut])
            current = current[cut:]
        current.append(line)
    if current:
        groups.append(current)
    return groups


def split_long(draft: Draft, limit: int = WORD_LIMIT) -> Draft:
    """Return `draft` unchanged if it fits, else a parent whose children fit."""
    if word_count(draft.text()) <= limit:
        return draft
    # 1. clause segments: a new segment starts at each "(1)" / "(i)" / "(a)" line
    segments: list[list[SourceLine]] = []
    for line in draft.lines:
        if not segments or SUBCLAUSE_START.match(line.normalized):
            segments.append([line])
        else:
            segments[-1].append(line)
    # 2. pack whole segments up to the limit; oversized segments are packed by line
    groups: list[list[SourceLine]] = []
    current: list[SourceLine] = []
    for seg in segments:
        seg_words = sum(word_count(ln.text) for ln in seg)
        if seg_words > limit:
            if current:
                groups.append(current)
                current = []
            groups.extend(_pack(seg, limit))
        elif current and sum(word_count(ln.text) for ln in current) + seg_words > limit:
            if not CLOSED.search(current[-1].normalized):
                # The group ends on an open lead ("Provided that where ... submitted,",
                # "(2) V-CIP Procedure"): move the lead to the list it introduces.
                cut = _closed_cut(current)
                tail = sum(word_count(ln.text) for ln in current[cut:])
                if cut and tail + seg_words <= limit:
                    groups.append(current[:cut])
                    current = current[cut:] + seg
                    continue
            groups.append(current)
            current = list(seg)
        else:
            current.extend(seg)
    if current:
        groups.append(current)
    lead = _lead(draft.lines)
    children = []
    for k, group in enumerate(groups):
        parts: list[str] = [lead] if k and lead else []
        if k and not CLOSED.search(groups[k - 1][-1].normalized):
            open_lead = _open_tail(groups[k - 1])
            if open_lead and open_lead != lead:
                parts.append(open_lead)
        children.append(
            replace(
                draft, lines=tuple(group), content="", role=ChunkRole.CHILD, children=(),
                context=" | ".join(parts) or None,
            )  # fmt: skip
        )
    return replace(draft, role=ChunkRole.PARENT, children=tuple(children))


def _cap(text: str, words: int = CONTEXT_MAX_WORDS) -> str:
    parts = text.split()
    return text if len(parts) <= words else " ".join(parts[:words]) + " …"


def _lead(lines: tuple[SourceLine, ...]) -> str | None:
    """The paragraph's introduction: its text before the first sub-clause."""
    intro: list[str] = []
    for line in lines:
        if intro and SUBCLAUSE_START.match(line.normalized):
            break
        intro.append(line.text)
    return _cap(join_lines(intro)) if intro else None


def _open_tail(group: list[SourceLine]) -> str | None:
    """Trailing lines after the last completed sentence: an open lead ("... that:")."""
    start = 0
    for i, line in enumerate(group[:-1]):
        if line.normalized.endswith((".", ";")):
            start = i + 1
    return _cap(join_lines([ln.text for ln in group[start:]])) or None


# --- finalisation --------------------------------------------------------------


def _common(values: Iterable[Any]) -> Any:
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


def _footnotes(
    lines: Iterable[SourceLine], notes: dict[tuple[int, str], Footnote]
) -> list[FootnoteRef]:
    refs: list[FootnoteRef] = []
    seen: set[tuple[int, str]] = set()
    for line in lines:
        for marker in line.footnote_refs:
            key = (line.page_number, marker)
            if key in notes and key not in seen:
                seen.add(key)
                note = notes[key]
                text = normalize_line(note.text)  # same normalisation as chunk text
                refs.append(FootnoteRef(marker=marker, text=text, page_number=note.page_number))
    return refs


def _metadata(
    doc: DocumentMetadata,
    draft: Draft,
    index: int,
    pages: dict[int, ProcessedPage],
    notes: dict[tuple[int, str], Footnote],
    parent_id: str | None,
    policy: OcrEvidencePolicy,
) -> ChunkRecord:
    lines = draft.lines
    page_numbers = [ln.page_number for ln in lines]
    first, last = min(page_numbers), max(page_numbers)
    statuses = [pages[p].status for p in sorted(set(page_numbers))]
    worst = max(statuses, key=lambda s: _STATUS_RANK[s])
    confidences = [ln.confidence for ln in lines if ln.confidence is not None]
    method = ExtractionMethod.OCR if confidences else ExtractionMethod.TEXT
    ocr_confidence = round(sum(confidences) / len(confidences) / 100, 4) if confidences else None
    reliability = policy.reliability(method, worst, ocr_confidence)
    embedding_eligible = draft.role is not ChunkRole.PARENT and (
        reliability is not TextReliability.OCR_UNCERTAIN or policy.embed_uncertain
    )
    meta = ChunkMetadata.for_document(
        doc,
        chunk_index=index,
        page_number=first,
        page_end=last if last != first else None,
        content=draft.text(),
        extraction_method=method,
        section=draft.section,
        parent_chunk_id=parent_id,
        entity_types=list(draft.entity_types) if draft.entity_types is not None else None,
        paragraph=draft.paragraph,
        region=_common(ln.region for ln in lines),
        printed_page=_common(ln.printed_page for ln in lines),
        content_kind=draft.content_kind,
        page_status=worst,
        ocr_confidence=ocr_confidence,
        chunk_role=draft.role,
        footnotes=_footnotes(lines, notes),
        context=draft.context,
        text_reliability=reliability,
        embedding_eligible=embedding_eligible,
    )
    extras: dict[str, Any] = {"raw_text": "\n".join(ln.text for ln in lines)}
    if draft.excluded_lines:
        extras["excluded_lines"] = [
            {"text": ln.text, "page_number": ln.page_number, "region": ln.region,
             "confidence": ln.confidence}
            for ln in draft.excluded_lines
        ]  # fmt: skip
    return ChunkRecord(metadata=meta, extras=extras)


def finalize(
    doc: DocumentMetadata,
    drafts: list[Draft],
    extracted: ExtractedDocument,
    pages: list[ProcessedPage],
    policy: OcrEvidencePolicy,
) -> list[ChunkRecord]:
    by_page = {p.page_number: p for p in pages}
    notes = {(f.page_number, f.marker): f for sp in extracted.pages for f in sp.footnotes}
    records: list[ChunkRecord] = []
    for draft in drafts:
        if not draft.lines or not draft.text():
            continue
        if draft.role is ChunkRole.PARENT:
            parent = _metadata(doc, draft, len(records), by_page, notes, None, policy)
            records.append(parent)
            pid = parent.metadata.chunk_id
            for child in draft.children:
                records.append(_metadata(doc, child, len(records), by_page, notes, pid, policy))
        else:
            records.append(_metadata(doc, draft, len(records), by_page, notes, None, policy))
    return records


def chunk_document(
    doc: DocumentMetadata,
    extracted: ExtractedDocument,
    pages: list[ProcessedPage],
    policy: OcrEvidencePolicy | None = None,
) -> ChunkingResult:
    """`policy` defaults to `OcrEvidencePolicy.for_document(doc, get_settings())`."""
    from app.ingestion.chunkers import PROFILES  # local: chunkers import this module

    policy = policy or OcrEvidencePolicy.for_document(doc, get_settings())
    profile = doc.effective_chunking_profile
    output = PROFILES[profile](extracted, pages)
    chunks = finalize(doc, output.drafts, extracted, pages, policy)
    return ChunkingResult(
        document_key=doc.document_key, profile=profile, chunks=chunks,
        exclusions=output.exclusions, policy=policy,
    )  # fmt: skip
