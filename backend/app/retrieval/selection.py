"""M2 A3: retrieval configuration selection on the DEVELOPMENT set (M2_PLAN section 6).

Registered before any A3 result was seen, and never changed afterwards:

- Candidates (exactly seven): C0 dense; C1 / C2 BM25 plain / + stopwords; C3-C6 hybrid
  with tokenizer {plain, + stopwords} x RRF k {60, 20}. Fixed for all: 50 candidates per
  retriever, top-k 5, BM25Okapi k1=1.5 b=0.75, the pinned embedding model and revision,
  production filters plus each case's entity terms.
- Qualification: a non-dense candidate qualifies only if its mean Recall@5 exceeds dense
  by at least 1/20 (0.05) AND its Hit@5 is not below dense. Metrics are exact fractions,
  so float rounding can never move a borderline case. Hit@5 is a condition, never a
  tie-break.
- Ordering of qualifiers: highest Recall@5; then highest MRR@5; then simplicity
  (dense < bm25 plain < bm25 stopwords < hybrid plain < hybrid stopwords; k=60 and k=20
  equally simple); then lower p50 latency; then candidate id.
- If none qualifies, C0 dense remains selected.

The frozen held-out set plays no part here (docs/10); it is evaluated once, later, for the
selected configuration only.
"""

import random
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.retrieval.evaluation import EvalReport

RECALL_MARGIN = Fraction(1, 20)
FIXED_PARAMETERS: dict[str, Any] = {
    "candidates_per_retriever": 50,
    "top_k": 5,
    "bm25": "rank_bm25 BM25Okapi k1=1.5 b=0.75",
    "embedding": "pinned EMBEDDING_MODEL @ EMBEDDING_MODEL_REVISION",
    "filters": "production eligibility filters + each case's entity terms",
}
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261110
RULE_TEXT = (
    "A non-dense configuration qualifies only if mean Recall@5 improves on dense by at least "
    "0.05 and Hit@5 does not decrease. Qualifiers are ordered by highest Recall@5, then "
    "highest MRR@5, then simplicity, then lower p50 latency, then configuration id. If none "
    "qualifies, dense (C0) remains selected."
)


@dataclass(frozen=True)
class Candidate:
    id: str
    mode: str  # "dense" | "bm25" | "hybrid"
    stopwords: bool | None  # BM25 tokenizer variant (None for dense)
    rrf_k: int | None  # hybrid only
    simplicity: int  # lower = simpler

    @property
    def label(self) -> str:
        parts = [self.mode]
        if self.stopwords is not None:
            parts.append("stopwords" if self.stopwords else "plain")
        if self.rrf_k is not None:
            parts.append(f"k={self.rrf_k}")
        return " ".join(parts)


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("C0", "dense", None, None, 0),
    Candidate("C1", "bm25", False, None, 1),
    Candidate("C2", "bm25", True, None, 2),
    Candidate("C3", "hybrid", False, 60, 3),
    Candidate("C4", "hybrid", False, 20, 3),
    Candidate("C5", "hybrid", True, 60, 4),
    Candidate("C6", "hybrid", True, 20, 4),
)
BASELINE_ID = "C0"


@dataclass(frozen=True)
class ExactMetrics:
    recall: Fraction
    hit: Fraction
    mrr: Fraction
    per_query_recall: tuple[Fraction, ...]


def exact_metrics(report: EvalReport) -> ExactMetrics:
    """Mean Recall@k, Hit@k and MRR@k as exact fractions (from matched / target counts and
    first-match ranks; never from the float averages)."""
    n = len(report.results)
    if n == 0:
        raise ValueError("empty report")
    recalls = tuple(Fraction(len(r.matched), len(r.case.targets)) for r in report.results)
    hits = sum((Fraction(1) for r in report.results if r.hit), Fraction(0))
    rr = sum(
        (Fraction(1, round(1 / r.reciprocal_rank)) for r in report.results if r.reciprocal_rank),
        Fraction(0),
    )
    return ExactMetrics(sum(recalls, Fraction(0)) / n, hits / n, rr / n, recalls)


@dataclass
class Outcome:
    candidate: Candidate
    report: EvalReport
    metrics: ExactMetrics
    p50_ms: float
    p95_ms: float
    delta_recall: Fraction = Fraction(0)
    qualifies: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class Decision:
    selected: Outcome
    baseline: Outcome
    outcomes: list[Outcome]
    qualifiers: list[Outcome]  # in the rule's order (selected first, if any)


