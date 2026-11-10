"""M1 retrieval benchmark: Recall@k on hand-labelled queries (docs/10, docs/12 §M1).

A case lists the passages that answer its query (`targets`). A retrieved hit matches a
target when it is from the same document, its page range covers the target page, and,
when given, its paragraph label equals the target's and its text contains the target's
`text_contains` phrase (for documents without paragraph labels, such as forms).

Metrics per case, then averaged:
- `recall@k` = matched targets / all targets (each target counts once);
- `hit@k` = 1 if any target is matched in the top k;
- `reciprocal rank` = 1 / rank of the first matching hit (0 if none in the top k).

Search uses the production filters (`SearchFilters()`: primary + supporting sources).
`evaluate` runs the M1 dense search; `evaluate_with` runs any retriever (M2: BM25) with
the same scoring, plus optional latency timing. Scoring is identical for every mode.
"""

import json
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.retrieval.embeddings import Embedder
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.search import SearchFilters, SearchHit, search

DEFAULT_K = 5
# (query, filters, k) -> ranked hits
Retriever = Callable[[str, SearchFilters, int], list[SearchHit]]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_key: str
    page: int = Field(ge=1)  # physical PDF page
    paragraph: str | None = None
    text_contains: str | None = None

    def matches(self, payload: dict[str, Any]) -> bool:
        if payload.get("document_key") != self.document_key:
            return False
        first = int(payload["page_number"])
        last = int(payload.get("page_end") or first)
        if not first <= self.page <= last:
            return False
        if self.paragraph is not None and payload.get("paragraph") != self.paragraph:
            return False
        if self.text_contains is not None:
            return self.text_contains.lower() in str(payload.get("content", "")).lower()
        return True

    def label(self) -> str:
        para = f" para {self.paragraph}" if self.paragraph else ""
        text = f' "{self.text_contains}"' if self.text_contains else ""
        return f"{self.document_key} p.{self.page}{para}{text}"


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    query: str = Field(min_length=3)
    targets: list[Target] = Field(min_length=1)
    entity_terms: list[str] = Field(default_factory=list)  # optional entity filter
    notes: str | None = None


class CaseResult(BaseModel):
    case: RetrievalCase
    recall: float
    hit: bool
    reciprocal_rank: float
    matched: list[str]  # target labels found in the top k
    retrieved: list[str]  # citations of the top k, in rank order


class Timing(BaseModel):
    """Per-query retrieval latency over timed passes (after one warm-up pass)."""

    passes: int
    p50_ms: float
    p95_ms: float
    mean_ms: float
    index_build_s: float | None = None


class EvalReport(BaseModel):
    dataset: str
    k: int
    embedding_model: str | None
    embedding_revision: str | None
    collection: str | None
    created_at: str
    results: list[CaseResult]
    mode: str = "dense"
    retriever: str | None = None  # description for non-dense modes
    timing: Timing | None = None

    @property
    def mean_recall(self) -> float:
        return sum(r.recall for r in self.results) / len(self.results)

    @property
    def hit_rate(self) -> float:
        return sum(r.hit for r in self.results) / len(self.results)

    @property
    def mrr(self) -> float:
        return sum(r.reciprocal_rank for r in self.results) / len(self.results)


