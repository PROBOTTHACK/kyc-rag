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
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EXTRACTOR_VERSION = 1
EXTRACTION_METHOD: Literal["m1_chunks"] = "m1_chunks"

DocumentRole = Literal["kyc_form", "reference_document"]
# `extracted` = read from the native text layer (not verified by a person);
# `ocr_reliable` / `ocr_uncertain` = OCR text, with M1's reliability verdict.
VerificationStatus = Literal["extracted", "ocr_reliable", "ocr_uncertain"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FormField(_Frozen):
    printed_label: str  # exactly as stored in the M1 chunk, including any `*`
    label: str  # display form: leading `*` marker(s) and surrounding spaces removed
    required_as_shown: bool | None  # see module docstring; never inferred
    marker_issue: str | None = None  # why `required_as_shown` is None
    page_number: int = Field(ge=1)  # physical PDF page of the field chunk
    page_end: int | None = None
    region: str | None = None
    chunk_id: str
    position: int = Field(ge=0)  # order of the label within its chunk
    text_reliability: str
    verification_status: VerificationStatus
    appears_on_form: Literal[True] = True
    mandatory_under_policy: None = None  # never set by extraction


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


class FormSchema(_Frozen):
    form_key: str  # = documents.document_key
    document_id: str
    document_sha256: str
    title: str
    version: str | None
    document_type: str
    document_role: DocumentRole
    evidence_eligibility: str
    quality: str | None
    extraction_method: Literal["m1_chunks"] = EXTRACTION_METHOD
    extractor_version: int = EXTRACTOR_VERSION
    sections: list[FormSection]
    issues: list[ParseIssue] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # sorted, document-level
    chunk_count: int
    field_count: int
    required_as_shown_count: int
    text_block_count: int
