"""Dense retrieval over indexed chunks (M1). The search CLI and Recall@5 come in Stage G.

Eligibility is a filter, not a ranking signal (docs/15): by default only `primary` and
`supporting` sources are searched, so `context_only` material (the NetBanking form) is
excluded unless asked for. `mandatory_evidence_only` further restricts results to chunks
with `can_support_mandatory = true` (primary, not uncertain OCR). Entity filters expand
groups ("LEGAL_ENTITY") into their member types.
"""

from dataclasses import dataclass
from typing import Any

from qdrant_client import models as qm

from app.retrieval.embeddings import Embedder
from app.retrieval.qdrant_store import ChunkStore
from app.schemas.taxonomy import EvidenceEligibility, expand_entity_terms

DEFAULT_TOP_K = 5


@dataclass(frozen=True)
class SearchFilters:
    include_context_only: bool = False
    mandatory_evidence_only: bool = False
    banks: tuple[str, ...] = ()
    document_keys: tuple[str, ...] = ()
    entity_terms: tuple[str, ...] = ()  # entity types and/or groups

    def matches(self, payload: dict[str, Any]) -> bool:
        """In-process equivalent of `build_filter` (keyword retrieval filters before
        scoring). Same semantics as the Qdrant filter, including its edge cases: a
        list-valued field matches `MatchAny` when any element is allowed, a missing or
        empty field never matches, and `mandatory_evidence_only` needs a boolean true.
        Parity with Qdrant is tested on the real corpus (tests/integration)."""
        allowed = {EvidenceEligibility.PRIMARY.value, EvidenceEligibility.SUPPORTING.value}
        if self.include_context_only:
            allowed.add(EvidenceEligibility.CONTEXT_ONLY.value)
        if payload.get("evidence_eligibility") not in allowed:
            return False
        if self.mandatory_evidence_only and payload.get("can_support_mandatory") is not True:
            return False
        if self.banks and payload.get("bank") not in self.banks:
            return False
        if self.document_keys and payload.get("document_key") not in self.document_keys:
            return False
        if self.entity_terms:
            types = {t.value for t in expand_entity_terms(list(self.entity_terms))}
            if not types & set(payload.get("entity_types") or ()):
                return False
        return True


@dataclass(frozen=True)
class SearchHit:
    rank: int
    score: float
    chunk_id: str
    payload: dict[str, Any]

    @property
    def citation(self) -> str:
        p = self.payload
        pages = (
            f"p.{p['page_number']}"
            if not p.get("page_end")
            else f"pp.{p['page_number']}-{p['page_end']}"
        )
        parts = [p["document_key"], pages]
        if p.get("region"):
            parts.append(f"{p['region']} half")
        if p.get("paragraph"):
            parts.append(f"para {p['paragraph']}")
        return ", ".join(parts)


def build_filter(filters: SearchFilters) -> qm.Filter:
    allowed = [EvidenceEligibility.PRIMARY.value, EvidenceEligibility.SUPPORTING.value]
    if filters.include_context_only:
        allowed.append(EvidenceEligibility.CONTEXT_ONLY.value)
    must: list[qm.Condition] = [
        qm.FieldCondition(key="evidence_eligibility", match=qm.MatchAny(any=allowed))
    ]
    if filters.mandatory_evidence_only:
        must.append(qm.FieldCondition(key="can_support_mandatory", match=qm.MatchValue(value=True)))
    if filters.banks:
        must.append(qm.FieldCondition(key="bank", match=qm.MatchAny(any=list(filters.banks))))
    if filters.document_keys:
        must.append(
            qm.FieldCondition(
                key="document_key", match=qm.MatchAny(any=list(filters.document_keys))
            )
        )
    if filters.entity_terms:
        types = sorted(t.value for t in expand_entity_terms(list(filters.entity_terms)))
        must.append(qm.FieldCondition(key="entity_types", match=qm.MatchAny(any=types)))
    return qm.Filter(must=must)


def search(
    query: str,
    embedder: Embedder,
    store: ChunkStore,
    *,
    top_k: int = DEFAULT_TOP_K,
    filters: SearchFilters | None = None,
) -> list[SearchHit]:
    if not query.strip():
        raise ValueError("query is empty")
    vector = embedder.embed_query(query)
    points = store.search(vector, top_k, build_filter(filters or SearchFilters()))
    return [
        SearchHit(rank=i + 1, score=float(p.score), chunk_id=str(p.id), payload=p.payload or {})
        for i, p in enumerate(points)
    ]
