"""Multimodal (Gemini) form extraction pipeline (M2 Pipeline B, G3; docs/04).

Sequence, per document: approval check -> PDF hash check -> page render -> provider (one
page per request) -> Pydantic + structural validation (one re-ask on invalid output) ->
corroboration against the stored M1 text of that page -> `FormSchema` -> Postgres.

Guarantees:
- Only documents on the approved-forms list, with a SHA-256 equal to the list, the
  manifest and `documents.sha256`, are rendered or sent (refused before rendering).
- A stored schema under the same identity (document, `gemini_multimodal`, extractor
  version) means NO provider call: the stored row is reported as `unchanged`.
- Model output is a claim. A field is `corroborated` only when its label is found in that
  page's stored M1 chunk text (and then carries that chunk's id), else `uncorroborated`.
  `mandatory_under_policy` is never set; `required_as_shown` comes only from a printed
  leading `*` (same rule as the deterministic path).
- Invalid output after the single re-ask is never repaired or stored: that page
  contributes nothing and the schema is flagged `partial_extraction` and
  `invalid_output_page_N`. A provider error (refusal, rate limit, timeout, outage)
  aborts the document: nothing is stored.
- The deterministic `m1_chunks` schemas are a separate extraction method and untouched.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.forms import SaveOutcome, save_form_schema, stored_form_schema_row
from app.db.models import Chunk, Document
from app.forms.approval import ApprovedForm, check_approved
from app.forms.extract import ROLES, parse_label
from app.forms.render import RENDER_DPI, render_pages
from app.ingestion.manifest import load_manifest
from app.llm.multimodal import MultimodalProvider, MultimodalRequest
from app.schemas.forms import (
    MULTIMODAL_METHOD,
    ExtractionProvenance,
    FormExtraction,
    FormField,
    FormSchema,
    FormSection,
    PageProvenance,
    extraction_errors,
)

GEMINI_EXTRACTOR_VERSION = 1  # bump when the prompt, schema or model changes
PROMPT_ID = "form_extraction"
PROMPT_VERSION = "1"
MIN_MATCH_CHARS = 3  # labels shorter than this (normalised) are never "corroborated"

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def instruction(page_number: int) -> str:
    return (
        f"The image is physical page {page_number} of a blank, publicly available bank "
        "form. Extract its section headings and every field label exactly as printed "
        "(keep any leading '*'), the kind of input each field expects, and any printed "
        f"options. Set page_number to {page_number} for every field. Mark a section as a "
        "declaration only if its heading or text says it is one. Return only the JSON."
    )


def reask(page_number: int, errors: list[str]) -> str:
    listed = "; ".join(errors[:10])
    return (
        instruction(page_number)
        + f" Your previous output was rejected by validation ({listed}). Return JSON that "
        "matches the schema exactly."
    )


def normalise(text: str) -> str:
    """NFKC, casefold, every run of non-alphanumerics -> one space."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _NON_ALNUM.sub(" ", folded).strip()


@dataclass(frozen=True)
class PageText:
    chunk_id: str
    text: str  # normalised, space-padded for whole-word matching
    reliability: str


def corroborate(label: str, page_texts: list[PageText]) -> PageText | None:
    """The first stored chunk on the page whose text contains the label as whole words."""
    needle = normalise(label.lstrip("*"))
    if len(needle.replace(" ", "")) < MIN_MATCH_CHARS:
        return None
    padded = f" {needle} "
    return next((p for p in page_texts if padded in p.text), None)


@dataclass
class Prepared:
    document: Document
    approval: ApprovedForm
    pdf_path: Path
    page_texts: dict[int, list[PageText]]  # physical page -> stored M1 chunk texts


@dataclass
class PageOutcome:
    page_number: int
    png_sha256: str
    extraction: FormExtraction | None  # None = invalid after the re-ask
    attempts: int  # requests made by the pipeline for this page (1 or 2)
    api_attempts: int  # provider-level attempts (transport retries included)
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)


