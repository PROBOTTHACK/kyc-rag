"""M2 B2: the single, bounded reranker DIAGNOSTIC (docs/milestones/M2_REMAINING_PLAN.md).

R0: dense retrieval, top 5.  R1: dense, 20 candidates -> MiniLM cross-encoder -> top 5.
Dataset: the previously used M1 development set ONLY (`data/eval/m1_retrieval.jsonl`).
This is a diagnostic on a reused dataset, NOT independent validation and NOT evidence of
generalization. Nothing is tuned; the reranker stays disabled by default whatever the
result. One run: refuses to run again once its results record exists.

Run from `backend/` (CPU, pinned models from the settings):
    PYTHONPATH=. ../.venv/Scripts/python.exe ../eval/m2_b2_rerank_diagnostic.py
"""

import json
import sys
import time
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

from app.config import REPO_ROOT, get_settings
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.retrieval.embeddings import embedder_from_settings
from app.retrieval.evaluation import (
    EvalReport,
    evaluate_retrieval,
    load_cases,
    score_case,
)
from app.retrieval.keyword import KeywordIndexManager
from app.retrieval.qdrant_store import store_from_settings
from app.retrieval.rerank import reranker_from_settings
from app.retrieval.retrieve import RetrievalResult, Retriever
from app.retrieval.search import SearchFilters
from app.retrieval.selection import exact_metrics

DATASET = "data/eval/m1_retrieval.jsonl"
RECORD = REPO_ROOT / "data" / "eval" / "m2_b2_rerank_diagnostic.json"
TIMED_PASSES = 3  # as in M2 A2/A3: 1 scored (warm-up) pass + 3 timed passes
K, CEILING_K = 5, 20


