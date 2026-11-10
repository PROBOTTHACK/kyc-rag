"""BM25 keyword retrieval, built in-process from Postgres chunk rows (M2 A1, docs/04).

- Corpus: every embedding-eligible chunk (single and child; the same rows Qdrant holds),
  loaded from Postgres, the source of truth. Parents are never in the corpus; a child hit
  expands to its parent paragraph through `evidence_context`, exactly as for dense hits.
- Indexed text: `embedding_input` (child context + content), the same text dense
  retrieval embeds. Payloads come from the same row -> payload mapping as Qdrant, so hits
  carry identical citations and provenance.
- Scoring: `rank_bm25.BM25Okapi` with the library defaults (k1=1.5, b=0.75, not tuned).
  IDF and average length are global over the corpus. Metadata filters are applied
  BEFORE scoring: only chunks passing `SearchFilters.matches` are scored at all.
  Eligibility and authority are filters, never ranking signals.
- Freshness: `KeywordIndexManager.get()` re-reads a version fingerprint of the stored
  rows on every call and rebuilds when it changed. Since M2 A2 this is the cheap
  `chunk_corpus_version` (row counts + `(id, xmin)` of chunks and documents; ~5 ms)
  instead of the A1 content hash (~100 ms); it detects every committed insert, update
  and delete. A failed read raises `KeywordIndexUnavailable`; a previously built index
  is never served after a failure. `last_check_ms` / `last_build_ms` report the cost.
"""

import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session, sessionmaker

from app.db.repository import chunk_corpus_version, eligible_chunk_payloads
from app.retrieval.search import DEFAULT_TOP_K, SearchFilters, SearchHit

TOKENIZER_VERSION = 1  # bump when `tokenize` changes
_TOKEN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")


class KeywordIndexUnavailable(RuntimeError):
    """The keyword index cannot be built or verified as current (e.g. Postgres is down)."""


def tokenize(text: str) -> list[str]:
    """Plain tokenizer (v1): NFKC, lowercase, alphanumeric runs. Internal `.`, `/` and `-`
    join a token, so terms such as "v-cip", "51a", "1.5" and "1718" stay whole."""
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).lower())


def index_text(payload: dict[str, Any]) -> str:
    """The text indexed for a chunk: the same input dense retrieval embeds."""
    return "\n".join(p for p in (payload.get("context"), payload["content"]) if p)


@dataclass
class KeywordIndex:
    """An immutable BM25 index over one snapshot of the eligible chunks."""

    payloads: list[dict[str, Any]]
    fingerprint: str = ""
    build_seconds: float = 0.0
    _bm25: BM25Okapi | None = field(default=None, repr=False)

    @classmethod
    def build(cls, payloads: list[dict[str, Any]], fingerprint: str = "") -> "KeywordIndex":
        for p in payloads:
            if not p.get("embedding_eligible") or p.get("chunk_role") == "parent":
                raise ValueError(f"chunk {p.get('chunk_id')} is not embedding-eligible")
        started = time.perf_counter()
        corpus = [tokenize(index_text(p)) for p in payloads]
        # rank_bm25 divides by the corpus size; an empty corpus simply has no index.
        bm25 = BM25Okapi(corpus) if corpus else None
        return cls(list(payloads), fingerprint, time.perf_counter() - started, bm25)

    def __len__(self) -> int:
        return len(self.payloads)

    def search(
        self, query: str, *, top_k: int = DEFAULT_TOP_K, filters: SearchFilters | None = None
    ) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("query is empty")
        terms = tokenize(query)
        flt = filters or SearchFilters()
        candidates = [i for i, p in enumerate(self.payloads) if flt.matches(p)]
        if self._bm25 is None or not terms or not candidates:
            return []
        scores = self._bm25.get_batch_scores(terms, candidates)
        ranked = sorted(
            ((float(s), i) for s, i in zip(scores, candidates, strict=True) if s > 0),
            key=lambda si: (-si[0], self.payloads[si[1]]["chunk_id"]),
        )[:top_k]
        return [
            SearchHit(rank=r, score=s, chunk_id=self.payloads[i]["chunk_id"],
                      payload=self.payloads[i])
            for r, (s, i) in enumerate(ranked, start=1)
        ]  # fmt: skip


class KeywordIndexManager:
    """Serves a keyword index that matches the stored rows, rebuilding when they change.

    The fingerprint and the rows are read in one REPEATABLE READ transaction, so an index
    is always built from exactly the snapshot its fingerprint describes.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        build: Callable[[list[dict[str, Any]], str], KeywordIndex] = KeywordIndex.build,
    ) -> None:
        self._sessions = sessions
        self._build = build
        self._index: KeywordIndex | None = None
        self._lock = threading.Lock()
        self.rebuilds = 0
        self.last_check_ms = 0.0  # freshness check of the last `get()` (session + version read)
        self.last_build_ms = 0.0  # row load + index build of the last `get()` (0 if reused)

    def get(self) -> KeywordIndex:
        with self._lock:
            started = time.perf_counter()
            self.last_build_ms = 0.0
            try:
                with self._sessions() as s:
                    s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                    fp = f"t{TOKENIZER_VERSION}:x:{chunk_corpus_version(s)}"
                    self.last_check_ms = (time.perf_counter() - started) * 1000
                    if self._index is not None and self._index.fingerprint == fp:
                        return self._index
                    payloads = eligible_chunk_payloads(s)  # same snapshot as `fp`
            except Exception as exc:
                self._index = None  # never serve an index we could not verify
                raise KeywordIndexUnavailable(
                    f"keyword index unavailable: {type(exc).__name__}: {exc}"
                ) from exc
            self._index = self._build(payloads, fp)
            self.rebuilds += 1
            self.last_build_ms = (time.perf_counter() - started) * 1000 - self.last_check_ms
            return self._index

    def search(
        self, query: str, *, top_k: int = DEFAULT_TOP_K, filters: SearchFilters | None = None
    ) -> list[SearchHit]:
        return self.get().search(query, top_k=top_k, filters=filters)


def payload_without_index(payload: dict[str, Any]) -> dict[str, Any]:
    """A Qdrant payload minus its indexing bookkeeping, comparable to a Postgres payload."""
    return {k: v for k, v in payload.items() if k != "index"}
