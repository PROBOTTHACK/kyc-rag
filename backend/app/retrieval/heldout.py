"""M2: the ONE-TIME held-out evaluation of the A3-selected configuration (docs/10).

Protocol (frozen in docs/milestones/M2_PLAN.md section 6 and docs/10; nothing here is
tunable):

- Only two configurations run: C0 (dense, the fixed reference) and the configuration the
  committed A3 decision record selected (`data/eval/m2_a3_selection.json`). The other
  candidates are never evaluated on held-out data.
- The held-out file must match its frozen SHA-256 (LF-normalised), or nothing runs.
- Exactly ONE scored pass per configuration. Latency is measured per query within that
  pass (no repeated timing passes over held-out queries). Before the pass, the services
  are warmed without any held-out query (model load, keyword index build).
- Alternative (non-scoring) evidence, recorded only in case notes after
  `ALTERNATIVE (non-scoring):`, is reported in its own columns and NEVER enters
  Recall@5, Hit@5 or MRR@5.
- The exact-term subgroup (m2h-06, m2h-11, m2h-12) is reported separately as well as in
  the overall means.
- A results record that already exists is never overwritten: the run is one-time.
"""

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.retrieval.evaluation import CaseResult, EvalReport, RetrievalCase, evaluate_retrieval
from app.retrieval.retrieve import RetrievalResult
from app.retrieval.search import SearchFilters
from app.retrieval.selection import CANDIDATES, Candidate, ExactMetrics, bootstrap_ci, exact_metrics

FROZEN_HELDOUT_SHA256 = "55ba14d9893333b89cc260ac0ece3ac9115ebede28781aa98adfee4d54b0210c"
ALT_MARKER = "ALTERNATIVE (non-scoring):"
EXACT_TERM_IDS = ("m2h-06", "m2h-11", "m2h-12")
REFERENCE_ID = "C0"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Run = Callable[[str, SearchFilters, int], RetrievalResult]


class HeldoutProtocolError(RuntimeError):
    """The frozen protocol would be violated (hash, record, repeat run, candidates)."""


def normalized_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def alternative_ids(notes: str | None) -> set[str]:
    """Chunk ids after the ALTERNATIVE marker (reporting only; never scored)."""
    _, found, tail = (notes or "").partition(ALT_MARKER)
    return set(_UUID.findall(tail)) if found else set()


def configurations(record: dict[str, Any]) -> list[Candidate]:
    """C0 and the configuration selected by the committed A3 record, in that order."""
    selected = record.get("selected", {}).get("id")
    by_id = {c.id: c for c in CANDIDATES}
    if selected not in by_id:
        raise HeldoutProtocolError(f"decision record selects unknown configuration {selected!r}")
    chosen = by_id[selected]
    want = record["selected"]["settings"]
    got = {"RETRIEVAL_MODE": chosen.mode, "BM25_STOPWORDS": bool(chosen.stopwords),
           "RRF_K": chosen.rrf_k if chosen.rrf_k is not None else 60}  # fmt: skip
    if got != want:
        raise HeldoutProtocolError(f"record settings {want} do not match registry {got}")
    return [by_id[REFERENCE_ID]] if selected == REFERENCE_ID else [by_id[REFERENCE_ID], chosen]


@dataclass
class ConfigRun:
    candidate: Candidate
    report: EvalReport
    metrics: ExactMetrics
    latencies_ms: list[float]
    alternative_hits: dict[str, bool]  # case id -> an alternative chunk is in the top 5

    @property
    def p50(self) -> float:
        return _pct(self.latencies_ms, 0.50)

    @property
    def p95(self) -> float:
        return _pct(self.latencies_ms, 0.95)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def run_once(
    cases: list[RetrievalCase], runs: dict[Candidate, Run], *, k: int = 5, dataset: str = ""
) -> list[ConfigRun]:
    """One scored pass per configuration; per-query latency measured inside that pass."""
    missing = set(EXACT_TERM_IDS) - {c.id for c in cases}
    if missing:  # checked before any retrieval
        raise HeldoutProtocolError(f"exact-term subgroup cases missing: {sorted(missing)}")
    out = []
    for cand, run in runs.items():
        latencies: list[float] = []

        def timed(
            q: str, f: SearchFilters, kk: int, run: Run = run, lat: list[float] = latencies
        ) -> RetrievalResult:
            started = time.perf_counter()
            result = run(q, f, kk)
            lat.append((time.perf_counter() - started) * 1000)
            return result

        report = evaluate_retrieval(cases, timed, mode=cand.mode, description=cand.label, k=k,
                                    dataset=dataset, timed_passes=0)  # fmt: skip
        if len(latencies) != len(cases):
            raise HeldoutProtocolError("each held-out query must be retrieved exactly once")
        alt = {
            r.case.id: bool(alternative_ids(r.case.notes) & set(r.retrieved_ids[:k]))
            for r in report.results
        }
        out.append(ConfigRun(cand, report, exact_metrics(report), latencies, alt))
    return out