def load_cases(path: Path) -> list[RetrievalCase]:
    cases = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith("//"):
            try:
                cases.append(RetrievalCase.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{n}: {exc}") from exc
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate case ids")
    return cases


def score_case(case: RetrievalCase, hits: list[SearchHit], k: int = DEFAULT_K) -> CaseResult:
    top = hits[:k]
    matched = [t for t in case.targets if any(t.matches(h.payload) for h in top)]
    first = next((h.rank for h in top if any(t.matches(h.payload) for t in case.targets)), None)
    return CaseResult(
        case=case,
        recall=len(matched) / len(case.targets),
        hit=bool(matched),
        reciprocal_rank=1 / first if first else 0.0,
        matched=[t.label() for t in matched],
        retrieved=[h.citation for h in top],
    )


def evaluate(
    cases: list[RetrievalCase],
    embedder: Embedder,
    store: ChunkStore,
    *,
    k: int = DEFAULT_K,
    dataset: str = "",
) -> EvalReport:
    results = []
    for case in cases:
        filters = SearchFilters(entity_terms=tuple(case.entity_terms))
        results.append(score_case(case, search(case.query, embedder, store, top_k=k,
                                               filters=filters), k))  # fmt: skip
    return EvalReport(
        dataset=dataset,
        k=k,
        embedding_model=embedder.model_name,
        embedding_revision=embedder.revision,
        collection=store.collection,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        results=results,
    )


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def evaluate_with(
    cases: list[RetrievalCase],
    retriever: Retriever,
    *,
    mode: str,
    description: str,
    k: int = DEFAULT_K,
    dataset: str = "",
    timed_passes: int = 0,
    index_build_s: float | None = None,
) -> EvalReport:
    """Score `retriever` exactly as `evaluate` scores dense search.

    The scored results come from the first pass, which doubles as the warm-up. With
    `timed_passes > 0`, every case is then timed `timed_passes` more times.
    """
    filters = [SearchFilters(entity_terms=tuple(c.entity_terms)) for c in cases]
    results = [
        score_case(c, retriever(c.query, f, k), k) for c, f in zip(cases, filters, strict=True)
    ]
    timing = None
    if timed_passes > 0:
        samples = []
        for _ in range(timed_passes):
            for c, f in zip(cases, filters, strict=True):
                started = time.perf_counter()
                retriever(c.query, f, k)
                samples.append((time.perf_counter() - started) * 1000)
        timing = Timing(
            passes=timed_passes,
            p50_ms=_percentile(samples, 0.50),
            p95_ms=_percentile(samples, 0.95),
            mean_ms=statistics.fmean(samples),
            index_build_s=index_build_s,
        )
    return EvalReport(
        dataset=dataset,
        k=k,
        embedding_model=None,
        embedding_revision=None,
        collection=None,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        results=results,
        mode=mode,
        retriever=description,
        timing=timing,
    )


def render_markdown(report: EvalReport) -> str:
    k = report.k
    if report.mode == "dense":
        title = f"# M1 retrieval benchmark: Recall@{k}"
        setup = (
            f"- Model: `{report.embedding_model}` @ `{report.embedding_revision}`; "
            f"collection `{report.collection}`; dense search, default eligibility filters"
        )
    else:
        title = f"# Retrieval benchmark ({report.mode}): Recall@{k}"
        setup = f"- Retrieval: {report.retriever}; default eligibility filters"
    t = report.timing
    latency = []
    if t is not None:
        build = f"; index build {t.index_build_s:.3f} s" if t.index_build_s is not None else ""
        latency = [
            f"- Latency per query ({t.passes} timed passes after a warm-up): p50 "
            f"{t.p50_ms:.1f} ms, p95 {t.p95_ms:.1f} ms, mean {t.mean_ms:.1f} ms{build}"
        ]
    lines = [
        title,
        "",
        f"- Dataset: `{report.dataset}` ({len(report.results)} queries)",
        setup,
        f"- Run: {report.created_at}",
        *latency,
        "",
        f"| Metric | Value |\n|---|---|\n| mean Recall@{k} | {report.mean_recall:.3f} |\n"
        f"| Hit@{k} | {report.hit_rate:.3f} |\n| MRR@{k} | {report.mrr:.3f} |",
        "",
        f"| id | query | Recall@{k} | first match rank | matched / targets |",
        "|---|---|---|---|---|",
    ]
    for r in report.results:
        rank = f"{1 / r.reciprocal_rank:.0f}" if r.reciprocal_rank else "-"
        lines.append(
            f"| {r.case.id} | {r.case.query} | {r.recall:.2f} | {rank} | "
            f"{len(r.matched)}/{len(r.case.targets)} |"
        )
    misses = [r for r in report.results if r.recall < 1]
    if misses:
        lines += ["", "## Targets not in the top k", ""]
        for r in misses:
            missing = [t.label() for t in r.case.targets if t.label() not in r.matched]
            lines.append(f"- **{r.case.id}** missing {', '.join(missing)}; "
                         f"retrieved: {'; '.join(r.retrieved)}")  # fmt: skip
    return "\n".join(lines) + "\n"


def report_json(report: EvalReport) -> str:
    body = report.model_dump(mode="json")
    body["summary"] = {"mean_recall": report.mean_recall, "hit_rate": report.hit_rate,
                       "mrr": report.mrr}  # fmt: skip
    return json.dumps(body, indent=2, ensure_ascii=False)
