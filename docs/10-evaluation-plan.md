# 10 — Evaluation & Testing

## Functional tests
Individual question -> correct entity; LLP -> LLP retrieval; private company -> company retrieval; missing entity -> clarification; form question -> multimodal path; policy-only -> form analysis skipped; unsupported question -> explicit uncertainty.

## Adversarial tests
Version conflict: when the 2016-based RBI FAQ (or the dated HDFC FAQ) and the 2025 RBI Directions differ, the system prefers the current one and flags the difference.
Prompt injection -> blocked/safe; malicious PDF instruction -> treated as data; system-prompt request -> refused; fake requirement -> not asserted; contradictory docs -> flagged; missing evidence -> unverified.

## Benchmark (start 50 cases, grow to 100+), stored in `data/eval/*.jsonl`
| Category | Cases |
|---|---|
| Normal entity-specific questions | 15 |
| Missing information | 10 |
| Entity classification | 5 |
| Prompt injection | 5 |
| Unsupported / contradictory claims | 10 |
| Multimodal form interpretation | 5 |
Label by hand from the real source PDFs (expected entity, expected evidence page(s), expected requirement set/uncertainty).

## M1 retrieval benchmark (implemented)
`data/eval/m1_retrieval.jsonl` has 20 queries with 28 target passages, written as natural customer/officer questions on topics the Stage D development probes did not use. Each target is `(document_key, physical page[, paragraph][, text_contains])`. Targets were labelled from the source text: every page and paragraph was checked against the stored chunk text before the first evaluation run, and the test suite checks that every target matches an eligible chunk. The labels were not changed after results were seen. One annotator: treat the numbers as a baseline, not a precise estimate.
- Matching: same document; the hit's page range covers the target page; paragraph label and `text_contains` must also match when given.
- Metrics: mean Recall@5 (matched targets / targets, per query), Hit@5 (≥ 1 target in the top 5), MRR@5. Dense BGE-small search with the production eligibility filter (primary + supporting).
- Run: `make eval` (`python -m app.ingestion.cli evaluate`).
- **Result (2026-11-08, `policy_chunks`, BGE-small-en-v1.5 @ `5c38ec7c…`):** Recall@5 **0.825**, Hit@5 **0.90**, MRR@5 **0.775**. Misses:
  - m1-01 (sole proprietorship): paras 33, 34 and 30 outrank para 31;
  - m1-05 (FPI): the p.50 half of the table is not in the top 5;
  - m1-06 (no OVD): 169MD para 28 is missing (FAQ Q6 found at rank 3);
  - m1-19 (company beneficial owner): the neighbouring definitions child ranks first;
  - m1-20 (NRO conversion): another chunk of the right form and page is found, not the labelled request sentence.

  These are the M2 baseline that hybrid BM25 + RRF must beat.

