"""Shared M4 S2 test builders: synthetic stored chunks, citations and items.

All text is synthetic (shaped like corpus sentences, not copied from it). Records are built
with the M1 `ChunkMetadata` builders used by the M3 S4/S5 tests, so eligibility,
reliability and `can_support_mandatory` come from the real derivation rules.
"""

from typing import Any

from app.db.repository import EvidenceContext
from app.graph.state import Citation, ValidatedRequirement
from app.ingestion.chunking import ChunkRecord
from app.schemas.taxonomy import ContentKind, TextReliability
from app.verification.claims import extract
from app.verification.decide import Thresholds, Verified, verify_requirement
from app.verification.nli import FakeNLI
from app.verification.premises import Premise, build_premise
from tests.unit.test_forms_extract import form_doc, rec
from tests.unit.test_indexing import doc

RBI = doc("rbi_directions", entity_types=["INDIVIDUAL", "COMPANY", "PARTNERSHIP", "TRUST", "LLP"])
FAQ = doc("rbi_faq", source_status="dated", status_reason="older basis")
FORM = form_doc("hdfc_ni_form", entity_types=["LLP", "TRUST", "COMPANY", "HUF"])
PARA = ContentKind.PARAGRAPH
T = Thresholds(entail=0.8, contradict=0.8, version="test-only")

_index = iter(range(10_000))


def primary(text: str, *, section: str | None = "C", **kw: Any) -> ChunkRecord:
    return rec(RBI, next(_index), text, section=section, kind=PARA, **kw)


def ocr_primary(text: str) -> ChunkRecord:
    return rec(RBI, next(_index), text, section="C", kind=PARA,
               ocr=TextReliability.OCR_UNCERTAIN, confidence=0.5)  # fmt: skip


def dated(text: str) -> ChunkRecord:
    return rec(FAQ, next(_index), text, section="Q", kind=PARA)


def form(text: str) -> ChunkRecord:
    return rec(FORM, next(_index), text, section="A) ENTITY DETAILS", kind=ContentKind.FORM_TEXT)


def citation(r: ChunkRecord, scope: str = "entity") -> Citation:
    m = r.metadata
    return Citation(
        evidence_id=m.chunk_id,
        document_key=m.document_key,
        page=m.page_number,
        page_end=m.page_end,
        paragraph=m.paragraph,
        match_scope=scope,
        evidence_eligibility=m.evidence_eligibility,
        text_reliability=m.text_reliability,
        source_status=m.source_status,
        can_support_mandatory=m.can_support_mandatory,
    )


def premise(r: ChunkRecord, scope: str = "entity", parent: ChunkRecord | None = None) -> Premise:
    return build_premise(citation(r, scope), EvidenceContext(retrieved=r, context=parent or r))


def item(
    name: str,
    records: list[ChunkRecord],
    *,
    applicability: str = "MANDATORY",
    proposed: str | None = None,
    explanation: str = "",
    scope: str = "entity",
) -> ValidatedRequirement:
    return ValidatedRequirement(
        requirement_id="REQ_001",
        name=name,
        explanation=explanation,
        applicability=applicability,
        proposed_applicability=proposed or applicability,
        citations=[citation(r, scope) for r in records],
        uncertainty_label="test",
    )


def run(
    name: str,
    records: list[ChunkRecord],
    *,
    nli: FakeNLI | None,
    entity: Any = "TRUST",
    scope: str = "entity",
    max_pairs: int = 500,
    **kw: Any,
) -> Verified:
    """Extract, build premises and decide, exactly as S5 will chain them."""
    from app.schemas.taxonomy import EntityType

    req = item(name, records, scope=scope, **kw)
    ent = EntityType(entity) if entity is not None else None
    prems = [premise(r, scope) for r in records]
    claims = extract(req, entity=ent, roles={p.evidence_role.value for p in prems}).claims
    return verify_requirement(
        req, claims, prems, scorer=nli, thresholds=T, entity=ent, max_pairs=max_pairs
    )