def outcome(candidate: Candidate, report: EvalReport) -> Outcome:
    t = report.timing
    if t is None:
        raise ValueError(f"{candidate.id}: report has no timing (needed for the tie-break)")
    return Outcome(candidate, report, exact_metrics(report), t.p50_ms, t.p95_ms)


def decide(outcomes: list[Outcome]) -> Decision:
    """Apply the registered rule exactly. `outcomes` must cover the registered candidates."""
    if [o.candidate for o in outcomes] != list(CANDIDATES):
        raise ValueError("outcomes must be exactly the registered candidates, in order")
    base = outcomes[0]
    for o in outcomes:
        o.delta_recall = o.metrics.recall - base.metrics.recall
        o.reasons = []
        if o is base:
            o.qualifies = False
            o.reasons.append("baseline")
            continue
        if o.delta_recall < RECALL_MARGIN:
            o.reasons.append(f"Recall@5 gain {float(o.delta_recall):+.4f} < 0.05")
        if o.metrics.hit < base.metrics.hit:
            o.reasons.append(
                f"Hit@5 {float(o.metrics.hit):.3f} below dense {float(base.metrics.hit):.3f}"
            )
        o.qualifies = not o.reasons
        if o.qualifies:
            o.reasons.append("qualifies")
    qualifiers = sorted(
        (o for o in outcomes if o.qualifies),
        key=lambda o: (-o.metrics.recall, -o.metrics.mrr, o.candidate.simplicity, o.p50_ms,
                       o.candidate.id),
    )  # fmt: skip
    return Decision(qualifiers[0] if qualifiers else base, base, outcomes, qualifiers)


