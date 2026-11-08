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
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.retrieval.embeddings import Embedder
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.search import SearchFilters, SearchHit, search

DEFAULT_K = 5


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


class EvalReport(BaseModel):
    dataset: str
    k: int
    embedding_model: str
    embedding_revision: str | None
    collection: str
    created_at: str
    results: list[CaseResult]

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


def render_markdown(report: EvalReport) -> str:
    k = report.k
    lines = [
        f"# M1 retrieval benchmark: Recall@{k}",
        "",
        f"- Dataset: `{report.dataset}` ({len(report.results)} queries)",
        f"- Model: `{report.embedding_model}` @ `{report.embedding_revision}`; "
        f"collection `{report.collection}`; dense search, default eligibility filters",
        f"- Run: {report.created_at}",
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
