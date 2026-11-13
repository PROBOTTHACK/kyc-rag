"""HDFC-only retrieval evaluation for the assistant (A2).

Dataset: `data/eval/assistant_retrieval.jsonl`. Each case has a scope (opening, conversion,
context), a profile, and either gold evidence chunks or `expected: no_evidence`:
- `not_established`: the coverage map gives no documents, so **no search must run**;
- `absent_in_scope`: the scope is searched (it is covered) but the corpus contains no
  answer; the top results are listed for inspection, never scored as recall.

Recall@k is computed only over cases with gold evidence (no gold is ever manufactured).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.retrieval import AssistantRetrieval, AssistantRetriever
from app.assistant.taxonomy import AccountType, CustomerCategory, EvidenceScope

DATASET = Path(__file__).resolve().parents[3] / "data" / "eval" / "assistant_retrieval.jsonl"
K = 5
NO_SCORE = "-"


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    query: str = Field(min_length=3)
    scope: Literal["opening", "conversion", "context"]
    category: CustomerCategory | None
    account_type: AccountType | None
    expected: Literal["evidence", "no_evidence"]
    expected_reason: Literal["not_established", "absent_in_scope"] | None = None
    gold: tuple[str, ...]
    source_type: Literal[
        "opening_form", "scanned_opening_form", "conversion_form", "context", "none"
    ]
    tags: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> EvalCase:
        if self.expected == "evidence" and (not self.gold or self.expected_reason is not None):
            raise ValueError(f"{self.id}: an evidence case has gold chunks and no reason")
        if self.expected == "no_evidence" and (self.gold or self.expected_reason is None):
            raise ValueError(f"{self.id}: a no-evidence case has no gold and states why")
        if len(set(self.gold)) != len(self.gold):
            raise ValueError(f"{self.id}: duplicate gold chunk")
        if self.scope == "opening" and (self.category is None or self.account_type is None):
            raise ValueError(f"{self.id}: an opening case names its category and account type")
        if self.scope == "context" and self.category is None:
            raise ValueError(f"{self.id}: a context case names its category")
        return self


def load_cases(path: Path = DATASET) -> list[EvalCase]:
    cases = [
        EvalCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case ids")
    return cases


@dataclass(frozen=True)
class CaseResult:
    case: EvalCase
    retrieval: AssistantRetrieval
    top_ids: tuple[str, ...]
    recall: float | None  # None for no-evidence cases
    first_gold_rank: int | None
    passed_no_evidence: bool | None  # None for evidence cases
    metadata_complete: bool


def retrieve_case(retriever: AssistantRetriever, case: EvalCase, k: int = K) -> AssistantRetrieval:
    scope = EvidenceScope(case.scope)
    if scope is EvidenceScope.CONVERSION:
        return retriever.conversion(case.query, top_k=k)
    if case.category is None:  # EvalCase validation rules this out; fail loudly regardless
        raise ValueError(f"{case.id}: a {scope.value} case names its category")
    if scope is EvidenceScope.OPENING:
        if case.account_type is None:
            raise ValueError(f"{case.id}: an opening case names its account type")
        return retriever.opening(case.query, case.category, case.account_type, top_k=k)
    return retriever.context(case.query, case.category, top_k=k)


def evaluate_case(retriever: AssistantRetriever, case: EvalCase, k: int = K) -> CaseResult:
    r = retrieve_case(retriever, case, k)
    top = tuple(h.chunk_id for h in r.hits[:k])
    complete = all(
        h.document_key
        and h.page >= 1
        and h.source_role
        and h.text_reliability
        and h.extraction_method
        for h in r.hits
    )
    if case.expected == "evidence":
        found = [i for i, cid in enumerate(top, start=1) if cid in case.gold]
        recall = len({cid for cid in top if cid in case.gold}) / len(case.gold)
        return CaseResult(case, r, top, recall, found[0] if found else None, None, complete)
    if case.expected_reason == "not_established":
        passed = (not r.searched) and not r.hits
    else:  # absent_in_scope: correct behaviour is decided downstream (A4); retrieval stays in scope
        passed = r.searched
    return CaseResult(case, r, top, None, None, passed, complete)


@dataclass
class Aggregate:
    n: int = 0
    recall_sum: float = 0.0
    hits: int = 0
    rr_sum: float = 0.0

    def add(self, res: CaseResult) -> None:
        if res.recall is None:
            raise ValueError("only cases with gold evidence are aggregated")
        self.n += 1
        self.recall_sum += res.recall
        self.hits += res.first_gold_rank is not None
        self.rr_sum += 1 / res.first_gold_rank if res.first_gold_rank else 0.0

    def row(self, label: str) -> str:
        if not self.n:
            return f"| {label} | 0 | {NO_SCORE} | {NO_SCORE} | {NO_SCORE} |"
        return (
            f"| {label} | {self.n} | {self.recall_sum / self.n:.3f} | {self.hits / self.n:.3f} "
            f"| {self.rr_sum / self.n:.3f} |"
        )

    @property
    def recall(self) -> float:
        return self.recall_sum / self.n if self.n else 0.0


@dataclass
class Report:
    mode: str
    k: int
    results: list[CaseResult]
    overall: Aggregate = field(default_factory=Aggregate)
    by_source: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))
    by_category: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))
    by_account: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))
    by_scope: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))

    def __post_init__(self) -> None:
        for res in self.results:
            if res.recall is None:
                continue
            c = res.case
            self.overall.add(res)
            self.by_source[c.source_type].add(res)
            self.by_category[c.category.value if c.category else "(conversion)"].add(res)
            self.by_account[c.account_type.value if c.account_type else "(none)"].add(res)
            self.by_scope[c.scope].add(res)

    @property
    def no_evidence(self) -> list[CaseResult]:
        return [r for r in self.results if r.recall is None]

    @property
    def metadata_complete(self) -> bool:
        return all(r.metadata_complete for r in self.results)

    def as_record(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "k": self.k,
            "overall": {"cases": self.overall.n, "recall_at_k": round(self.overall.recall, 4)},
            "cases": [
                {
                    "id": r.case.id,
                    "recall_at_k": r.recall,
                    "first_gold_rank": r.first_gold_rank,
                    "no_evidence_passed": r.passed_no_evidence,
                    "searched": r.retrieval.searched,
                    "top": [
                        {
                            "chunk_id": h.chunk_id,
                            "document_key": h.document_key,
                            "page": h.page,
                            "source_role": h.source_role.value,
                            "text_reliability": h.text_reliability,
                        }
                        for h in r.retrieval.hits[: self.k]
                    ],
                }
                for r in self.results
            ],
        }


def run(retriever: AssistantRetriever, cases: Iterable[EvalCase], k: int = K) -> Report:
    return Report(retriever.mode.value, k, [evaluate_case(retriever, c, k) for c in cases])


def _hit_label(r: CaseResult, i: int) -> str:
    h = r.retrieval.hits[i]
    mark = "**gold**" if h.chunk_id in r.case.gold else ""
    return f"{h.document_key} p{h.page} [{h.text_reliability}] {h.chunk_id[:8]} {mark}".strip()


def render(report: Report) -> str:
    k = report.k
    head = f"| Group | Cases | Recall@{k} | Hit@{k} | MRR@{k} |\n|---|---|---|---|---|"
    out = [
        f"### Mode `{report.mode}` (k = {report.k})",
        "",
        head,
        report.overall.row("**Overall (cases with gold evidence)**"),
        "",
        "**By source type**",
        "",
        head,
        *(report.by_source[s].row(s) for s in sorted(report.by_source)),
        "",
        "**By customer category**",
        "",
        head,
        *(report.by_category[s].row(s) for s in sorted(report.by_category)),
        "",
        "**By account type**",
        "",
        head,
        *(report.by_account[s].row(s) for s in sorted(report.by_account)),
        "",
        "**Cases with missing or partial retrieval (Recall@k < 1)**",
        "",
    ]
    partial = [r for r in report.results if r.recall is not None and r.recall < 1]
    if not partial:
        out.append("None.")
    for r in partial:
        status = "no gold retrieved" if r.recall == 0 else f"recall {r.recall:.2f}"
        out.append(f"- `{r.case.id}` ({status}): {r.case.query}")
        out += [f"  - {i + 1}. {_hit_label(r, i)}" for i in range(len(r.retrieval.hits))]
    out += ["", "**Cases whose correct result is no supporting evidence**", ""]
    for r in report.no_evidence:
        verdict = "PASS" if r.passed_no_evidence else "FAIL"
        if r.case.expected_reason == "not_established":
            what = "no search run" if not r.retrieval.searched else "a search ran"
        else:
            what = f"{len(r.retrieval.hits)} in-scope result(s) returned for inspection"
        out.append(f"- `{r.case.id}` [{r.case.expected_reason}] {verdict}: {what}. {r.case.query}")
        if r.case.expected_reason == "absent_in_scope":
            out += [f"  - {i + 1}. {_hit_label(r, i)}" for i in range(len(r.retrieval.hits))]
    out += [
        "",
        f"Every returned result carries document key, page, source role, text reliability and "
        f"extraction method: **{'yes' if report.metadata_complete else 'NO'}**.",
    ]
    return "\n".join(out)


def to_json(report: Report) -> str:
    return json.dumps(report.as_record(), indent=2)
