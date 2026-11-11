"""Pure helpers for M3 S4: evidence items, the evidence status, form choice and links.

No I/O and no model calls: the nodes fetch through `CorpusReader` / the retriever and pass
plain values here. Principle ("no evidence, no answer", M3_PLAN): everything produced
here traces to a retrieved stored chunk or a stored form-schema row; missing evidence is
a gap code, never a negative finding; nothing is filled in to satisfy a question.

Vocabularies are the existing ones (`EntityType`, `ResidencyStatus`, `ENTITY_GROUPS`);
nothing here defines another. `account_type` is not used: no corpus document is tagged
by account type, so any account-to-form mapping would be invented.
"""

import re
from collections.abc import Iterable, Sequence
from typing import Literal

from app.db.repository import EvidenceContext
from app.graph.corpus import FormDocument, FormSchemaRowInfo
from app.graph.state import (
    EntityProfile,
    EvidenceItem,
    FieldEvidenceLink,
    FormFieldRef,
    LinkKind,
)
from app.schemas.forms import FormSchema
from app.schemas.taxonomy import ENTITY_GROUPS, EntityGroup, EntityType

EvidenceStatus = Literal["found", "partial", "none", "retrieval_failed"]
Scope = Literal["entity", "entity_group"]

TOP_K = 5  # per retrieval pass (the M1/M2 default)
MAX_EVIDENCE_CHARS = 1500  # per item; protects the S5 token budget (flagged when cut)
MAX_FORM_FIELDS = 60  # per form in state (flagged `fields_capped`)
MAX_LINKS_PER_FIELD = 3
MAX_LINKS = 200

GAP_NO_ENTITY_PRIMARY = "no_entity_specific_primary_evidence"
GAP_PRIMARY_ONLY_VIA_GROUP = "primary_evidence_only_via_entity_group"
GAP_NO_ELIGIBLE = "no_eligible_evidence"
GAP_RETRIEVAL_FAILED = "retrieval_failed"

GEMINI = "gemini_multimodal"
M1 = "m1_chunks"
INCOMPLETE_GEMINI_FLAGS = frozenset({"partial_extraction", "gemini_extraction_refused"})
# Schema flags a reviewer must see (anything else, e.g. per-page codes, is left out).
REVIEW_FLAGS = frozenset(
    {
        "scanned_document",
        "ocr_uncertain_text",
        "uncorroborated_fields",
        "no_field_structure",
        "ocr_fallback_requires_review",
        "recitation_refused",
        "repeated_labels",
        "ambiguous_required_markers",
        "parse_issues",
        "multimodal_model_output",
    }
)


# --- retrieval ----------------------------------------------------------------------------------


def group_for(entity: EntityType) -> EntityGroup | None:
    """The narrowest existing taxonomy group containing `entity` (none for INDIVIDUAL)."""
    for group in (EntityGroup.LEGAL_ENTITY, EntityGroup.NON_INDIVIDUAL):
        if entity in ENTITY_GROUPS[group]:
            return group
    return None


def evidence_item(
    ctx: EvidenceContext, *, rank: int, score_kind: str, scope: Scope
) -> EvidenceItem:
    """An evidence item built only from the stored chunk (and its parent paragraph when
    the hit is a child). Citations point at the text actually shown."""
    hit = ctx.retrieved.metadata
    shown = ctx.context.metadata
    text = shown.content
    truncated = len(text) > MAX_EVIDENCE_CHARS
    return EvidenceItem(
        evidence_id=hit.chunk_id,
        document_id=str(hit.document_id),
        document_key=hit.document_key,
        bank=hit.bank,
        page=shown.page_number,
        page_end=shown.page_end,
        paragraph=hit.paragraph,
        section=hit.section,
        source_status=hit.source_status,
        evidence_eligibility=hit.evidence_eligibility,
        text_reliability=hit.text_reliability,
        can_support_mandatory=hit.can_support_mandatory,
        rank=rank,
        score_kind=score_kind,
        text=text[:MAX_EVIDENCE_CHARS] if truncated else text,
        expanded_parent_id=shown.chunk_id if ctx.expanded else None,
        match_scope=scope,
        text_truncated=truncated,
    )


def classify_evidence(items: Sequence[EvidenceItem]) -> tuple[EvidenceStatus, list[str]]:
    """`found` / `partial` / `none` for a search that ran (failures are classified by the
    node). `found` means entity-scope primary-capable evidence was RETRIEVED; it does not
    mean the evidence is relevant or correct (scores are uncalibrated; S5 judges support).
    """
    eligible = [i for i in items if i.evidence_eligibility in ("primary", "supporting")]
    if not eligible:
        return "none", [GAP_NO_ELIGIBLE]
    if any(i.can_support_mandatory and i.match_scope == "entity" for i in eligible):
        return "found", []
    gaps = [GAP_NO_ENTITY_PRIMARY]
    if any(i.can_support_mandatory for i in eligible):
        gaps.append(GAP_PRIMARY_ONLY_VIA_GROUP)
    return "partial", gaps


# --- forms -------------------------------------------------------------------------------------


def candidate_forms(
    documents: Iterable[FormDocument], profile: EntityProfile, evidence_keys: Iterable[str]
) -> list[FormDocument]:
    """KYC forms for the profile, plus forms whose chunks were retrieved.

    Entity must be listed on the form. Residency is used only for INDIVIDUAL (the approved
    rule, docs/15): the form must list the stated residency. For other entities residency
    is unknown and is not used, so it is never assumed."""
    entity = profile.entity_type.value
    residency = profile.residency.value
    retrieved = set(evidence_keys)
    out = []
    for doc in documents:
        matches = entity is not None and entity in doc.entity_types
        if matches and entity == EntityType.INDIVIDUAL:
            matches = residency is not None and residency in doc.residency_statuses
        if matches or doc.document_key in retrieved:
            out.append(doc)
    return out


