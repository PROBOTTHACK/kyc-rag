"""Local Tesseract OCR for pages a multimodal model refused with RECITATION (M2 G3).

Reuses M1's OCR exactly: `ocr_page` (same DPI, PSM, language, fold-out regions) and the
same OCR cache, so a page M1 already OCR'd returns identical text without re-running
Tesseract. The text is kept verbatim with per-line confidence and region; it is never
parsed into form fields and always requires human review. Nothing is sent to any model.
"""

from collections.abc import Callable
from pathlib import Path

import pymupdf

from app.config import Settings
from app.ingestion.ocr import OCR_LANGUAGE, OCR_PSM, OcrSetupError, tesseract_setup
from app.ingestion.pages import OCR_DPI, OcrCache, ocr_page
from app.llm.multimodal import RecitationDiagnostics
from app.schemas.forms import OcrFallbackLine, OcrFallbackPage, OcrFallbackRegion

# (pdf path, 1-based page, recitation diagnostics, verified pdf sha256) -> fallback page
OcrFallbackFn = Callable[[Path, int, RecitationDiagnostics, str], OcrFallbackPage]


def ocr_fallback_page(
    pdf_path: Path,
    page_number: int,
    recitation: RecitationDiagnostics,
    pdf_sha256: str,
    *,
    settings: Settings,
) -> OcrFallbackPage:
    """OCR one page locally. Setup or OCR failures are recorded in `errors`, never hidden
    and never replaced by anything else."""
    base = {"page_number": page_number, "dpi": OCR_DPI, "psm": OCR_PSM, "language": OCR_LANGUAGE}
    try:
        setup = tesseract_setup(settings)
    except OcrSetupError as exc:
        return OcrFallbackPage(
            **base, engine="tesseract (unavailable)", errors=[str(exc)], recitation=recitation
        )
    with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
        regions, errors = ocr_page(
            doc[page_number - 1],
            page_number,
            pdf_sha256=pdf_sha256,
            setup=setup,
            cache=OcrCache(settings.OCR_CACHE_DIR),
        )
    return OcrFallbackPage(
        **base,
        engine=f"tesseract {setup.version}",
        regions=[
            OcrFallbackRegion(
                label=r.label,
                printed_page=r.printed_page,
                mean_confidence=round(r.text.mean_confidence, 2),
                word_count=r.text.word_count,
                from_cache=r.from_cache,
            )
            for r in regions
        ],
        lines=[
            OcrFallbackLine(
                region=r.label,
                text=line.text,
                confidence=round(line.confidence, 2),
                low_confidence=line.low_confidence,
            )
            for r in regions
            for line in r.text.lines
        ],
        errors=errors,
        recitation=recitation,
    )


def ocr_fallback_from_settings(settings: Settings) -> OcrFallbackFn:
    def fn(path: Path, page: int, recitation: RecitationDiagnostics, sha: str) -> OcrFallbackPage:
        return ocr_fallback_page(path, page, recitation, sha, settings=settings)

    return fn
