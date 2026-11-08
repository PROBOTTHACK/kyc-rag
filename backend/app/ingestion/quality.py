"""Page-quality decisions for Pipeline A (pure functions; no I/O).

OCR trigger (approved in the M1 corpus review): fewer than 50 non-whitespace characters
in the text layer AND images covering more than 80% of the page. Short native pages
(e.g. RBI Directions p55/p74, ~150 chars, no images) are therefore never OCR'd, and
OCR output never replaces a usable native text layer.
"""

from dataclasses import dataclass

from app.ingestion.pdf import PageMetrics
from app.schemas.taxonomy import ExtractionMethod, PageStatus

OCR_MAX_NATIVE_CHARS = 50  # trigger: char_count < this
OCR_MIN_IMAGE_COVERAGE = 0.8  # trigger: image_coverage > this
OCR_OK_CONFIDENCE = 70.0  # mean word confidence (0-100) at or above this is `ok`
FOLD_OUT_ASPECT = 1.5  # wider than this = two printed pages side by side. The corpus
# fold-out sheet is 1.60 (1432x894); A4 landscape (1.41) and Letter landscape (1.29) stay whole.


def needs_ocr(metrics: PageMetrics) -> bool:
    return (
        metrics.char_count < OCR_MAX_NATIVE_CHARS
        and metrics.image_coverage > OCR_MIN_IMAGE_COVERAGE
    )


def is_fold_out(width: float, height: float) -> bool:
    return height > 0 and width / height > FOLD_OUT_ASPECT


@dataclass(frozen=True)
class PageDecision:
    status: PageStatus
    extraction_method: ExtractionMethod | None
    error: str | None = None


def native_decision(metrics: PageMetrics) -> PageDecision:
    """Page that did not need OCR: its text layer is the extraction."""
    if metrics.char_count == 0:
        return PageDecision(
            PageStatus.LOW_QUALITY, None, "no text layer and not image-covered; nothing extracted"
        )
    return PageDecision(PageStatus.OK, ExtractionMethod.TEXT)


def ocr_decision(
    metrics: PageMetrics,
    *,
    ocr_chars: int,
    ocr_confidence: float,
    errors: list[str],
) -> PageDecision:
    """Page that needed OCR. Native text is kept unless OCR produced something better."""
    if errors:
        return PageDecision(PageStatus.FAILED, None, "; ".join(errors))
    if ocr_chars == 0:
        if metrics.char_count:
            return PageDecision(
                PageStatus.LOW_QUALITY, ExtractionMethod.TEXT, "OCR produced no text"
            )
        return PageDecision(PageStatus.FAILED, None, "OCR produced no text")
    if ocr_chars <= metrics.char_count:
        # OCR found no more than the (short) native layer: keep the native text.
        return PageDecision(
            PageStatus.LOW_QUALITY,
            ExtractionMethod.TEXT,
            "OCR did not improve on the native text layer",
        )
    if ocr_confidence >= OCR_OK_CONFIDENCE:
        return PageDecision(PageStatus.OK, ExtractionMethod.OCR)
    return PageDecision(PageStatus.OCR_LOW_CONFIDENCE, ExtractionMethod.OCR)
