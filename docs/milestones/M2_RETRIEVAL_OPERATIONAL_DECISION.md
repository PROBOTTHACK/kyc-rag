# M2 Retrieval: Operational Default After the Held-out Review

**Decision (2026-11-10, after the one-time held-out evaluation): the runtime default is `dense` again** (`RETRIEVAL_MODE=dense`). C6 (hybrid, BM25 stopword variant, RRF k = 20) stays available as an explicitly selectable experimental mode: `--mode hybrid`, or `RETRIEVAL_MODE=hybrid`. With the configured `RRF_K=20` and `BM25_STOPWORDS=true`, that mode is exactly C6.

This is a **separate, post-held-out operational decision.** It does not rewrite the experiment.
- **The A3 selection stands as recorded:** C6 was selected on the development set by the pre-registered rule (`data/eval/m2_a3_selection.json`, `docs/milestones/M2_A3_SELECTION.md`, commit `0a51add`). Both files are unchanged.
- **The held-out run stands as recorded:** `data/eval/m2_heldout_results.json`, `docs/milestones/M2_HELDOUT_EVALUATION.md`, commit `0eef277`.
- **What follows:** no data set was re-used, no experiment was re-run, and nothing was tuned.

## Evidence (held-out set, 20 queries; C0 dense vs C6)

| Metric | C0 | C6 | C6 − C0 |
|---|---|---|---|
| Recall@5 | 0.900 (9/10) | 0.875 (7/8) | −0.025 (−1/40) |
| Hit@5 | 0.900 | 0.950 | +0.050 (one query, m2h-01) |
| MRR@5 | 0.850 (17/20) | 0.867 (13/15) | +0.017 (1/60) |
| p50 latency | 46.5 ms | 65.5 ms | +19.0 ms (+41%) |
| p95 latency | 60.7 ms | 72.5 ms | +11.8 ms (+19%) |

- **Per query (Recall@5):**
  - 1 better (m2h-01), 2 worse (m2h-03, m2h-18), 17 unchanged.
  - First correct hit: up in 2 queries (m2h-05, m2h-14), down in 2 (m2h-08, m2h-20).
- **Exact-term subgroup** (m2h-06, m2h-11, m2h-12): 1.000 for both. Dense already ranks all three first.
- **No significance:** the ΔRecall@5 paired bootstrap 95% CI is [−0.125, +0.050]. **None of the differences is statistically significant on 20 single-annotator queries.** That applies equally to dense's small Recall@5 lead and to C6's small Hit@5 and MRR@5 leads.

## Reasoning

1. **The selection premise didn't hold.** C6 was selected for a Recall@5 gain on the development set (+0.075). That gain did not generalize: on held-out data, C6's Recall@5 is 0.025 *below* dense. Its Hit@5 and MRR@5 advantages amount to one query or less, and nothing distinguishes the two configurations statistically.
2. **Equal evidence, so the simpler and cheaper option.** On held-out latency, C6 costs about +19 ms at p50 and +12 ms at p95 because its two retrievers run one after the other. It also adds a Postgres dependency to every query.
3. **A documented quality risk specific to C6.** Its stopword BM25 side promotes HDFC *form* chunks, including OCR-uncertain text, above policy and FAQ answers:
   - m2h-03 loses the HDFC trust-documents row to an OCR-uncertain account-form chunk;
   - m2h-18 loses the HDFC "when does KYC apply" passage;
   - in m2h-08 and m2h-20, a form page takes rank 1.

   For a compliance engine whose primary evidence is policy and regulatory text, this displacement is the wrong direction. Form chunks are only ever `supporting` evidence, and uncertain OCR can never support a MANDATORY requirement.
4. **The negation risk is unmitigated.** The stopword list removes "no" and "not", so BM25 treats a negated question like its positive form. No held-out query showed harm, but the benchmark wasn't built to test this, and the risk applies to every hybrid query under C6.
5. **What C6 does well is real but narrow.** It recovers passages dense ranks far down when BM25 ranks them highly (m2h-01 on held-out; m1-01 and m1-20 on development). That is a reason to keep it **available** for experiments and for later stages (for example as an input to M2 B reranking), not to make it the default.

## What changed

- `backend/app/config.py`, `infra/.env.example`: `RETRIEVAL_MODE` default `hybrid` → `dense`. `RRF_K=20` and `BM25_STOPWORDS=true` are unchanged, so `--mode hybrid` is C6.
  - Note: `BM25_STOPWORDS` also applies to `--mode bm25` (C2 rather than C1). Set `BM25_STOPWORDS=false` for the plain tokenizer.
- **Tests:** `tests/unit/test_selection.py` now asserts that the A3 record still selects C6, that `--mode hybrid` with the configured settings equals C6, and that the operational default is dense.
- **Docs:** this file, `docs/04`, `docs/10`, the README, and wording clarifications in `docs/milestones/M2_HELDOUT_EVALUATION.md` (no number changed).

## Revisiting

Reconsider hybrid as the default only with **new** labelled data, since both the development and held-out sets are spent for this decision. The questions to test are a non-stopword or form-aware lexical side, and reranking (M2 B) on top of the hybrid candidate set.