def prepare(
    session: Session,
    document_key: str,
    *,
    approvals: dict[str, ApprovedForm],
    corpus_dir: Path,
    manifest_path: Path,
) -> Prepared:
    """Read-only checks and inputs; raises `NotApproved` / `LookupError` before rendering."""
    document = session.scalar(select(Document).where(Document.document_key == document_key))
    if document is None:
        raise LookupError(f"document {document_key!r} is not stored")
    entry = next((e for e in load_manifest(manifest_path) if e.document_key == document_key), None)
    if entry is None:
        raise LookupError(f"document {document_key!r} is not in the manifest")
    approval = check_approved(
        approvals,
        document_key=document_key,
        document_type=document.document_type,
        manifest_sha256=entry.sha256,
        stored_sha256=document.sha256,
    )
    chunks = session.scalars(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.chunk_index)
    ).all()
    page_texts: dict[int, list[PageText]] = {}
    # Child/standalone chunks first, then parents (a parent repeats its children's text).
    for c in sorted(chunks, key=lambda c: (c.chunk_role == "parent", c.chunk_index)):
        for page in range(c.page_number, (c.page_end or c.page_number) + 1):
            page_texts.setdefault(page, []).append(
                PageText(str(c.id), f" {normalise(c.content)} ", c.text_reliability)
            )
    return Prepared(document, approval, corpus_dir / entry.file, page_texts)


async def extract_pages(
    prepared: Prepared, provider: MultimodalProvider, *, dpi: int = RENDER_DPI
) -> list[PageOutcome]:
    """One request per page; one re-ask on invalid output. Provider errors propagate."""
    pages = render_pages(
        prepared.pdf_path,
        document_key=prepared.document.document_key,
        expected_sha256=prepared.approval.sha256,
        dpi=dpi,
    )
    outcomes: list[PageOutcome] = []
    for page in pages:
        n = page.image.page_number
        before = getattr(provider, "attempts_made", 0)
        out = PageOutcome(n, page.image.png_sha256, None, attempts=0, api_attempts=0)
        prompt = instruction(n)
        for _ in range(2):  # the first request plus at most one re-ask
            out.attempts += 1
            req = MultimodalRequest(
                prompt_id=PROMPT_ID,
                prompt_version=PROMPT_VERSION,
                instruction=prompt,
                pages=[page.image],
            )
            result = await provider.analyze_pages([page.png], req, FormExtraction)
            out.latency_ms += result.latency_ms
            if result.usage is not None:
                out.input_tokens += result.usage.input_tokens or 0
                out.output_tokens += result.usage.output_tokens or 0
            errors = list(result.validation_errors)
            if result.parsed is not None:
                errors += extraction_errors(result.parsed, sent_pages={n})
            if not errors:
                out.extraction = result.parsed
                break
            out.errors += [f"p{n} attempt {out.attempts}: {e}" for e in errors]
            prompt = reask(n, errors)
        out.api_attempts = getattr(provider, "attempts_made", 0) - before
        outcomes.append(out)
    return outcomes


