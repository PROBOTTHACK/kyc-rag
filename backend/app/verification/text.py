"""Deterministic text segmentation with character offsets (M4 S2; M4_PLAN §D.3, §F).

One segmenter serves both sides of verification:
- a model explanation is split into verbatim sentences (explanatory claims);
- a stored evidence passage is split into segments whose offsets point back into the
  stored text, so every judgement names its document, page and exact character span.

Segments end at sentence punctuation (. ! ?), at ";" and at ":" (a list lead-in such as
"the following documents shall be obtained:"), and before list markers such as "(a)",
"(iv)" or "b)". Abbreviations and numbers common in the corpus never end a sentence:
"Form No. 60", "Rs. 50,000", "e.g.", "para 16.1", "i.e.".

No model, no I/O. Limitations (documented, measured in S3/S4): rule-based splitting can
merge or split clauses imperfectly; that only affects which windows are scored, never
what any verdict is allowed to conclude.
"""

import re
from dataclasses import dataclass

# Tokens after which a "." is not a sentence end (compared case-insensitively, without
# the dot). Single letters cover initials and list labels such as "a." in "sub-para a.".
ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "no", "nos", "rs", "re", "e.g", "i.e", "etc", "viz", "vs", "ltd", "pvt", "co",
        "para", "paras", "sr", "dr", "mr", "mrs", "ms", "st", "w.e.f", "approx", "art",
        "sec", "cl", "govt", "dept", "fig", "vol", "ch", "u/s", "a.k.a", "inc",
    }
)  # fmt: skip

_BOUNDARY = re.compile(r"[.!?;:]")
_LIST_MARKER = re.compile(
    r"(?<!\w)(?:\((?:[a-z]{1,2}|[ivx]{1,5}|\d{1,2})\)|(?:[a-z]|[ivx]{1,5})\))\s"
)
_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z./]*)$")
_CONNECTORS = frozenset({"and", "or", "and/or", "and / or"})
# A paragraph or question label on its own: "35.", "16.1.", "Q6.", "(a)", "iv)".
_LABEL_ONLY = re.compile(r"^(?:Q?\d+(?:\.\d+)*\.?|\(?[a-z]{1,2}\)|\(?[ivx]{1,5}\))$", re.IGNORECASE)


@dataclass(frozen=True)
class Segment:
    """A stripped span of `text`: `text[start:end] == segment`."""

    start: int
    end: int
    text: str
    lead_in: bool  # ends with ":" (introduces a list or a set of conditions)


def _is_sentence_end(text: str, i: int) -> bool:
    ch = text[i]
    if ch in ";:!?":
        return ch != ":" or not (i + 1 < len(text) and text[i + 1].isdigit())  # "10:30"
    # "." : not inside a number ("16.1", "1.5"), not after a protected abbreviation,
    # and only when followed by whitespace or the end of text.
    if i + 1 < len(text) and not text[i + 1].isspace():
        return False
    before = text[:i]
    if before and before[-1].isdigit() and i + 1 < len(text):
        nxt = text[i + 1 :].lstrip()
        if nxt[:1].isdigit():
            return False
    m = _WORD_BEFORE.search(before)
    if m:
        word = m.group(1).lower().rstrip(".")
        if word in ABBREVIATIONS or len(word) == 1:
            return False
    return True


def _cut_points(text: str) -> list[int]:
    """Offsets where a new segment starts."""
    cuts: set[int] = set()
    for m in _BOUNDARY.finditer(text):
        if _is_sentence_end(text, m.start()):
            cuts.add(m.end())
    for m in _LIST_MARKER.finditer(text):
        cuts.add(m.start())
    return sorted(c for c in cuts if 0 < c < len(text))


def segments(text: str) -> list[Segment]:
    """Non-empty, stripped segments of `text` with offsets into `text`, in order."""
    out: list[Segment] = []
    bounds = [0, *_cut_points(text), len(text)]
    pending: int | None = None  # start of a label-only segment waiting for its text
    for a, b in zip(bounds, bounds[1:], strict=False):
        raw = text[a:b]
        stripped = raw.strip()
        if not stripped or not any(c.isalnum() for c in stripped):
            continue
        start = a + (len(raw) - len(raw.lstrip()))
        end = start + len(stripped)
        if _LABEL_ONLY.match(stripped) or stripped.lower() in _CONNECTORS:
            pending = start if pending is None else pending
            continue
        if pending is not None:
            start, pending = pending, None
        seg = text[start:end]
        out.append(Segment(start, end, seg, seg.endswith(":")))
    if pending is not None:  # a trailing label: keep it rather than lose text
        seg = text[pending:].strip()
        out.append(Segment(pending, pending + len(seg), seg, False))
    return out


def sentences(text: str) -> list[str]:
    """Verbatim sentences for claims: like `segments`, but list items stay with their
    sentence and ":" does not split, so a claim keeps its own wording."""
    out: list[str] = []
    for seg in segments(text):
        continues = seg.text[:1].islower() or _LIST_MARKER.match(seg.text + " ")
        if out and (continues or out[-1].endswith(":")):
            out[-1] = f"{out[-1]} {seg.text}"
        else:
            out.append(seg.text)
    return out
