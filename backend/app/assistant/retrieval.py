"""HDFC-only retrieval for the assistant (A2; A1 §2, §8.2).

Each query runs in exactly one `EvidenceScope`. The coverage map gives the only document
keys that scope may read, so opening-form evidence never mixes with re-KYC context or the
legacy reference. Two independent guards keep RBI, external and out-of-scope material out:

1. the search itself is filtered (`bank = HDFC` and the scope's document keys), in Qdrant
   for dense search and before scoring for BM25 (`SearchFilters`);
2. every returned hit is checked again; any hit from another bank, an unknown document or
   a document outside the scope raises `AssistantScopeError` (fail closed: an out-of-scope
   chunk is never returned, not even silently dropped).

An opening query for a NOT_ESTABLISHED cell retrieves nothing (no search is run).
Hits keep their document key, page reference, source role, document type, extraction
method, text reliability and OCR confidence; scores stay internal (never for display).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.assistant.coverage import Coverage, coverage
from app.assistant.taxonomy import (
    ASSISTANT_BANK,
    SCOPE_ROLES,
    AccountType,
    CustomerCategory,
    EvidenceScope,
    SourceRole,
)
from app.retrieval.retrieve import RetrievalMode, RetrievalResult
from app.retrieval.search import SearchFilters, SearchHit

DEFAULT_TOP_K = 5


class AssistantScopeError(RuntimeError):
    """A retrieval returned a chunk the assistant may never use (wrong bank or scope)."""


class _Retriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        mode: RetrievalMode | str,
        top_k: int = DEFAULT_TOP_K,
        filters: SearchFilters | None = None,
        rerank: bool = False,
    ) -> RetrievalResult: ...


@dataclass(frozen=True)
class AssistantHit:
    rank: int
    chunk_id: str
    document_key: str
    document_title: str
    document_type: str
    source_role: SourceRole
    page: int
    page_end: int | None
    section: str | None
    extraction_method: str
    text_reliability: str
    ocr_confidence: float | None
    content: str
    score: float  # internal ranking signal only; never shown to customers

    @property
    def quotable(self) -> bool:
        """A1 §8.2 rule 3a: only native or reliable OCR text may be shown as a quotation."""
        return self.text_reliability in ("native", "ocr_reliable")


@dataclass(frozen=True)
class AssistantRetrieval:
    scope: EvidenceScope
    document_keys: tuple[str, ...]
    hits: tuple[AssistantHit, ...]
    searched: bool  # False when the scope had no documents (nothing was retrieved)


def _int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


class AssistantRetriever:
    def __init__(
        self,
        retriever: _Retriever,
        *,
        coverage_map: Coverage | None = None,
        mode: RetrievalMode = RetrievalMode.DENSE,
    ) -> None:
        self._retriever = retriever
        self._coverage = coverage_map or coverage()
        self.mode = mode

    # --- the four scopes ------------------------------------------------------------------------

    def opening(
        self,
        query: str,
        category: CustomerCategory,
        account_type: AccountType,
        *,
        top_k: int = DEFAULT_TOP_K,
    ) -> AssistantRetrieval:
        keys = self._coverage.documents_for(EvidenceScope.OPENING, category, account_type)
        return self._run(EvidenceScope.OPENING, query, keys, top_k)

    def conversion(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> AssistantRetrieval:
        keys = self._coverage.documents_for(EvidenceScope.CONVERSION)
        return self._run(EvidenceScope.CONVERSION, query, keys, top_k)

    def context(
        self, query: str, category: CustomerCategory, *, top_k: int = DEFAULT_TOP_K
    ) -> AssistantRetrieval:
        """Re-KYC / dormant material for this category: unconfirmed context only (A1 §3)."""
        keys = self._coverage.documents_for(EvidenceScope.CONTEXT, category)
        return self._run(EvidenceScope.CONTEXT, query, keys, top_k)

    def legacy(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> AssistantRetrieval:
        """The legacy FAQ: for evaluation and diagnostics only; never in MVP checklists."""
        keys = self._coverage.documents_for(EvidenceScope.LEGACY)
        return self._run(EvidenceScope.LEGACY, query, keys, top_k)

    # --- internals --------------------------------------------------------------------------------

    def _run(
        self, scope: EvidenceScope, query: str, keys: tuple[str, ...], top_k: int
    ) -> AssistantRetrieval:
        if not keys:
            return AssistantRetrieval(scope, keys, (), searched=False)
        allowed_roles = SCOPE_ROLES[scope]
        for key in keys:  # the coverage map itself must never hand out a foreign document
            if self._coverage.role(key) not in allowed_roles:
                raise AssistantScopeError(f"{key} is not readable in scope {scope}")
        filters = SearchFilters(banks=(ASSISTANT_BANK,), document_keys=keys)
        result = self._retriever.retrieve(query, mode=self.mode, top_k=top_k, filters=filters)
        hits = tuple(self._checked(h, scope, keys) for h in result.hits)
        return AssistantRetrieval(scope, keys, hits, searched=True)

    def _checked(self, hit: SearchHit, scope: EvidenceScope, keys: tuple[str, ...]) -> AssistantHit:
        p = hit.payload
        key = str(p.get("document_key", ""))
        if p.get("bank") != ASSISTANT_BANK:
            raise AssistantScopeError(f"non-HDFC chunk {hit.chunk_id} returned ({p.get('bank')!r})")
        if key not in keys:
            raise AssistantScopeError(f"chunk {hit.chunk_id} from {key!r} is outside scope {scope}")
        role = self._coverage.role(key)
        if role is None or role not in SCOPE_ROLES[scope]:
            raise AssistantScopeError(f"chunk {hit.chunk_id} has role {role} in scope {scope}")
        page = _int(p.get("page_number"))
        if page is None:
            raise AssistantScopeError(f"chunk {hit.chunk_id} has no page reference")
        doc = self._coverage.documents[key]
        return AssistantHit(
            rank=hit.rank,
            chunk_id=hit.chunk_id,
            document_key=key,
            document_title=doc.title,
            document_type=str(p.get("document_type", "")),
            source_role=role,
            page=page,
            page_end=_int(p.get("page_end")),
            section=(p.get("section") or None),
            extraction_method=str(p.get("extraction_method", "")),
            text_reliability=str(p.get("text_reliability", "")),
            ocr_confidence=_float(p.get("ocr_confidence")),
            content=str(p.get("content", "")),
            score=float(hit.score),
        )
