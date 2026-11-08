"""Stage B: per-page quality assessment and OCR fallback on top of Stage A extraction.

The Stage A `StructuredPage` (native PyMuPDF text, footnotes, removed-line counts) is
kept untouched; OCR output is stored beside it in `ProcessedPage.ocr_regions`, so the
two can always be compared. Every page returns a `ProcessedPage` with a status, even
when OCR is unavailable or fails (status `failed` + error), so nothing is dropped.

Fold-out pages (e.g. the non-individual form: two printed pages per landscape sheet)
are OCR'd as left and right regions that are never mixed.

OCR results are cached on disk by (PDF sha256, page, region, DPI, PSM, language,
Tesseract version), so re-runs and dry-runs don't repeat OCR. Only successful runs are
cached; an unreadable cache entry is ignored and recomputed.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from app.ingestion.ocr import (
    OCR_LANGUAGE,
    OCR_PSM,
    OcrLine,
    OcrRunError,
    OcrText,
    TesseractSetup,
    run_ocr,
)
from app.ingestion.pdf import open_pdf
from app.ingestion.quality import is_fold_out, native_decision, needs_ocr, ocr_decision
from app.ingestion.structure import ExtractedDocument, StructuredPage, extract_document
from app.schemas.taxonomy import ExtractionMethod, PageStatus

log = logging.getLogger(__name__)

OCR_DPI = 300
OCR_CACHE_FORMAT = 1  # bump when the cached structure changes
PRINTED_PAGE = re.compile(r"\bPage\s+(\d{1,2})\s+of\s+(\d{1,2})\b", re.IGNORECASE)

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class OcrRegion:
    label: str  # "full", or "left"/"right" for fold-out halves
    clip: BBox  # region of the PDF page, in PDF points
    printed_page: int | None  # from "Page N of M" in the region, when OCR found it
    text: OcrText
    from_cache: bool

    @property
    def low_confidence_lines(self) -> int:
        return sum(line.low_confidence for line in self.text.lines)


@dataclass
class ProcessedPage:
    structured: StructuredPage  # Stage A output, unchanged
    status: PageStatus
    extraction_method: ExtractionMethod | None
    ocr_triggered: bool
    ocr_regions: list[OcrRegion] = field(default_factory=list)
    error: str | None = None

    @property
    def page_number(self) -> int:
        return self.structured.page_number

    @property
    def ocr_confidence(self) -> float | None:
        """Mean word confidence across regions, 0-1 (`document_pages.ocr_confidence`)."""
        confs = [c for r in self.ocr_regions for c in r.text.word_confidences]
        return round(sum(confs) / len(confs) / 100, 4) if confs else None

    def metadata(self) -> dict[str, Any]:
        """JSON-serialisable page record (stored in `document_pages.metadata`)."""
        meta: dict[str, Any] = {
            "removed_lines": dict(self.structured.content.removed),
            "page_label": self.structured.page_label,
            "footnotes": len(self.structured.footnotes),
            "ocr_triggered": self.ocr_triggered,
        }
        if self.ocr_triggered:
            meta["ocr"] = {
                "dpi": OCR_DPI,
                "psm": OCR_PSM,
                "language": OCR_LANGUAGE,
                "regions": [
                    {
                        "label": r.label,
                        "printed_page": r.printed_page,
                        "words": r.text.word_count,
                        "lines": len(r.text.lines),
                        "mean_confidence": round(r.text.mean_confidence, 2),
                        "low_confidence_lines": r.low_confidence_lines,
                        "from_cache": r.from_cache,
                    }
                    for r in self.ocr_regions
                ],
            }
        return meta


# --- cache -------------------------------------------------------------------


class OcrCache:
    def __init__(self, root: Path | None) -> None:
        self.root = root  # None disables caching

    @staticmethod
    def key(pdf_sha256: str, page_number: int, label: str, clip: BBox, version: str) -> str:
        clip_s = ",".join(f"{v:.2f}" for v in clip)
        raw = (
            f"{OCR_CACHE_FORMAT}|{pdf_sha256}|{page_number}|{label}|{clip_s}|"
            f"{OCR_DPI}|{OCR_PSM}|{OCR_LANGUAGE}|{version}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _path(root: Path, key: str) -> Path:
        return root / key[:2] / f"{key}.json"

    def get(self, key: str) -> OcrText | None:
        if self.root is None:
            return None
        path = self._path(self.root, key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            lines = tuple(OcrLine(**{**ln, "bbox": tuple(ln["bbox"])}) for ln in data["lines"])
            return OcrText(lines=lines, word_confidences=tuple(data["word_confidences"]))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable OCR cache entry %s: %s", path.name, type(exc).__name__)
            return None

    def put(self, key: str, text: OcrText) -> None:
        if self.root is None:
            return
        path = self._path(self.root, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lines": [asdict(line) for line in text.lines],
            "word_confidences": list(text.word_confidences),
        }
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves a half-written entry


# --- processing --------------------------------------------------------------


def _regions(page: pymupdf.Page) -> list[tuple[str, BBox]]:
    r = page.rect
    if is_fold_out(r.width, r.height):
        mid = r.x0 + r.width / 2
        return [("left", (r.x0, r.y0, mid, r.y1)), ("right", (mid, r.y0, r.x1, r.y1))]
    return [("full", (r.x0, r.y0, r.x1, r.y1))]


def _printed_page(text: OcrText) -> int | None:
    match = PRINTED_PAGE.search(text.text())
    return int(match.group(1)) if match else None


def ocr_page(
    page: pymupdf.Page,
    page_number: int,
    *,
    pdf_sha256: str,
    setup: TesseractSetup,
    cache: OcrCache,
) -> tuple[list[OcrRegion], list[str]]:
    regions: list[OcrRegion] = []
    errors: list[str] = []
    for label, clip in _regions(page):
        key = OcrCache.key(pdf_sha256, page_number, label, clip, setup.version)
        cached = cache.get(key)
        if cached is not None:
            text, from_cache = cached, True
        else:
            try:
                png = page.get_pixmap(dpi=OCR_DPI, clip=pymupdf.Rect(clip)).tobytes("png")
                text = run_ocr(setup, png, OCR_DPI)
            except (OcrRunError, RuntimeError, ValueError) as exc:
                errors.append(f"OCR failed for region {label}: {exc}")
                continue
            from_cache = False
            if text.word_count:
                cache.put(key, text)
        regions.append(
            OcrRegion(
                label=label,
                clip=clip,
                printed_page=_printed_page(text),
                text=text,
                from_cache=from_cache,
            )
        )
    return regions, errors


def process_page(
    structured: StructuredPage,
    page: pymupdf.Page,
    *,
    pdf_sha256: str,
    setup: TesseractSetup | None,
    setup_error: str | None,
    cache: OcrCache,
) -> ProcessedPage:
    metrics = structured.content.metrics
    if not needs_ocr(metrics):
        d = native_decision(metrics)
        return ProcessedPage(structured, d.status, d.extraction_method, False, error=d.error)
    if setup is None:
        error = f"OCR needed but Tesseract is unavailable: {setup_error or 'not configured'}"
        return ProcessedPage(structured, PageStatus.FAILED, None, True, error=error)
    regions, errors = ocr_page(
        page, structured.page_number, pdf_sha256=pdf_sha256, setup=setup, cache=cache
    )
    ocr_chars = sum(len(r.text.text().replace(" ", "").replace("\n", "")) for r in regions)
    confs = [c for r in regions for c in r.text.word_confidences]
    d = ocr_decision(
        metrics,
        ocr_chars=ocr_chars,
        ocr_confidence=sum(confs) / len(confs) if confs else 0.0,
        errors=errors,
    )
    return ProcessedPage(
        structured, d.status, d.extraction_method, True, ocr_regions=regions, error=d.error
    )


def process_document(
    path: Path,
    *,
    pdf_sha256: str,
    setup: TesseractSetup | None,
    setup_error: str | None = None,
    cache: OcrCache,
    detect_tables: bool = False,
) -> tuple[ExtractedDocument, list[ProcessedPage]]:
    """Stage A extraction + Stage B quality/OCR for every page of one PDF."""
    extracted = extract_document(path, detect_tables=detect_tables)
    processed = process_pages(
        path, extracted, pdf_sha256=pdf_sha256, setup=setup, setup_error=setup_error, cache=cache
    )
    return extracted, processed


def process_pages(
    path: Path,
    extracted: ExtractedDocument,
    *,
    pdf_sha256: str,
    setup: TesseractSetup | None,
    setup_error: str | None = None,
    cache: OcrCache,
) -> list[ProcessedPage]:
    """Stage B for every page of an extracted PDF (split out so callers can tell an
    extraction failure from an OCR failure)."""
    doc = open_pdf(path)  # rendering for OCR needs live page objects
    try:
        processed = [
            process_page(
                sp,
                doc[sp.page_number - 1],
                pdf_sha256=pdf_sha256,
                setup=setup,
                setup_error=setup_error,
                cache=cache,
            )
            for sp in extracted.pages
        ]
    finally:
        doc.close()
    for p in processed:
        if p.status in (PageStatus.FAILED, PageStatus.LOW_QUALITY, PageStatus.OCR_LOW_CONFIDENCE):
            log.warning(
                "%s p%d: %s%s", path.name, p.page_number, p.status.value,
                f" ({p.error})" if p.error else "",
            )  # fmt: skip
    return processed