def _sub(results: list[CaseResult], ids: tuple[str, ...]) -> tuple[Fraction, Fraction, Fraction]:
    rows = [r for r in results if r.case.id in ids]
    n = len(rows)
    rec = sum((Fraction(len(r.matched), len(r.case.targets)) for r in rows), Fraction(0)) / n
    hit = Fraction(sum(r.hit for r in rows), n)
    rr = sum((Fraction(1, round(1 / r.reciprocal_rank)) for r in rows if r.reciprocal_rank),
             Fraction(0)) / n  # fmt: skip
    return rec, hit, rr


def _rank(r: CaseResult) -> str:
    return f"{1 / r.reciprocal_rank:.0f}" if r.reciprocal_rank else "-"


def _rel(new: Fraction, old: Fraction) -> str:
    return f"{float((new - old) / old) * 100:+.1f}%" if old else "n/a"


def render_report(
    runs: list[ConfigRun], *, record: dict[str, Any], dataset: str, sha256: str, created_at: str
) -> str:
    ref, sel = runs[0], runs[-1]
    lines = [
        "# M2 held-out evaluation (one-time)",
        "",
        f"- Dataset: `{dataset}` ({len(ref.report.results)} queries), SHA-256 `{sha256}` "
        "(matches the frozen value)",
        f"- Run: {created_at}",
        f"- Configurations: {', '.join(f'{r.candidate.id} ({r.candidate.label})' for r in runs)}; "
        f"selected by the committed A3 record (code commit {record.get('code_commit', '?')[:7]})",
        "- One scored pass per configuration; per-query latency measured within that pass "
        "(services warmed beforehand without held-out queries).",
        "- Alternative (non-scoring) evidence is reported separately and never counted.",
        "",
        "| Metric | " + " | ".join(r.candidate.id for r in runs) + " | Δ (abs) | Δ (rel) |",
        "|---|" + "---|" * len(runs) + "---|---|",
    ]
    for name, attr in (("Recall@5", "recall"), ("Hit@5", "hit"), ("MRR@5", "mrr")):
        vals = [getattr(r.metrics, attr) for r in runs]
        d = vals[-1] - vals[0]
        lines.append(f"| {name} | " + " | ".join(f"{float(v):.3f}" for v in vals)
                     + f" | {float(d):+.3f} | {_rel(vals[-1], vals[0])} |")  # fmt: skip
    lines.append("| p50 latency ms | " + " | ".join(f"{r.p50:.1f}" for r in runs) + " | | |")
    lines.append("| p95 latency ms | " + " | ".join(f"{r.p95:.1f}" for r in runs) + " | | |")
    if len(runs) > 1:
        lo, hi = bootstrap_ci(sel.metrics.per_query_recall, ref.metrics.per_query_recall)
        pairs = list(zip(sel.metrics.per_query_recall, ref.metrics.per_query_recall, strict=True))
        win = sum(a > b for a, b in pairs)
        loss = sum(a < b for a, b in pairs)
        lines += [
            "",
            f"- ΔRecall@5 paired bootstrap 95% CI (10,000 resamples, seed 20261110): "
            f"[{lo:+.3f}, {hi:+.3f}] (reported only)",
            f"- Per query on Recall@5, {sel.candidate.id} vs {ref.candidate.id}: {win} better, "
            f"{loss} worse, {len(pairs) - win - loss} unchanged",
        ]
    lines += [
        "",
        "## Per query",
        "",
        "| id | " + " | ".join(f"{r.candidate.id} recall (rank)" for r in runs)
        + " | change | scoring target hit | alternative evidence hit "
        + f"| {sel.candidate.id} first match: dense / bm25 rank |",
        "|---|" + "---|" * len(runs) + "---|---|---|---|",
    ]  # fmt: skip
    for i, base in enumerate(ref.report.results):
        cells = [f"{r.report.results[i].recall:.2f} ({_rank(r.report.results[i])})" for r in runs]
        a, b = base, sel.report.results[i]
        if b.recall > a.recall:
            change = "win"
        elif b.recall < a.recall:
            change = "loss"
        elif b.reciprocal_rank > a.reciprocal_rank:
            change = "same recall, rank up"
        elif b.reciprocal_rank < a.reciprocal_rank:
            change = "same recall, rank down"
        else:
            change = "unchanged"
        hits = " / ".join("yes" if r.report.results[i].hit else "no" for r in runs)
        alts = " / ".join(
            ("yes" if r.alternative_hits[base.case.id] else "no")
            if alternative_ids(base.case.notes) else "n/a" for r in runs
        )  # fmt: skip
        p = b.first_match_provenance or {}
        prov = f"{p.get('dense_rank') or '-'} / {p.get('bm25_rank') or '-'}" if p else "-"
        tag = " (exact-term)" if base.case.id in EXACT_TERM_IDS else ""
        lines.append(f"| {base.case.id}{tag} | " + " | ".join(cells)
                     + f" | {change} | {hits} | {alts} | {prov} |")  # fmt: skip
    lines += ["", "## Exact-term subgroup (m2h-06, m2h-11, m2h-12)", "",
              "| Config | Recall@5 | Hit@5 | MRR@5 |", "|---|---|---|---|"]  # fmt: skip
    for r in runs:
        rec, hit, mrr = _sub(r.report.results, EXACT_TERM_IDS)
        lines.append(
            f"| {r.candidate.id} | {float(rec):.3f} | {float(hit):.3f} | {float(mrr):.3f} |"
        )
    lines += [
        "",
        "## Development vs held-out",
        "",
        "| Config | dev Recall@5 | dev Hit@5 | dev MRR@5 | held-out Recall@5 "
        "| held-out Hit@5 | held-out MRR@5 |",
        "|---|---|---|---|---|---|---|",
    ]
    dev = {c["id"]: c for c in record.get("candidates", [])}
    for r in runs:
        d = dev.get(r.candidate.id, {})
        lines.append(
            f"| {r.candidate.id} | {d.get('recall_at_5', {}).get('value', float('nan')):.3f} "
            f"| {d.get('hit_at_5', {}).get('value', float('nan')):.3f} "
            f"| {d.get('mrr_at_5', {}).get('value', float('nan')):.3f} "
            f"| {float(r.metrics.recall):.3f} | {float(r.metrics.hit):.3f} "
            f"| {float(r.metrics.mrr):.3f} |"
        )
    return "\n".join(lines) + "\n"


