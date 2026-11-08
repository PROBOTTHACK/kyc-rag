"""PDF validation and per-page extraction with PyMuPDF (Pipeline A, docs/04).

Every physical page yields one `PageContent`, including empty and image-only pages:
pages are never dropped here. Page numbers are 1-based physical PDF pages, which is
what all citations use (data/processed/corpus_report.md: printed = physical for 169MD).
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

PDF_MAGIC = b"%PDF-"
SUPERSCRIPT_FLAG = 1  # PyMuPDF span flag bit 0


class PdfValidationError(Exception):
    """The file cannot be processed as a PDF (bad header, encrypted, empty, unreadable)."""


@dataclass(frozen=True)
class Span:
    text: str
    size: float
    flags: int
    font: str
    bbox: tuple[float, float, float, float]

    @property
    def is_superscript(self) -> bool:
        return bool(self.flags & SUPERSCRIPT_FLAG)


@dataclass(frozen=True)
class Line:
    spans: tuple[Span, ...]
    bbox: tuple[float, float, float, float]
    block: int
    # Footnote markers that were removed from this line's spans (set by structure.py).
    footnote_refs: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:
        """Dominant font size, weighted by characters."""
        weights: dict[float, int] = {}
        for s in self.spans:
            n = len(s.text.strip())
            if n:
                weights[s.size] = weights.get(s.size, 0) + n
        return max(weights, key=lambda k: weights[k]) if weights else 0.0

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]


@dataclass(frozen=True)
class PageMetrics:
    char_count: int  # non-whitespace characters in the text layer
    alpha_ratio: float  # letters / non-whitespace characters (0 when no text)
    image_coverage: float  # share of the page covered by images, capped at 1
    n_images: int


@dataclass
class PageContent:
    page_number: int  # 1-based physical page
    width: float
    height: float
    rotation: int
    lines: list[Line]
    metrics: PageMetrics
    raw_text: str
    # Lines taken out of the body by structure.py, counted by reason (never silent).
    removed: dict[str, int] = field(default_factory=dict)


def validate_pdf(path: Path) -> None:
    """Cheap checks before PyMuPDF parsing; raises PdfValidationError."""
    if not path.is_file():
        raise PdfValidationError(f"not a file: {path}")
    with path.open("rb") as f:
        head = f.read(1024)
    if PDF_MAGIC not in head:
        raise PdfValidationError(f"missing %PDF header: {path.name}")


def open_pdf(path: Path) -> pymupdf.Document:
    validate_pdf(path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # PyMuPDF raises several types for corrupt files
        raise PdfValidationError(f"cannot open {path.name}: {type(exc).__name__}") from exc
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise PdfValidationError(f"encrypted PDF is not supported: {path.name}")
    if doc.page_count == 0:
        doc.close()
        raise PdfValidationError(f"PDF has no pages: {path.name}")
    return doc


def text_metrics(text: str) -> tuple[int, float]:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0, 0.0
    return len(chars), sum(c.isalpha() for c in chars) / len(chars)


def image_coverage(page: pymupdf.Page) -> tuple[float, int]:
    page_rect = page.rect
    area = page_rect.width * page_rect.height
    infos = page.get_image_info()
    covered = 0.0
    for info in infos:
        r = pymupdf.Rect(info["bbox"]) & page_rect
        if not r.is_empty:
            covered += r.width * r.height
    return (min(1.0, covered / area) if area else 0.0), len(infos)


BBox = tuple[float, float, float, float]


def _bbox(values: Sequence[float]) -> BBox:
    x0, y0, x1, y1 = (float(v) for v in values)
    return (x0, y0, x1, y1)


def _lines(page: pymupdf.Page) -> list[Line]:
    out: list[Line] = []
    for b_index, block in enumerate(page.get_text("dict")["blocks"]):
        for line in block.get("lines", []):
            spans = tuple(
                Span(
                    text=s["text"],
                    size=round(float(s["size"]), 1),
                    flags=int(s["flags"]),
                    font=str(s["font"]),
                    bbox=_bbox(s["bbox"]),
                )
                for s in line["spans"]
            )
            if any(s.text.strip() for s in spans):
                out.append(Line(spans=spans, bbox=_bbox(line["bbox"]), block=b_index))
    return out


def read_page(page: pymupdf.Page, page_number: int) -> PageContent:
    raw = page.get_text("text")
    chars, alpha = text_metrics(raw)
    coverage, n_images = image_coverage(page)
    return PageContent(
        page_number=page_number,
        width=page.rect.width,
        height=page.rect.height,
        rotation=page.rotation,
        lines=_lines(page),
        metrics=PageMetrics(
            char_count=chars,
            alpha_ratio=round(alpha, 4),
            image_coverage=round(coverage, 4),
            n_images=n_images,
        ),
        raw_text=raw,
    )


def iter_pages(doc: pymupdf.Document) -> Iterator[PageContent]:
    for index in range(doc.page_count):
        yield read_page(doc[index], index + 1)