def frac(x: Fraction) -> dict[str, Any]:
    return {"exact": f"{x.numerator}/{x.denominator}", "value": float(x)}


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def main() -> int:
    if RECORD.exists():
        print(f"refused: {RECORD} exists; the B2 diagnostic is run once")
        return 2
    s = get_settings()
    if s.RERANK_ENABLED or s.RETRIEVAL_MODE != "dense":
        print("refused: expected the operational defaults (dense, reranking off)")
        return 2
    cases = load_cases(REPO_ROOT / DATASET)
    sessions = create_sync_sessionmaker(create_sync_engine(s))
    store = store_from_settings(s)
    embedder = embedder_from_settings(s)
    reranker = reranker_from_settings(s)

    # Warm both models WITHOUT any dataset query; the reranker load time is measured here.
    _ = embedder.dimension
    embedder.embed_query("warm-up query for model initialisation")
    started = time.perf_counter()
    reranker.score("warm-up query", ["warm-up passage for model initialisation"])
    load_wall_ms = (time.perf_counter() - started) * 1000

    r = Retriever(embedder=lambda: embedder, store=lambda: store,
                  keyword=lambda: KeywordIndexManager(sessions), reranker=lambda: reranker,
                  rerank_candidates=s.RERANK_CANDIDATES)  # fmt: skip

    def r0(q: str, f: SearchFilters, k: int) -> RetrievalResult:
        return r.retrieve(q, mode="dense", top_k=k, filters=f)

    def r1(q: str, f: SearchFilters, k: int) -> RetrievalResult:
        return r.retrieve(q, mode="dense", top_k=k, filters=f, rerank=True)

    rep0 = evaluate_retrieval(cases, r0, mode="dense", description="R0 dense top 5", k=K,
                              dataset=DATASET, timed_passes=TIMED_PASSES)  # fmt: skip
    rep1 = evaluate_retrieval(cases, r1, mode="dense+rerank",
                              description=f"R1 dense {s.RERANK_CANDIDATES} -> {s.RERANK_MODEL} "
                              f"@ {s.RERANK_MODEL_REVISION} -> top 5", k=K, dataset=DATASET,
                              timed_passes=TIMED_PASSES)  # fmt: skip
    # The reranker's ceiling: dense Recall@20 over the same filtered candidates.
    ceiling = [
        score_case(c, r0(c.query, SearchFilters(entity_terms=tuple(c.entity_terms)), CEILING_K)
                   .hits, CEILING_K)
        for c in cases
    ]  # fmt: skip
    rec20 = sum((Fraction(len(c.matched), len(c.case.targets)) for c in ceiling), Fraction(0))
    rec20 /= len(ceiling)

    m0, m1 = exact_metrics(rep0), exact_metrics(rep1)
    rows, wins, losses = [], 0, 0
    for a, b, c20 in zip(rep0.results, rep1.results, ceiling, strict=True):
        ra = round(1 / a.reciprocal_rank) if a.reciprocal_rank else None
        rb = round(1 / b.reciprocal_rank) if b.reciprocal_rank else None
        if b.recall > a.recall:
            change, wins = "win", wins + 1
        elif b.recall < a.recall:
            change, losses = "loss", losses + 1
        elif ra != rb:
            change = "same recall, rank " + ("up" if (rb or 99) < (ra or 99) else "down")
        else:
            change = "unchanged"
        rows.append({"id": a.case.id, "r0_recall": a.recall, "r1_recall": b.recall,
                     "r0_first_rank": ra, "r1_first_rank": rb, "recall_at_20": c20.recall,
                     "change": change,
                     "r1_first_match_pre_rerank_rank":
                         (b.first_match_provenance or {}).get("pre_rerank_rank"),
                     "r0_top5": a.retrieved, "r1_top5": b.retrieved})  # fmt: skip

    def timing(rep: EvalReport) -> dict[str, Any]:
        t = rep.timing
        if t is None:
            raise RuntimeError("timed passes missing")
        return {"total": {"p50": t.p50_ms, "p95": t.p95_ms}, "stages": t.stages}

    record = {
        "stage": "M2 B2 reranker diagnostic (previously used dev set; not a validation)",
        "dataset": DATASET,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": {"embedding": f"{s.EMBEDDING_MODEL}@{s.EMBEDDING_MODEL_REVISION}",
                   "reranker": f"{s.RERANK_MODEL}@{s.RERANK_MODEL_REVISION}", "device": "cpu"},
        "candidates": s.RERANK_CANDIDATES, "top_k": K, "timed_passes": TIMED_PASSES,
        "reranker_load_ms": {"measured_wall": round(load_wall_ms, 1),
                             "model_reported": round(reranker.load_ms, 1)},
        "R0": {"recall_at_5": frac(m0.recall), "hit_at_5": frac(m0.hit), "mrr_at_5": frac(m0.mrr),
               "latency": timing(rep0)},
        "R1": {"recall_at_5": frac(m1.recall), "hit_at_5": frac(m1.hit), "mrr_at_5": frac(m1.mrr),
               "latency": timing(rep1)},
        "dense_recall_at_20": frac(rec20),
        "per_query": rows,
        "summary": {"wins": wins, "losses": losses, "unchanged_recall": len(rows) - wins - losses},
    }  # fmt: skip
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    st0, st1 = rep0.timing.stages if rep0.timing else {}, rep1.timing.stages if rep1.timing else {}
    print(f"R0 dense       R@5 {float(m0.recall):.3f} H@5 {float(m0.hit):.3f} "
          f"MRR@5 {float(m0.mrr):.3f}  total p50/p95 {st0['total']['p50']:.1f}/"
          f"{st0['total']['p95']:.1f} ms")  # fmt: skip
    print(f"R1 dense+rerank R@5 {float(m1.recall):.3f} H@5 {float(m1.hit):.3f} "
          f"MRR@5 {float(m1.mrr):.3f}  total p50/p95 {st1['total']['p50']:.1f}/"
          f"{st1['total']['p95']:.1f} ms; rerank p50/p95 {st1['rerank']['p50']:.1f}/"
          f"{st1['rerank']['p95']:.1f} ms")  # fmt: skip
    print(
        f"dense Recall@20 (ceiling) {float(rec20):.3f}; reranker load {load_wall_ms:.0f} ms; "
        f"wins {wins}, losses {losses}, unchanged recall {len(rows) - wins - losses}"
    )
    for row in rows:
        print(
            f"  {row['id']}: R0 {row['r0_recall']:.2f} (rank {row['r0_first_rank']}) -> "
            f"R1 {row['r1_recall']:.2f} (rank {row['r1_first_rank']})  @20 "
            f"{row['recall_at_20']:.2f}  {row['change']}"
        )
    print(f"record written to {RECORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