def choose_schema_row(rows: Iterable[FormSchemaRowInfo], form_key: str) -> FormSchemaRowInfo | None:
    """The latest COMPLETE Gemini row (no partial or refused extraction), else the latest
    m1_chunks row (decision D4). None when nothing is stored."""
    mine = [r for r in rows if r.form_key == form_key]
    gemini = [
        r for r in mine if r.method == GEMINI and not set(r.quality_flags) & INCOMPLETE_GEMINI_FLAGS
    ]
    if gemini:
        return max(gemini, key=lambda r: r.version)
    m1 = [r for r in mine if r.method == M1]
    return max(m1, key=lambda r: r.version) if m1 else None


def field_subset(
    schema: FormSchema, retrieved_ids: set[str]
) -> tuple[list[FormFieldRef], dict[str, list[str]], bool]:
    """Fields from sections containing a retrieved chunk, fields shown as required, and
    declaration fields; capped at MAX_FORM_FIELDS (third value: whether it was capped).
    Every field is copied from the stored schema, with its extraction status."""
    chosen: list[FormFieldRef] = []
    sections: dict[str, list[str]] = {}
    capped = False
    for section in schema.sections:
        section_hit = bool(set(section.chunk_ids) & retrieved_ids)
        for f in section.fields:
            if not (
                section_hit
                or section.is_declaration
                or f.required_as_shown
                or (f.chunk_id is not None and f.chunk_id in retrieved_ids)
            ):
                continue
            if len(chosen) >= MAX_FORM_FIELDS:
                capped = True
                break
            chosen.append(
                FormFieldRef(
                    printed_label=f.printed_label,
                    label=f.label,
                    section=section.name,
                    page_number=f.page_number,
                    chunk_id=f.chunk_id,
                    required_as_shown=f.required_as_shown,
                    verification_status=f.verification_status,
                )
            )
            if section.name is not None:
                sections.setdefault(section.name, list(section.chunk_ids))
    return chosen, sections, capped


def review_flags(schema: FormSchema, row: FormSchemaRowInfo, capped: bool) -> list[str]:
    flags = sorted((set(schema.flags) | set(row.quality_flags)) & REVIEW_FLAGS)
    return [*flags, "fields_capped"] if capped else flags


# --- association -------------------------------------------------------------------------------

# Generic words that say nothing about a specific requirement.
_STOPWORDS = frozenset(
    (
        "name",
        "names",
        "date",
        "dates",
        "address",
        "details",
        "detail",
        "number",
        "numbers",
        "account",
        "accounts",
        "customer",
        "customers",
        "applicant",
        "applicants",
        "bank",
        "branch",
        "signature",
        "signatures",
        "please",
        "type",
        "other",
        "code",
        "city",
        "state",
        "country",
        "mobile",
        "phone",
        "email",
        "with",
        "from",
        "that",
        "this",
        "have",
        "will",
        "shall",
        "their",
        "your",
        "form",
        "forms",
        "section",
        "sections",
        "yes",
        "kyc",
        "where",
        "which",
        "when",
        "what",
        "each",
        "any",
        "all",
    )
)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def distinctive_tokens(label: str) -> set[str]:
    """Case-folded label tokens that can identify a topic: acronyms (2–6 capitals, e.g.
    PAN, OVD, CKYC) and words of 4+ letters that are not generic."""
    out = set()
    for tok in _TOKEN.findall(label):
        low = tok.casefold()
        if low in _STOPWORDS:
            continue
        if (tok.isupper() and 2 <= len(tok) <= 6) or len(tok) >= 4:
            out.add(low)
    return out


def build_links(
    evidence: Sequence[EvidenceItem],
    forms: Sequence[tuple[str, Sequence[FormFieldRef], dict[str, list[str]]]],
    form_document_keys: set[str],
) -> list[FieldEvidenceLink]:
    """Relationships between form fields and retrieved evidence (never obligations):
    `same_chunk` (the field's own chunk was retrieved), `same_section` (a retrieved chunk of
    the same form section), `topic_match` (a policy passage containing a distinctive
    word of the label). At most MAX_LINKS_PER_FIELD per field and MAX_LINKS in total;
    order follows the forms, their fields and the evidence rank."""
    ordered = sorted(evidence, key=lambda e: e.rank)
    policy_tokens = {
        e.evidence_id: set(t.casefold() for t in _TOKEN.findall(e.text))
        for e in ordered
        if e.document_key not in form_document_keys
    }
    links: list[FieldEvidenceLink] = []
    for form_key, fields, section_chunks in forms:
        for field in fields:
            found: list[tuple[str, LinkKind]] = []
            for e in ordered:
                if field.chunk_id is not None and e.evidence_id == field.chunk_id:
                    found.append((e.evidence_id, LinkKind.SAME_CHUNK))
                elif (
                    e.document_key == form_key
                    and field.section is not None
                    and e.evidence_id in section_chunks.get(field.section, ())
                ):
                    found.append((e.evidence_id, LinkKind.SAME_SECTION))
                elif e.evidence_id in policy_tokens and distinctive_tokens(field.label) & (
                    policy_tokens[e.evidence_id]
                ):
                    found.append((e.evidence_id, LinkKind.TOPIC_MATCH))
                if len(found) >= MAX_LINKS_PER_FIELD:
                    break
            for evidence_id, kind in found:
                if len(links) >= MAX_LINKS:
                    return links
                links.append(
                    FieldEvidenceLink(
                        form_key=form_key,
                        field_label=field.label,
                        field_chunk_id=field.chunk_id,
                        evidence_id=evidence_id,
                        link_kind=kind,
                    )
                )
    return links
