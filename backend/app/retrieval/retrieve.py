"""Retrieval modes and hybrid orchestration (M2 A2, docs/04).

`Retriever.retrieve(query, mode=...)` returns a `RetrievalResult`:

- `dense`: exactly M1 `search()` (BGE + Qdrant). Hits and payloads are M1's, including
  the Qdrant `index` bookkeeping. A failure is an error (no fallback).
- `bm25`: exactly the A1 keyword index (`KeywordIndexManager`). A failure is an error.
- `hybrid`: both retrievers, run sequentially, each returning `candidates` hits with the
  same `SearchFilters`; their rankings are fused with RRF (`fusion.rrf_fuse`) to top-k.
  Hit payloads are the Postgres-built payloads (no Qdrant `index` field), identical
  whichever retriever found the chunk.
  - Both succeed: a normal fused result, `degraded == ()`.
  - One fails: the other's own ranking, `degraded == ("dense",)` or `("bm25",)`, with
    the error kept in `failures`. Still a valid, clearly labelled result.
  - Both fail: `RetrievalError`.

Score semantics (never mix them up downstream; `RetrievalResult.score_kind` says which):
- `SearchHit.score` is the original dense cosine score in `dense` mode, the original
  BM25 score in `bm25` mode, and the RRF fused score in `hybrid` mode. In a degraded
  hybrid result it is the surviving retriever's original score.
- `RetrievalResult.provenance[chunk_id]` always keeps the original dense rank and score,
  the original BM25 rank and score (each `None` when that retriever did not return the
  chunk) and, for fused results, the fused score.

An empty query is an input error (`ValueError`), never a degradation.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.retrieval.embeddings import Embedder
from app.retrieval.fusion import DEFAULT_RRF_K, rrf_fuse
from app.retrieval.keyword import KeywordIndexManager, payload_without_index
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.search import DEFAULT_TOP_K, SearchFilters, SearchHit, search

DEFAULT_CANDIDATES = 50  # per retriever, before fusion (M2_PLAN section 6; not tuned)


class RetrievalMode(StrEnum):
    DENSE = "dense"
    BM25 = "bm25"
    HYBRID = "hybrid"


class ScoreKind(StrEnum):
    DENSE = "dense_cosine"
    BM25 = "bm25"
    RRF = "rrf"


@dataclass(frozen=True)
class HitProvenance:
    dense_rank: int | None = None
    dense_score: float | None = None
    bm25_rank: int | None = None
    bm25_score: float | None = None
    fused_score: float | None = None

    @property
    def retrievers(self) -> tuple[str, ...]:
        found = (("dense", self.dense_rank), ("bm25", self.bm25_rank))
        return tuple(name for name, rank in found if rank is not None)


@dataclass(frozen=True)
class RetrieverFailure:
    retriever: str  # "dense" | "bm25"
    error: str


class RetrievalError(RuntimeError):
    """No retriever produced a result (both failed in hybrid mode, or the only one)."""

    def __init__(self, failures: Sequence[RetrieverFailure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(f"{f.retriever}: {f.error}" for f in self.failures))


@dataclass(frozen=True)
class RetrievalResult:
    mode: RetrievalMode
    hits: list[SearchHit]
    provenance: dict[str, HitProvenance]
    degraded: tuple[str, ...] = ()  # retrievers that failed (hybrid mode only)
    failures: tuple[RetrieverFailure, ...] = ()
    timings_ms: dict[str, float] = field(default_factory=dict)
    dropped: tuple[str, ...] = ()  # dense chunk ids with no Postgres row (not cited)

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded)

    @property
    def score_kind(self) -> ScoreKind:
        if self.mode is RetrievalMode.DENSE or self.degraded == ("bm25",):
            return ScoreKind.DENSE
        if self.mode is RetrievalMode.BM25 or self.degraded == ("dense",):
            return ScoreKind.BM25
        return ScoreKind.RRF


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class _TimedEmbedder:
    """Delegates to an embedder and records the time spent embedding the query."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.model_name = inner.model_name
        self.revision = inner.revision
        self.query_ms = 0.0

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self.inner.embed_passages(texts)

    def embed_query(self, text: str) -> list[float]:
        started = time.perf_counter()
        try:
            return self.inner.embed_query(text)
        finally:
            self.query_ms = (time.perf_counter() - started) * 1000