## M2 retrieval evaluation: development and held-out sets
M2 retrieval choices follow `docs/milestones/M2_PLAN.md` §6.
- **Development set:** `data/eval/m1_retrieval.jsonl` (20 queries). All choices are made on it: retrieval mode, tokenizer, RRF configuration, reranker model, depth and on/off. The dense baseline is pinned by `backend/tests/fixtures/m1_dense_golden.json` (top-5 ids per query; Recall@5 0.825, Hit@5 0.90, MRR@5 0.775).
- **Held-out set (frozen):** `data/eval/m2_heldout.jsonl`. It has 20 queries and 26 scoring targets, in the same format and matching rules as M1.
  - **SHA-256** (committed file, LF line endings): `55ba14d9893333b89cc260ac0ece3ac9115ebede28781aa98adfee4d54b0210c`. `tests/unit/test_m2_heldout.py` fails if the file changes.
  - **Use:** reporting only, never to choose or tune. It is run once for the selected configuration at Stage A3 (retrieval) and once at Stage B2 (reranker), each time with one fixed, named reference configuration. A disappointing held-out result is reported as is and does not reopen tuning.
  - **Label errors:** never fixed by editing this file. A correction goes into a new versioned file, and the original numbers stay reported.
  - **Labelling and validation:** labels were written from the stored source text before any retrieval mode was run on these queries. `tests/integration/test_m2_heldout_labels.py` checks that every scoring target maps to exactly one eligible chunk (after the case's entity filter), which is the chunk named in its notes.
  - **Coverage:** paraphrase, exact-term, entity-filtered (m2h-03 TRUST, m2h-04 PARTNERSHIP, m2h-05 SOCIETY), form (including one reliable-OCR form page, m2h-15) and FAQ-vs-Directions/policy cases.
  - **Overlap with the development set:** m2h-15 shares physical page 3 of the NRI re-KYC form with dev case m1-04, but it targets a different chunk, section and fact (token Jaccard 0.04).
- **Exact-term stress tests (intentional):** m2h-06 (UCIC), m2h-11 (UNSCR 1718) and m2h-12 (shell bank) contain their key term word for word. They test whether lexical retrieval helps with rare exact terms and are expected to favour BM25. They are reported as a subgroup as well as in the overall means.
- **Alternative evidence (non-scoring):**
  - **Where it is recorded:** where two passages give essentially the same answer, one is the canonical scoring target and the other is recorded only in `notes`, after the marker `ALTERNATIVE (non-scoring):`. This applies to m2h-02 (169MD ¶27(1)(v) scores; RBI FAQ Q20 is the alternative) and m2h-16 / m2h-17 (the NRI account opening form scores; the identical declaration in the resident-to-NRO conversion form is the alternative). The unchanged M1 loader reads the file because alternatives are not targets.
  - **How it is reported:** reports show `scoring_target_hit`, `alternative_evidence_hit` and scoring Recall@5 separately. **Alternative hits never count toward Recall@5, Hit@5 or MRR@5.** A query answered only by its alternative passage is scored as a miss and flagged in the report.
- **A1 result: BM25 alone on the development set** (2026-11-10, `make eval MODE=bm25`).
  - **Setup:** `rank_bm25` 0.2.2 BM25Okapi (k1 = 1.5, b = 0.75), plain tokenizer v1, 429 eligible Postgres chunks, production filters.
  - **Scores:** Recall@5 **0.692**, Hit@5 **0.80**, MRR@5 **0.633**, against dense 0.825 / 0.90 / 0.775. **BM25 alone is worse than dense on this set.**
  - **Misses:**
    - dense finds m1-04 (PAN/Form 60), m1-11, m1-12 and m1-17, which BM25 misses;
    - BM25 finds the m1-20 form request (rank 2), which dense misses;
    - both miss m1-01 para 31 and m1-05 p.50.
  - **No decision taken:** this is reported, not acted on. Nothing was tuned, and mode selection happens only in A3 under the pre-registered rule.
  - **Latency:** p50 ~110 ms per query, almost all of it the per-query freshness check; BM25 scoring itself is ~4 ms (docs/04).
- **A2 comparison on the development set** (2026-11-10, `make eval-compare`; real BGE model, dev collection; fixed A2 defaults: RRF k = 60, 50 candidates per retriever, plain tokenizer; 1 scored warm-up pass + 3 timed passes; Windows laptop CPU, indicative):

  | Mode | Recall@5 | Hit@5 | MRR@5 | p50 / p95 ms |
  |---|---|---|---|---|
  | dense | 0.825 | 0.900 | 0.775 | 43.0 / 53.7 |
  | bm25 | 0.692 | 0.800 | 0.633 | 13.2 / 16.2 |
  | hybrid | 0.858 | 0.900 | 0.704 | 63.2 / 73.3 |

  - **Hybrid vs dense:** 4 queries better on Recall@5 (m1-01, m1-06, m1-17 and m1-19 now complete, since BM25 lifts dense's misses), 3 worse (m1-04 PAN/Form 60 and m1-11 lose their only target; m1-12 drops to 0.67), 13 equal.
    - MRR@5 falls, from 0.775 to 0.704, because fused first-match ranks are often lower.
    - The Recall@5 gain (+0.033) is **below one query-equivalent (0.05)**, so on the pre-registered rule (M2_PLAN §6) it would not qualify. That decision belongs to A3 and isn't taken here.
  - **Regressions:** dense run through the new retriever reproduces M1 exactly (0.825 / 0.90 / 0.775), and BM25 reproduces A1 (0.692 / 0.80 / 0.633).
  - **Per-stage p50 latency in hybrid:** dense 48 ms (query embedding 33 ms, Qdrant 15 ms) + BM25 13 ms (freshness check 6 ms, scoring 6 ms) + fusion 0.6 ms. The stages run sequentially, so the latencies add.
  - **Freshness check:** about 6 ms per query, down from ~104 ms in A1, after the A2 switch to the `xmin` version check (docs/04).
  - **No configuration was selected or tuned, and the held-out set was not read.**
- **Limitations:**
  - **Single annotator:** all held-out labels were drafted by the implementing assistant and reviewed by the project owner. There was no independent annotation or inter-annotator validation, and no such claim is made.
  - **Resolution:** with 20 queries, one query moves mean Recall@5 by about 0.05, so confidence intervals are wide and "no measurable difference" is a valid outcome.

## Metrics
Retrieval **Recall@5**, **citation precision**, **unsupported-claim rate**, claim-verification precision/recall, evidence coverage, false-rejection rate, entity-classification accuracy, form-extraction accuracy, **security attack success rate**, end-to-end latency, token usage.

## Ablations
| Exp | Architecture |
|---|---|
| A | Basic RAG, no claim verification |
| B | RAG + metadata filtering |
| C | RAG + hybrid retrieval |
| D | RAG + claim-level NLI |
| E | Full pipeline (security + human review) |
Report accuracy, unsupported rate, citation quality, latency, tokens. **Don't assume E wins** — let the numbers decide and report honestly.

## Tooling
`pytest` for unit/integration; `make eval` runs the benchmark per config and writes `eval_runs/eval_results` + a markdown report. Keep LLM judge usage minimal (quota); prefer deterministic checks.