def bootstrap_ci(
    a: tuple[Fraction, ...], b: tuple[Fraction, ...], *, resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:  # fmt: skip
    """Paired bootstrap 95% CI of mean(a) - mean(b) (reported only; never decides)."""
    if len(a) != len(b) or not a:
        raise ValueError("paired samples of equal, non-zero length required")
    diffs = [float(x - y) for x, y in zip(a, b, strict=True)]
    rng = random.Random(seed)  # noqa: S311 - statistical resampling, not cryptography
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def _rank(rr: float) -> str:
    return f"{1 / rr:.0f}" if rr else "-"


def tradeoff_rows(decision: Decision) -> list[str]:
    """Per-query evidence for the Recall@5 / MRR@5 trade-off of each hybrid candidate.

    For every query whose first-match rank or recall differs from dense: dense's and the
    candidate's first-match rank and recall, the original dense / BM25 ranks of the
    candidate's first matching chunk, and where dense's first matching chunk landed in the
    candidate's top 5 (`-` = pushed out) and in BM25's top 5."""
    base = decision.baseline.report.results
    bm25_top = {
        o.candidate.stopwords: [r.retrieved_ids for r in o.report.results]
        for o in decision.outcomes
        if o.candidate.mode == "bm25"
    }
    lines = []
    for o in decision.outcomes:
        if o.candidate.mode != "hybrid":
            continue
        lines += [
            "",
            f"**{o.candidate.id} ({o.candidate.label}) vs C0**",
            "",
            "| id | C0 rank | rank | C0 recall | recall | its first match: dense / bm25 rank "
            "| C0's first match: rank here / in BM25 top 5 |",
            "|---|---|---|---|---|---|---|",
        ]
        tops = bm25_top.get(o.candidate.stopwords, [])
        for i, (d, h) in enumerate(zip(base, o.report.results, strict=True)):
            if d.reciprocal_rank == h.reciprocal_rank and d.recall == h.recall:
                continue
            p = h.first_match_provenance or {}
            prov = f"{p.get('dense_rank') or '-'} / {p.get('bm25_rank') or '-'}" if p else "-"
            if d.first_match_id is None:
                moved = "-"
            else:
                here = (h.retrieved_ids.index(d.first_match_id) + 1
                        if d.first_match_id in h.retrieved_ids else "-")  # fmt: skip
                bm = (tops[i].index(d.first_match_id) + 1
                      if tops and d.first_match_id in tops[i] else "-")  # fmt: skip
                moved = f"{here} / {bm}"
            lines.append(
                f"| {d.case.id} | {_rank(d.reciprocal_rank)} | {_rank(h.reciprocal_rank)} "
                f"| {d.recall:.2f} | {h.recall:.2f} | {prov} | {moved} |"
            )
    return lines


def render_selection(decision: Decision, *, dataset: str, dataset_sha256: str) -> str:
    base = decision.baseline
    lines = [
        "# M2 A3: retrieval configuration selection (development set only)",
        "",
        f"- Dataset: `{dataset}` ({len(base.report.results)} queries), SHA-256 `{dataset_sha256}`",
        f"- Run: {base.report.created_at}",
        "- Fixed: " + "; ".join(f"{k} = {v}" for k, v in FIXED_PARAMETERS.items()),
        f"- Rule (pre-registered, M2_PLAN section 6): {RULE_TEXT}",
        "- The frozen held-out set was not read, loaded, queried, hashed or evaluated.",
        "",
        "| ID | Configuration | Recall@5 | Hit@5 | MRR@5 | ΔRecall@5 vs C0 | 95% CI (bootstrap) "
        "| wins / losses vs C0 | p50 ms | p95 ms | Qualifies? |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for o in decision.outcomes:
        m = o.metrics
        if o is base:
            ci, wl = "-", "-"
        else:
            lo, hi = bootstrap_ci(m.per_query_recall, base.metrics.per_query_recall)
            ci = f"[{lo:+.3f}, {hi:+.3f}]"
            pairs = list(zip(m.per_query_recall, base.metrics.per_query_recall, strict=True))
            wl = f"{sum(x > y for x, y in pairs)} / {sum(x < y for x, y in pairs)}"
        lines.append(
            f"| {o.candidate.id} | {o.candidate.label} | {float(m.recall):.3f} "
            f"| {float(m.hit):.3f} | {float(m.mrr):.3f} | {float(o.delta_recall):+.3f} | {ci} "
            f"| {wl} | {o.p50_ms:.1f} | {o.p95_ms:.1f} | {'; '.join(o.reasons)} |"
        )
    lines += ["", "## Per query: Recall@5 (first matching rank)", "",
              "| id | " + " | ".join(o.candidate.id for o in decision.outcomes) + " |",
              "|---|" + "---|" * len(decision.outcomes)]  # fmt: skip
    for i, r in enumerate(base.report.results):
        cells = [
            f"{o.report.results[i].recall:.2f} ({_rank(o.report.results[i].reciprocal_rank)})"
            for o in decision.outcomes
        ]
        lines.append(f"| {r.case.id} | " + " | ".join(cells) + " |")
    lines += ["", "## Recall@5 / MRR@5 trade-off evidence", *tradeoff_rows(decision)]
    sel = decision.selected
    lines += [
        "",
        "## Decision",
        "",
        f"- Qualifying configurations (in rule order): "
        f"{', '.join(o.candidate.id for o in decision.qualifiers) or 'none'}",
        f"- **Selected: {sel.candidate.id} ({sel.candidate.label})**"
        + ("" if decision.qualifiers else " - no configuration qualified, dense remains"),
    ]
    return "\n".join(lines) + "\n"


def selection_record(
    decision: Decision, *, dataset: str, dataset_sha256: str, code_commit: str
) -> dict[str, Any]:
    """Machine-readable decision record (committed before any held-out evaluation)."""

    def frac(x: Fraction) -> dict[str, Any]:
        return {"exact": f"{x.numerator}/{x.denominator}", "value": float(x)}

    sel = decision.selected.candidate
    return {
        "stage": "M2 A3",
        "dataset": dataset,
        "dataset_sha256": dataset_sha256,
        "code_commit": code_commit,
        "created_at": decision.baseline.report.created_at,
        "heldout": "not read, loaded, queried, hashed or evaluated during A3",
        "rule": RULE_TEXT,
        "fixed_parameters": FIXED_PARAMETERS,
        "selected": {
            "id": sel.id,
            "label": sel.label,
            "settings": {
                "RETRIEVAL_MODE": sel.mode,
                "BM25_STOPWORDS": bool(sel.stopwords),
                "RRF_K": sel.rrf_k if sel.rrf_k is not None else 60,
            },
            "dense_retained": not decision.qualifiers,
        },
        "qualifiers": [o.candidate.id for o in decision.qualifiers],
        "candidates": [
            {
                "id": o.candidate.id,
                "label": o.candidate.label,
                "recall_at_5": frac(o.metrics.recall),
                "hit_at_5": frac(o.metrics.hit),
                "mrr_at_5": frac(o.metrics.mrr),
                "delta_recall_vs_dense": frac(o.delta_recall),
                "p50_ms": round(o.p50_ms, 2),
                "p95_ms": round(o.p95_ms, 2),
                "qualifies": o.qualifies,
                "reasons": o.reasons,
            }
            for o in decision.outcomes
        ],
    }
