# A7: retrieval re-run for comparison with A2 (E7, not a gate)

Re-run on 2026-11-15 with `make assistant-eval MODE=dense|bm25|hybrid` (development corpus, the committed A2 set `data/eval/assistant_retrieval.jsonl`; the committed A2 record was **not** rewritten). Reports: `data/processed/assistant_retrieval_<mode>_report.md` (local, not committed).

| Mode | A2 Recall@5 | A7 Recall@5 |
|---|---|---|
| dense (default) | 0.733 | 0.733 |
| bm25 | 0.613 | 0.613 |
| hybrid (RRF, experimental) | 0.793 | 0.793 |

The results are identical to A2. The A2 limitations still apply: 25 scored cases, a single annotator, development set only, no held-out split; one case moves Recall@5 by up to 0.04. Retrieval adds only supplementary citations to checklist items (the catalogue cites its own stored chunks), so the checklist audit (coverage.md) is the evidence gate, not these figures.
