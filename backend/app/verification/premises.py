"""Premises: windows of STORED evidence text with exact provenance (M4 S2; M4_PLAN §D–§F).

A premise is built from the Postgres evidence context of one citation
(`CorpusReader.evidence()` / `app.db.repository.evidence_context`), never from the
truncated copy held in graph state. Its text is the stored `content` of the evidence
context chunk: the cited chunk itself, or the parent paragraph a child chunk expands to
(`window_chunk_id` then names the parent). Every window is a contiguous span of that text,
so `content[start:end] == window.text` always holds and a reviewer can find it.

Windows (bounded, deterministic order):
1. every segment (sentence, clause, list item; `app.verification.text.segments`);
2. every pair of adjacent segments;
3. a list lead-in through each later item of the same sentence ("... shall be obtained:
   (a) ...; (b) Trust deed;"), so an obligation stated in the lead-in is read together
   with the item it governs.
A single segment longer than `MAX_WINDOW_CHARS` is cut at a word boundary (it stays
contiguous); a combined window (2 or 3) that would be too long is not built.
At most `MAX_WINDOWS` per premise; a premise that hit the cap says so.

The citation must agree with the stored chunk (id, document, eligibility, reliability,
status, mandatory capability); any disagreement raises `PremiseMismatch` rather than
silently trusting either side.
"""

from dataclasses import dataclass
from typing import Literal

from app.db.repository import EvidenceContext
from app.graph.state import Citation, EvidenceRole, evidence_role
from app.schemas.taxonomy import DocumentType, EvidenceEligibility, SourceStatus, TextReliability
from app.verification.text import Segment, segments

MAX_WINDOW_CHARS = 1000  # well inside a 512-token NLI window with the hypothesis (S4 checks)
MAX_WINDOWS = 40


class PremiseMismatch(ValueError):
    """A citation does not agree with the stored chunk it names."""


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Premise:
    evidence_id: str
    window_chunk_id: str | None  # None: offsets index the cited chunk itself
    document_key: str
    document_type: DocumentType
    page: int
    page_end: int | None
    section: str | None
    evidence_role: EvidenceRole
    evidence_eligibility: EvidenceEligibility
    match_scope: Literal["entity", "entity_group"]
    text_reliability: TextReliability
    source_status: SourceStatus
    can_support_mandatory: bool
    windows: tuple[Window, ...]
    windows_capped: bool


def _cut(text: str, start: int, end: int) -> tuple[int, int]:
    """Shorten [start, end) to MAX_WINDOW_CHARS at a word boundary."""
    if end - start <= MAX_WINDOW_CHARS:
        return start, end
    limit = start + MAX_WINDOW_CHARS
    space = text.rfind(" ", start, limit)
    stop = space if space > start else limit
    while stop > start and text[stop - 1].isspace():
        stop -= 1
    return start, stop


def windows(text: str) -> tuple[tuple[Window, ...], bool]:
    segs = segments(text)
    spans: list[tuple[int, int]] = [(s.start, s.end) for s in segs]
    spans += [(a.start, b.end) for a, b in zip(segs, segs[1:], strict=False)]
    lead: Segment | None = None
    for i, seg in enumerate(segs):
        if seg.lead_in:
            lead = seg
            continue
        if lead is not None and i > 0:
            spans.append((lead.start, seg.end))
        if seg.text.endswith((".", "?", "!")):
            lead = None  # the list's sentence ended
    singles = {(s.start, s.end) for s in segs}
    seen: set[tuple[int, int]] = set()
    out: list[Window] = []
    for start, end in sorted(spans, key=lambda s: (s[0], s[1])):
        if end - start > MAX_WINDOW_CHARS and (start, end) not in singles:
            continue  # a combined window that is too long: its parts are windows already
        start, end = _cut(text, start, end)
        if end <= start or (start, end) in seen:
            continue
        seen.add((start, end))
        out.append(Window(start, end, text[start:end]))
    return tuple(out[:MAX_WINDOWS]), len(out) > MAX_WINDOWS


def build_premise(citation: Citation, ctx: EvidenceContext) -> Premise:
    stored = ctx.retrieved.metadata
    source = ctx.context.metadata
    checks = {
        "evidence_id": (citation.evidence_id, stored.chunk_id),
        "document_key": (citation.document_key, stored.document_key),
        "evidence_eligibility": (citation.evidence_eligibility, stored.evidence_eligibility),
        "text_reliability": (citation.text_reliability, stored.text_reliability),
        "source_status": (citation.source_status, stored.source_status),
        "can_support_mandatory": (citation.can_support_mandatory, stored.can_support_mandatory),
    }
    wrong = sorted(name for name, (cited, kept) in checks.items() if cited != kept)
    if wrong:
        raise PremiseMismatch(f"citation disagrees with the stored chunk on: {', '.join(wrong)}")
    wins, capped = windows(source.content)
    return Premise(
        evidence_id=stored.chunk_id,
        window_chunk_id=source.chunk_id if ctx.expanded else None,
        document_key=stored.document_key,
        document_type=stored.document_type,
        page=citation.page,
        page_end=citation.page_end,
        section=source.section,
        evidence_role=evidence_role(stored.document_type, stored.evidence_eligibility),
        evidence_eligibility=stored.evidence_eligibility,
        match_scope=citation.match_scope,
        text_reliability=stored.text_reliability,
        source_status=stored.source_status,
        can_support_mandatory=stored.can_support_mandatory,
        windows=wins,
        windows_capped=capped,
    )