class Retriever:
    """Runs one retrieval mode. Dependencies are created lazily, so `bm25` mode never
    loads the embedding model or contacts Qdrant and `dense` mode never reads Postgres."""

    def __init__(
        self,
        *,
        embedder: Callable[[], Embedder],
        store: Callable[[], ChunkStore],
        keyword: Callable[[], KeywordIndexManager],
        rrf_k: int = DEFAULT_RRF_K,
        candidates: int = DEFAULT_CANDIDATES,
    ) -> None:
        if candidates < 1:
            raise ValueError("candidates must be >= 1")
        self._embedder_factory, self._store_factory = embedder, store
        self._keyword_factory = keyword
        self._embedder: _TimedEmbedder | None = None
        self._store: ChunkStore | None = None
        self._keyword: KeywordIndexManager | None = None
        self.rrf_k = rrf_k
        self.candidates = candidates

    # --- the two retrievers --------------------------------------------------------------

    def _dense(
        self, query: str, top_k: int, filters: SearchFilters, t: dict[str, float]
    ) -> list[SearchHit]:
        started = time.perf_counter()
        try:
            if self._embedder is None:
                self._embedder = _TimedEmbedder(self._embedder_factory())
            if self._store is None:
                self._store = self._store_factory()
            self._embedder.query_ms = 0.0
            hits = search(query, self._embedder, self._store, top_k=top_k, filters=filters)
        finally:
            total = (time.perf_counter() - started) * 1000
            embed = self._embedder.query_ms if self._embedder is not None else 0.0
            t |= {"dense_ms": total, "embed_ms": embed, "qdrant_ms": max(0.0, total - embed)}
        return hits

    def _bm25(
        self, query: str, top_k: int, filters: SearchFilters, t: dict[str, float]
    ) -> tuple[list[SearchHit], dict[str, dict[str, Any]]]:
        started = time.perf_counter()
        try:
            if self._keyword is None:
                self._keyword = self._keyword_factory()
            index = self._keyword.get()
            t |= {
                "freshness_ms": self._keyword.last_check_ms,
                "keyword_build_ms": self._keyword.last_build_ms,
            }
            scoring = time.perf_counter()
            hits = index.search(query, top_k=top_k, filters=filters)
            t["bm25_scoring_ms"] = (time.perf_counter() - scoring) * 1000
        finally:
            t["bm25_ms"] = (time.perf_counter() - started) * 1000
        return hits, {p["chunk_id"]: p for p in index.payloads}

    # --- modes ---------------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        mode: RetrievalMode | str,
        top_k: int = DEFAULT_TOP_K,
        filters: SearchFilters | None = None,
    ) -> RetrievalResult:
        mode = RetrievalMode(mode)
        if not query.strip():
            raise ValueError("query is empty")
        flt = filters or SearchFilters()
        t: dict[str, float] = {}
        started = time.perf_counter()
        try:
            if mode is RetrievalMode.DENSE:
                return self._single(mode, self._try_dense(query, top_k, flt, t), t)
            if mode is RetrievalMode.BM25:
                keyword = self._try_bm25(query, top_k, flt, t)
                hits = keyword if isinstance(keyword, RetrieverFailure) else keyword[0]
                return self._single(mode, hits, t)
            return self._hybrid(query, top_k, flt, t)
        finally:
            t["total_ms"] = (time.perf_counter() - started) * 1000

    def _try_dense(
        self, query: str, top_k: int, flt: SearchFilters, t: dict[str, float]
    ) -> list[SearchHit] | RetrieverFailure:
        try:
            return self._dense(query, top_k, flt, t)
        except Exception as exc:
            return RetrieverFailure("dense", _describe(exc))

    def _try_bm25(
        self, query: str, top_k: int, flt: SearchFilters, t: dict[str, float]
    ) -> tuple[list[SearchHit], dict[str, dict[str, Any]]] | RetrieverFailure:
        try:
            return self._bm25(query, top_k, flt, t)
        except Exception as exc:
            return RetrieverFailure("bm25", _describe(exc))

    @staticmethod
    def _provenance(name: str, hits: list[SearchHit]) -> dict[str, HitProvenance]:
        if name == "dense":
            return {h.chunk_id: HitProvenance(dense_rank=h.rank, dense_score=h.score) for h in hits}
        return {h.chunk_id: HitProvenance(bm25_rank=h.rank, bm25_score=h.score) for h in hits}

    def _single(
        self,
        mode: RetrievalMode,
        outcome: list[SearchHit] | RetrieverFailure,
        t: dict[str, float],
    ) -> RetrievalResult:
        if isinstance(outcome, RetrieverFailure):
            raise RetrievalError([outcome])
        return RetrievalResult(mode, outcome, self._provenance(mode.value, outcome), timings_ms=t)

    def _hybrid(
        self, query: str, top_k: int, flt: SearchFilters, t: dict[str, float]
    ) -> RetrievalResult:
        mode = RetrievalMode.HYBRID
        dense = self._try_dense(query, self.candidates, flt, t)
        keyword = self._try_bm25(query, self.candidates, flt, t)
        if isinstance(keyword, RetrieverFailure):
            if isinstance(dense, RetrieverFailure):  # both failed: no result at all
                raise RetrievalError([dense, keyword])
            # BM25 failed: the dense ranking alone, labelled (Postgres payloads unavailable,
            # so the Qdrant payload minus its `index` bookkeeping, which is identical).
            hits = [
                SearchHit(h.rank, h.score, h.chunk_id, payload_without_index(h.payload))
                for h in dense[:top_k]
            ]
            return RetrievalResult(
                mode, hits, self._provenance("dense", hits), ("bm25",), (keyword,), t
            )
        bm25, payloads = keyword
        if isinstance(dense, RetrieverFailure):  # dense failed: BM25 ranking alone, labelled
            hits = bm25[:top_k]
            return RetrievalResult(
                mode, hits, self._provenance("bm25", hits), ("dense",), (dense,), t
            )
        fusing = time.perf_counter()
        by_dense = {h.chunk_id: h for h in dense}
        by_bm25 = {h.chunk_id: h for h in bm25}
        # Postgres is the source of truth: a dense point without a stored chunk is not cited.
        dropped = tuple(h.chunk_id for h in dense if h.chunk_id not in payloads)
        fused = rrf_fuse(
            {"dense": [h.chunk_id for h in dense if h.chunk_id in payloads],
             "bm25": [h.chunk_id for h in bm25]},
            k=self.rrf_k, top_k=top_k,
        )  # fmt: skip
        hits, provenance = [], {}
        for rank, c in enumerate(fused, start=1):
            hits.append(SearchHit(rank, c.score, c.chunk_id, payloads[c.chunk_id]))
            d, b = by_dense.get(c.chunk_id), by_bm25.get(c.chunk_id)
            provenance[c.chunk_id] = HitProvenance(
                dense_rank=d.rank if d else None,
                dense_score=d.score if d else None,
                bm25_rank=b.rank if b else None,
                bm25_score=b.score if b else None,
                fused_score=c.score,
            )
        t["fusion_ms"] = (time.perf_counter() - fusing) * 1000
        return RetrievalResult(mode, hits, provenance, timings_ms=t, dropped=dropped)
