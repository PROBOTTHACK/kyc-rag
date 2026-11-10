"""Deterministic form-schema extraction from the stored M1 chunks (M2 C1; docs/04).

Input: the `documents` row and every `chunks` row of one form (or the context-only
NetBanking reference), in `chunk_index` order. Nothing is re-extracted from the PDF.

- Sections: consecutive chunks with the same M1 section heading form one section, in
  document order (a heading that recurs later starts a new section, so page/chunk
  provenance and ordering are never merged across the document).
- Fields: only native-text `form_fields` chunks in the verified M1 format
  `"Fields: " + " | ".join(labels)` are split, on exactly `" | "`. Each piece is kept
  verbatim as `printed_label`. Only LEADING asterisks are read as a requirement marker:
  exactly one -> `required_as_shown=True`; none anywhere -> False; anything else
  (several leading asterisks, an asterisk elsewhere in the label) -> None with a
  `marker_issue`. Empty pieces are reported as parse issues, never invented.
- Text blocks: every other chunk (headings, declarations, instructions) verbatim, with
  its reliability, OCR confidence and page status. OCR chunks are ALWAYS text blocks,
  even if marked as field lists, and keep their M1 reliability verdict.
- Flags: sections with OCR text and no fields are `no_field_structure`; document-level
  flags summarise scanned / reference / context-only / uncertain-OCR / parse issues.

No timestamps, no randomness: identical stored chunks give identical schemas.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.corpus import chunk_record_from_rows
from app.db.models import Chunk, Document
from app.ingestion.chunking import ChunkRecord
from app.schemas.forms import (
    DocumentRole,
    FormField,
    FormSchema,
    FormSection,
    ParseIssue,
    TextBlock,
    VerificationStatus,
)
from app.schemas.taxonomy import ContentKind, DocumentType, TextReliability

FIELDS_PREFIX = "Fields: "  # verified M1 format (app/ingestion/chunkers/form.py)
FIELDS_SEPARATOR = " | "
DECLARATION_MARKER = "DECLARATION"
ROLES: dict[str, DocumentRole] = {
    DocumentType.FORM.value: "kyc_form",
    DocumentType.REFERENCE.value: "reference_document",
}


class NotAFormError(ValueError):
    """The document is not a form or form-like reference document."""


def parse_label(printed: str) -> tuple[str, bool | None, str | None]:
    """`(label, required_as_shown, marker_issue)` for one printed label.

    Only leading asterisks count. The label keeps any other characters as printed."""
    stripped = printed.strip()
    rest = stripped.lstrip("*")
    leading = len(stripped) - len(rest)
    label = rest.strip()
    issues = []
    if leading > 1:
        issues.append(f"{leading} leading asterisks")
    if "*" in label:
        issues.append("asterisk not at the start of the label")
    if issues:
        return label, None, "; ".join(issues)
    return label, leading == 1, None


def _status(reliability: str) -> VerificationStatus:
    if reliability == TextReliability.OCR_RELIABLE.value:
        return "ocr_reliable"
    if reliability == TextReliability.OCR_UNCERTAIN.value:
        return "ocr_uncertain"
    return "extracted"


def _is_native(rec: ChunkRecord) -> bool:
    return rec.metadata.text_reliability is TextReliability.NATIVE


def _fields(rec: ChunkRecord, issues: list[ParseIssue]) -> list[FormField] | None:
    """Fields of a native field chunk, or None if it is not in the verified format."""
    m = rec.metadata
    if m.content_kind is not ContentKind.FORM_FIELDS or not _is_native(rec):
        return None
    if not m.content.startswith(FIELDS_PREFIX):
        issues.append(ParseIssue(chunk_id=m.chunk_id, page_number=m.page_number,
                                 issue="unexpected_field_chunk_format",
                                 detail="form_fields chunk without the 'Fields: ' prefix; "
                                        "kept as a text block"))  # fmt: skip
        return None
    out = []
    for position, piece in enumerate(m.content[len(FIELDS_PREFIX) :].split(FIELDS_SEPARATOR)):
        label, required, marker_issue = parse_label(piece)
        if not label:
            issues.append(
                ParseIssue(
                    chunk_id=m.chunk_id,
                    page_number=m.page_number,
                    issue="empty_label",
                    detail=f"piece {position} is {piece!r}; not a field",
                )
            )
            continue
        out.append(
            FormField(
                printed_label=piece,
                label=label,
                required_as_shown=required,
                marker_issue=marker_issue,
                page_number=m.page_number,
                page_end=m.page_end,
                region=m.region,
                chunk_id=m.chunk_id,
                position=position,
                text_reliability=m.text_reliability.value,
                verification_status=_status(m.text_reliability.value),
            )
        )
    return out


def _text_block(rec: ChunkRecord) -> TextBlock:
    m = rec.metadata
    return TextBlock(
        chunk_id=m.chunk_id,
        page_number=m.page_number,
        page_end=m.page_end,
        region=m.region,
        content_kind=m.content_kind.value,
        text=m.content,
        text_reliability=m.text_reliability.value,
        ocr_confidence=m.ocr_confidence,
        page_status=m.page_status.value,
        excluded_line_count=len(rec.extras.get("excluded_lines") or []),
        verification_status=_status(m.text_reliability.value),
    )


def build_schema(document: Document, records: Sequence[ChunkRecord]) -> FormSchema:
    """The form schema of `document` from its stored chunk records (any order given)."""
    role = ROLES.get(document.document_type)
    if role is None:
        raise NotAFormError(f"{document.document_key} is a {document.document_type} document")
    ordered = sorted(records, key=lambda r: r.metadata.chunk_index)
    issues: list[ParseIssue] = []
    runs: list[list[ChunkRecord]] = []
    for rec in ordered:  # consecutive chunks with the same heading form one section
        if runs and runs[-1][-1].metadata.section == rec.metadata.section:
            runs[-1].append(rec)
        else:
            runs.append([rec])
    sections = []
    for ordinal, run in enumerate(runs):
        fields: list[FormField] = []
        blocks: list[TextBlock] = []
        for rec in run:
            parsed = _fields(rec, issues)
            if parsed is None:
                if rec.metadata.content_kind is ContentKind.FORM_FIELDS and not _is_native(rec):
                    issues.append(ParseIssue(
                        chunk_id=rec.metadata.chunk_id, page_number=rec.metadata.page_number,
                        issue="ocr_field_list_kept_as_text",
                        detail="OCR text is never turned into structured fields"))  # fmt: skip
                blocks.append(_text_block(rec))
            else:
                fields.extend(parsed)
        name = run[0].metadata.section
        ocr = any(not _is_native(r) for r in run)
        flags = []
        if ocr and not fields:
            flags.append("no_field_structure")
        if any(b.verification_status == "ocr_uncertain" for b in blocks):
            flags.append("ocr_uncertain_text")
        sections.append(
            FormSection(
                ordinal=ordinal,
                name=name,
                page_start=min(r.metadata.page_number for r in run),
                page_end=max(r.metadata.page_end or r.metadata.page_number for r in run),
                regions=sorted({r.metadata.region for r in run if r.metadata.region}),
                is_declaration=DECLARATION_MARKER in (name or "").upper(),
                chunk_ids=[r.metadata.chunk_id for r in run],
                fields=fields,
                text_blocks=blocks,
                flags=flags,
            )
        )
    all_fields = [f for s in sections for f in s.fields]
    doc_flags = set()
    if role == "reference_document":
        doc_flags.add("reference_document")
    if document.evidence_eligibility == "context_only":
        doc_flags.add("context_only")
    if document.quality == "scanned":
        doc_flags.add("scanned_document")
    if not all_fields:
        doc_flags.add("no_field_structure")
    if any("ocr_uncertain_text" in s.flags for s in sections):
        doc_flags.add("ocr_uncertain_text")
    if issues:
        doc_flags.add("parse_issues")
    if any(f.marker_issue for f in all_fields):
        doc_flags.add("ambiguous_required_markers")
    return FormSchema(
        form_key=document.document_key,
        document_id=str(document.id),
        document_sha256=document.sha256,
        title=document.title,
        version=document.version,
        document_type=document.document_type,
        document_role=role,
        evidence_eligibility=document.evidence_eligibility,
        quality=document.quality,
        sections=sections,
        issues=issues,
        flags=sorted(doc_flags),
        chunk_count=len(ordered),
        field_count=len(all_fields),
        required_as_shown_count=sum(f.required_as_shown is True for f in all_fields),
        text_block_count=sum(len(s.text_blocks) for s in sections),
    )


def extract_form(session: Session, document_key: str) -> FormSchema:
    """Read one form's stored rows and build its schema (read-only)."""
    document = session.scalar(select(Document).where(Document.document_key == document_key))
    if document is None:
        raise LookupError(f"document {document_key!r} is not stored")
    rows = session.scalars(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.chunk_index)
    ).all()
    return build_schema(document, [chunk_record_from_rows(c, document) for c in rows])


def extractable_document_keys(session: Session) -> list[str]:
    """Stored forms and form-like reference documents, sorted by key."""
    return list(
        session.scalars(
            select(Document.document_key)
            .where(Document.document_type.in_(list(ROLES)))
            .order_by(Document.document_key)
        )
    )