def build_schema(
    prepared: Prepared,
    outcomes: list[PageOutcome],
    *,
    provider: MultimodalProvider,
    dpi: int = RENDER_DPI,
    extractor_version: int = GEMINI_EXTRACTOR_VERSION,
) -> FormSchema:
    """Map validated page outputs to a `FormSchema` (pure; no I/O)."""
    doc = prepared.document
    sections: list[FormSection] = []
    for out in outcomes:
        if out.extraction is None:
            continue
        texts = prepared.page_texts.get(out.page_number, [])
        for sec in out.extraction.sections:
            fields: list[FormField] = []
            for pos, f in enumerate(sec.fields):
                label, required, issue = parse_label(f.printed_label)
                match = corroborate(f.printed_label, texts)
                fields.append(
                    FormField(
                        printed_label=f.printed_label,
                        label=label,
                        required_as_shown=required,
                        marker_issue=issue,
                        page_number=f.page_number,
                        chunk_id=match.chunk_id if match else None,
                        position=pos,
                        text_reliability=match.reliability if match else "model_output",
                        verification_status="corroborated" if match else "uncorroborated",
                        field_type=f.field_type,
                        options=list(f.options),
                    )
                )
            chunk_ids = sorted({f.chunk_id for f in fields if f.chunk_id})
            sections.append(
                FormSection(
                    ordinal=len(sections),
                    name=sec.name,
                    page_start=out.page_number,
                    page_end=out.page_number,
                    is_declaration=sec.is_declaration,
                    chunk_ids=chunk_ids,
                    fields=fields,
                    flags=["no_fields"] if not fields else [],
                )
            )
    all_fields = [f for s in sections for f in s.fields]
    flags = {"multimodal_model_output"}
    failed = [o.page_number for o in outcomes if o.extraction is None]
    if failed:
        flags.add("partial_extraction")
        flags.update(f"invalid_output_page_{n}" for n in failed)
    if doc.quality == "scanned":
        flags.add("scanned_document")
    if any(f.verification_status == "uncorroborated" for f in all_fields):
        flags.add("uncorroborated_fields")
    if any(f.marker_issue for f in all_fields):
        flags.add("ambiguous_required_markers")
    if not all_fields:
        flags.add("no_field_structure")
    provenance = ExtractionProvenance(
        provider=provider.name,
        model_id=provider.model_id,
        prompt_id=PROMPT_ID,
        prompt_version=PROMPT_VERSION,
        render_dpi=dpi,
        pages=[
            PageProvenance(page_number=o.page_number, png_sha256=o.png_sha256) for o in outcomes
        ],
        input_tokens=sum(o.input_tokens for o in outcomes),
        output_tokens=sum(o.output_tokens for o in outcomes),
        latency_ms=sum(o.latency_ms for o in outcomes),
        attempts=max(1, sum(o.attempts for o in outcomes)),
        validation_errors=[e for o in outcomes for e in o.errors][:50],
    )
    return FormSchema(
        form_key=doc.document_key,
        document_id=str(doc.id),
        document_sha256=doc.sha256,
        title=doc.title,
        version=doc.version,
        document_type=doc.document_type,
        document_role=ROLES[doc.document_type],
        evidence_eligibility=doc.evidence_eligibility,
        quality=doc.quality,
        extraction_method=MULTIMODAL_METHOD,
        extractor_version=extractor_version,
        sections=sections,
        flags=sorted(flags),
        chunk_count=len({f.chunk_id for f in all_fields if f.chunk_id}),
        field_count=len(all_fields),
        required_as_shown_count=sum(f.required_as_shown is True for f in all_fields),
        text_block_count=0,
        provenance=provenance,
    )


@dataclass(frozen=True)
class GeminiRun:
    outcome: SaveOutcome | None  # None = nothing stored (no valid page output)
    schema: FormSchema | None
    pages: list[PageOutcome]
    provider_calls: int


async def extract_with_provider(
    session: Session,
    prepared: Prepared,
    provider: MultimodalProvider,
    *,
    extractor_version: int = GEMINI_EXTRACTOR_VERSION,
) -> GeminiRun:
    """Skip if stored; otherwise extract, build and save (the caller commits)."""
    doc = prepared.document
    existing = stored_form_schema_row(session, doc.id, MULTIMODAL_METHOD, extractor_version)
    if existing is not None:
        outcome = SaveOutcome(
            doc.document_key, "unchanged", extractor_version, existing.schema_sha256
        )
        return GeminiRun(outcome, None, [], provider_calls=0)
    before = getattr(provider, "attempts_made", 0)
    pages = await extract_pages(prepared, provider)
    calls = getattr(provider, "attempts_made", 0) - before
    if all(p.extraction is None for p in pages):
        return GeminiRun(None, None, pages, provider_calls=calls)
    schema = build_schema(prepared, pages, provider=provider, extractor_version=extractor_version)
    return GeminiRun(save_form_schema(session, schema), schema, pages, provider_calls=calls)