def results_record(
    runs: list[ConfigRun], *, record: dict[str, Any], dataset: str, sha256: str,
    code_commit: str, created_at: str,
) -> dict[str, Any]:  # fmt: skip

    def frac(x: Fraction) -> dict[str, Any]:
        return {"exact": f"{x.numerator}/{x.denominator}", "value": float(x)}

    out: dict[str, Any] = {
        "stage": "M2 held-out evaluation (one-time)",
        "dataset": dataset,
        "dataset_sha256": sha256,
        "frozen_sha256_verified": sha256 == FROZEN_HELDOUT_SHA256,
        "code_commit": code_commit,
        "decision_record_code_commit": record.get("code_commit"),
        "selected_configuration": record.get("selected", {}).get("id"),
        "created_at": created_at,
        "protocol": "one scored pass per configuration; C0 reference + selected only; "
        "alternative evidence never scored",
        "configurations": [],
    }
    for r in runs:
        sub = _sub(r.report.results, EXACT_TERM_IDS)
        out["configurations"].append({
            "id": r.candidate.id,
            "label": r.candidate.label,
            "recall_at_5": frac(r.metrics.recall),
            "hit_at_5": frac(r.metrics.hit),
            "mrr_at_5": frac(r.metrics.mrr),
            "p50_ms": round(r.p50, 2),
            "p95_ms": round(r.p95, 2),
            "exact_term_subgroup": {"recall_at_5": frac(sub[0]), "hit_at_5": frac(sub[1]),
                                    "mrr_at_5": frac(sub[2])},
            "per_query": [
                {"id": c.case.id, "recall": frac(Fraction(len(c.matched), len(c.case.targets))),
                 "first_match_rank": round(1 / c.reciprocal_rank) if c.reciprocal_rank else None,
                 "scoring_target_hit": c.hit,
                 "alternative_evidence_hit": r.alternative_hits[c.case.id]
                 if alternative_ids(c.case.notes) else None,
                 "first_match_provenance": c.first_match_provenance,
                 "retrieved_ids": c.retrieved_ids}
                for c in r.report.results
            ],
        })  # fmt: skip
    return out
