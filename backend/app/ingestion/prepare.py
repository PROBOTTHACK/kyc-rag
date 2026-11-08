"""Stages A-C for one manifest entry: extraction, quality/OCR, chunking.

Shared by the CLI, the ingestion pipeline and tests so every caller gets the same
profile-specific settings (table detection only for `rbi_directions`) and the
document's OCR evidence policy.

`prepare` keeps the processed pages (for `document_pages`) and attributes an exception
to the stage that raised it (`StageFailure`); `prepare_document` is the Stage D entry
point and returns only the chunks.
"""

from dataclasses import dataclass

from app.config import Settings
from app.ingestion.chunking import ChunkingResult, OcrEvidencePolicy, chunk_document
from app.ingestion.ocr import OcrSetupError, TesseractSetup, tesseract_setup
from app.ingestion.pages import OcrCache, ProcessedPage, process_pages
from app.ingestion.structure import extract_document
from app.schemas.corpus import DocumentMetadata
from app.schemas.taxonomy import ChunkingProfile, IngestionStage, PageStatus


class StageFailure(RuntimeError):
    """A pipeline stage raised; `stage` says which one (recorded in `ingestion_runs`)."""

    def __init__(self, stage: IngestionStage, error: BaseException) -> None:
        super().__init__(f"{stage.value} failed: {type(error).__name__}: {error}")
        self.stage = stage
        self.error = error


@dataclass
class PreparedDocument:
    entry: DocumentMetadata
    pages: list[ProcessedPage]
    result: ChunkingResult

    @property
    def failed_pages(self) -> list[ProcessedPage]:
        """Pages with no usable reading (e.g. OCR error or Tesseract unavailable)."""
        return [p for p in self.pages if p.status is PageStatus.FAILED]


def ocr_setup(settings: Settings) -> tuple[TesseractSetup | None, str | None]:
    """Tesseract, or None plus the reason (scanned pages then fail explicitly)."""
    try:
        return tesseract_setup(settings), None
    except OcrSetupError as exc:
        return None, str(exc)


def prepare(
    entry: DocumentMetadata,
    settings: Settings,
    *,
    setup: TesseractSetup | None,
    setup_error: str | None = None,
    cache: OcrCache | None = None,
) -> PreparedDocument:
    """Stages A-C. Raises `StageFailure` naming the stage that raised.

    A page that fails OCR does not raise: it is returned with status `failed` and its
    error (Stage B), and the caller records it.
    """
    path = settings.CORPUS_DIR / entry.file
    profile = entry.effective_chunking_profile
    try:
        extracted = extract_document(path, detect_tables=profile is ChunkingProfile.RBI_DIRECTIONS)
    except Exception as exc:
        raise StageFailure(IngestionStage.EXTRACTION, exc) from exc
    try:
        pages = process_pages(
            path,
            extracted,
            pdf_sha256=entry.sha256,
            setup=setup,
            setup_error=setup_error,
            cache=cache if cache is not None else OcrCache(settings.OCR_CACHE_DIR),
        )
    except Exception as exc:
        raise StageFailure(IngestionStage.OCR, exc) from exc
    try:
        result = chunk_document(
            entry, extracted, pages, OcrEvidencePolicy.for_document(entry, settings)
        )
    except Exception as exc:
        raise StageFailure(IngestionStage.CHUNKING, exc) from exc
    return PreparedDocument(entry=entry, pages=pages, result=result)


def prepare_document(
    entry: DocumentMetadata,
    settings: Settings,
    *,
    setup: TesseractSetup | None,
    setup_error: str | None = None,
    cache: OcrCache | None = None,
) -> ChunkingResult:
    """Stages A-C, chunks only (the Stage D `index` command); raises the original error."""
    try:
        prepared = prepare(entry, settings, setup=setup, setup_error=setup_error, cache=cache)
    except StageFailure as exc:
        raise exc.error from None
    return prepared.result
