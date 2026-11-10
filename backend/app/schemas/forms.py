"""Deterministic KYC form schemas built from stored M1 chunks (M2 C1; docs/04).

These describe what a form SHOWS, never what policy REQUIRES:
- every field `appears_on_form`; `mandatory_under_policy` is always `None` here and can
  only ever be set by a later rules/verification layer, never by extraction;
- `required_as_shown` is True only when the printed label carries one leading `*`; it is
  False when the label has no asterisk at all, and None (with `marker_issue`) when the
  marker is ambiguous or malformed;
- OCR text is kept verbatim as text blocks with its reliability and confidence. It is
  never turned into structured fields and never marked verified.

Extraction is deterministic and timestamp-free: identical stored chunks give identical
schemas (`extractor_version` changes whenever the output for the same input would).

Pipeline B (multimodal, `extraction_method="gemini_multimodal"`) reuses these models. Its
extra keys (`field_type`, `options`, `provenance`) are omitted from the serialised JSON
when unset, so `m1_chunks` schemas (and their stored hashes) are unchanged. Its fields
are `corroborated` only when the label is found in that page's stored M1 text, else
`uncorroborated`: model output is a claim, never verified by itself.
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from app.llm.multimodal import RecitationDiagnostics

EXTRACTOR_VERSION = 1
EXTRACTION_METHOD: Literal["m1_chunks"] = "m1_chunks"
MULTIMODAL_METHOD: Literal["gemini_multimodal"] = "gemini_multimodal"
ExtractionMethod = Literal["m1_chunks", "gemini_multimodal"]
FieldType = Literal["text", "date", "number", "checkbox", "signature", "photo", "table", "other"]

DocumentRole = Literal["kyc_form", "reference_document"]
# `extracted` = read from the native text layer (not verified by a person);
# `ocr_reliable` / `ocr_uncertain` = OCR text, with M1's reliability verdict.
# `corroborated` / `uncorroborated` = multimodal output whose label was / was not found in
# that page's stored M1 text (corroboration shows it appears on the form, nothing more).
VerificationStatus = Literal[
    "extracted", "ocr_reliable", "ocr_uncertain", "corroborated", "uncorroborated"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Optional extension keys dropped from the serialised form when None or empty.
    _omit_when_unset: ClassVar[tuple[str, ...]] = ()

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            for key in self._omit_when_unset:
                if data.get(key) in (None, [], {}):
                    data.pop(key, None)
        return data


class FormField(_Frozen):
    _omit_when_unset = ("field_type", "options")

    printed_label: str  # exactly as stored in the M1 chunk, including any `*`
    label: str  # display form: leading `*` marker(s) and surrounding spaces removed
    required_as_shown: bool | None  # see module docstring; never inferred
    marker_issue: str | None = None  # why `required_as_shown` is None
    page_number: int = Field(ge=1)  # physical PDF page of the field chunk
    page_end: int | None = None
    region: str | None = None
    # M1 chunk holding the label; for multimodal fields, the chunk that corroborates it
    # (None when uncorroborated).
    chunk_id: str | None
    position: int = Field(ge=0)  # order of the label within its chunk (or its section)
    text_reliability: str
    verification_status: VerificationStatus
    appears_on_form: Literal[True] = True
    mandatory_under_policy: None = None  # never set by extraction
    field_type: FieldType | None = None  # multimodal only
    options: list[str] = Field(default_factory=list)  # printed choices (multimodal only)


class TextBlock(_Frozen):
    chunk_id: str
    page_number: int = Field(ge=1)
    page_end: int | None = None
    region: str | None = None
    content_kind: str
    text: str  # the stored chunk content, verbatim
    text_reliability: str
    ocr_confidence: float | None = None
    page_status: str
    excluded_line_count: int = 0  # low-confidence OCR lines M1 left out of `text`
    verification_status: VerificationStatus


class FormSection(_Frozen):
    ordinal: int = Field(ge=0)  # order of appearance in the document
    name: str | None  # the M1 section heading (None = before any heading)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    regions: list[str] = Field(default_factory=list)
    is_declaration: bool
    chunk_ids: list[str]  # every chunk in this section, in document order
    fields: list[FormField] = Field(default_factory=list)
    text_blocks: list[TextBlock] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # e.g. no_field_structure


class ParseIssue(_Frozen):
    chunk_id: str
    page_number: int
    issue: str  # e.g. empty_label, unexpected_field_chunk_format
    detail: str


PageOutcomeKind = Literal["extracted", "invalid_output", "recitation_refused"]


class PageProvenance(_Frozen):
    _omit_when_unset = ("model_id", "outcome")

    page_number: int = Field(ge=1)
    png_sha256: str
    model_id: str | None = None  # the model that answered this page (primary or fallback)
    outcome: PageOutcomeKind | None = None  # unset in rows stored before it existed


# --- Local OCR fallback for pages the model refused with RECITATION -------------------
# Verbatim Tesseract output (M1's OCR, same cache), kept SEPARATE from `sections`: it is
# never parsed into FormField objects, carries no `mandatory_under_policy`, and always
# requires human review.


class OcrFallbackRegion(_Frozen):
    label: str  # "full", or "left"/"right" for fold-out halves (never mixed)
    printed_page: int | None
    mean_confidence: float  # mean word confidence, 0-100
    word_count: int = Field(ge=0)
    from_cache: bool


class OcrFallbackLine(_Frozen):
    region: str
    text: str  # exactly as Tesseract returned it
    confidence: float  # mean word confidence of the line, 0-100
    low_confidence: bool


class OcrFallbackPage(_Frozen):
    page_number: int = Field(ge=1)
    source: Literal["local_tesseract_ocr"] = "local_tesseract_ocr"
    reason: Literal["recitation_refused"] = "recitation_refused"
    verification_status: Literal["unverified_ocr"] = "unverified_ocr"
    review_status: Literal["requires_human_review"] = "requires_human_review"
    engine: str  # e.g. "tesseract 5.x"
    dpi: int
    psm: int
    language: str
    regions: list[OcrFallbackRegion] = Field(default_factory=list)
    lines: list[OcrFallbackLine] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)  # OCR problems, never hidden
    recitation: RecitationDiagnostics


class ExtractionProvenance(_Frozen):
    """How a multimodal schema was produced (no secrets, no timestamps)."""

    provider: str
    model_id: str
    prompt_id: str
    prompt_version: str
    render_dpi: int
    pages: list[PageProvenance]
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)  # provider calls made (validation retry included)
    validation_errors: list[str] = Field(default_factory=list)  # from the failed attempts


class FormSchema(_Frozen):
    _omit_when_unset = ("provenance", "ocr_fallback")

    form_key: str  # = documents.document_key
    document_id: str
    document_sha256: str
    title: str
    version: str | None
    document_type: str
    document_role: DocumentRole
    evidence_eligibility: str
    quality: str | None
    extraction_method: ExtractionMethod = EXTRACTION_METHOD
    extractor_version: int = EXTRACTOR_VERSION
    sections: list[FormSection]
    issues: list[ParseIssue] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # sorted, document-level
    chunk_count: int
    field_count: int
    required_as_shown_count: int
    text_block_count: int
    provenance: ExtractionProvenance | None = None  # multimodal only
    # Pages the model refused (RECITATION): local OCR text for human review, never fields.
    ocr_fallback: list[OcrFallbackPage] = Field(default_factory=list)


# --- Multimodal structured output (what the model returns; validated before use) -------
# Deliberately small: no `mandatory_under_policy`, no applicability or legal judgment.


class ExtractedField(_Frozen):
    printed_label: str = Field(min_length=1)  # exactly as printed, including any `*`
    field_type: FieldType
    options: list[str] = Field(default_factory=list)
    page_number: int = Field(ge=1)


class ExtractedSection(_Frozen):
    name: str | None
    is_declaration: bool
    fields: list[ExtractedField]


class FormExtraction(_Frozen):
    sections: list[ExtractedSection]


def extraction_errors(extraction: FormExtraction, sent_pages: set[int]) -> list[str]:
    """Structural defects beyond the schema: pages not sent, blank or duplicate labels."""
    errors: list[str] = []
    seen: set[tuple[int, str | None, str]] = set()
    for si, sec in enumerate(extraction.sections):
        for fi, f in enumerate(sec.fields):
            where = f"sections.{si}.fields.{fi}"
            if f.page_number not in sent_pages:
                errors.append(f"{where}: page {f.page_number} was not sent")
            if not f.printed_label.strip():
                errors.append(f"{where}: blank label")
            key = (f.page_number, sec.name, f.printed_label.strip())
            if key in seen:
                errors.append(f"{where}: duplicate label on the same page and section")
            seen.add(key)
    return errors
