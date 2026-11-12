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
- **A3 selection protocol (registered before the A3 run; `app/retrieval/selection.py`):**
  - **The seven configurations, and only these:** C0 dense; C1 BM25 plain; C2 BM25 + stopwords; C3 hybrid plain k = 60; C4 hybrid plain k = 20; C5 hybrid stopwords k = 60; C6 hybrid stopwords k = 20.
  - **Fixed for all:** 50 candidates per retriever, top-k 5, BM25Okapi k1 = 1.5 / b = 0.75, the pinned embedding model and revision, production filters plus the case's entity terms.
  - **Stopwords:** the registered Lucene English list (docs/04), which removes "no" and "not".
  - **Qualification:** mean Recall@5 ≥ dense + 0.05 **and** Hit@5 not below dense. Computed with exact fractions. Hit@5 is a condition, never a tie-break.
  - **Order of qualifiers:** Recall@5, then MRR@5, then simplicity (dense < BM25 plain < BM25 stopwords < hybrid plain < hybrid stopwords; k = 60 and k = 20 equally simple), then p50 latency, then configuration ID. If none qualifies, C0 dense stays.
  - **Reported only, never decisive:** the paired bootstrap 95% CI of ΔRecall@5 (10,000 resamples, seed 20261110), per-query wins and losses, and per-query trade-off evidence (the original dense and BM25 ranks of the hybrid's first matching chunk, and where dense's first match landed).
  - **Run once:** `make eval-select` runs once, on the development set only. Its decision record (`data/eval/m2_a3_selection.json`, plus `docs/milestones/M2_A3_SELECTION.md`) is committed before any held-out evaluation.
  - **Exact-term subgroup:** m2h-06/11/12 are held-out cases, so they are reported **only** in the one-time held-out run, never during A3.
  - **Tests during A3:** `tests/unit/test_m2_heldout.py` and `tests/integration/test_m2_heldout_labels.py` are excluded (`--ignore`), and the held-out file is checked for changes through git metadata only. Both modules run again in the held-out verification stage.
  - **Negation limitation:** in the stopword variants, removing "no"/"not" changes lexical matching for negated queries. This is noted as a limitation of the experiment, not corrected after results.
- **A3 result and decision (development set, one run of `make eval-select` on 2026-11-10 at code commit `86d337e`; full record in `docs/milestones/M2_A3_SELECTION.md` and `data/eval/m2_a3_selection.json`):**

  | ID | Configuration | Recall@5 | Hit@5 | MRR@5 | ΔRecall@5 | p50 ms | Qualifies |
  |---|---|---|---|---|---|---|---|
  | C0 | dense | 0.825 | 0.900 | 0.775 | — | 34.3 | baseline |
  | C1 | BM25 plain | 0.692 | 0.800 | 0.633 | −0.133 | 9.3 | no (Recall, Hit) |
  | C2 | BM25 stopwords | 0.717 | 0.800 | 0.696 | −0.108 | 8.8 | no (Recall, Hit) |
  | C3 | hybrid plain k=60 | 0.858 | 0.900 | 0.704 | +0.033 | 52.4 | no (Recall) |
  | C4 | hybrid plain k=20 | 0.875 | 0.900 | 0.700 | +0.050 | 49.1 | yes (exactly 1/20) |
  | C5 | hybrid stopwords k=60 | 0.833 | 0.900 | 0.723 | +0.008 | 49.9 | no (Recall) |
  | C6 | hybrid stopwords k=20 | 0.900 | 0.950 | 0.735 | +0.075 | 49.2 | yes |

  - **Selected: C6** (hybrid, BM25 stopwords, RRF k = 20). It ranks above the other qualifier, C4, on the first key, Recall@5.
  - **Settings:** they now default to C6; `--mode dense` stays the M1 path.
  - **Trade-off:** every hybrid has a higher Recall@5 but a lower MRR@5 than dense.
    - RRF promotes passages both retrievers rank moderately (m1-01, m1-20), lifting Recall at ranks 3–4.
    - It demotes correct dense #1 passages that BM25 ranks poorly (m1-17, m1-04).
    - It loses m1-11 in every hybrid.
  - **Uncertainty:** every bootstrap CI includes zero (C6: [−0.125, +0.275]). On 20 single-annotator queries, C6 is not statistically distinguishable from dense.
  - **Held-out next:** a separate stage runs the held-out set once, for C6 with C0 as the fixed reference, including the exact-term subgroup.
- **Held-out result (the one-time run, 2026-11-10, commit `b701baa`; `docs/milestones/M2_HELDOUT_EVALUATION.md`, `data/eval/m2_heldout_results.json`):**
  - **Validation first:** both held-out test modules passed (4/4, 3/3) and the frozen SHA-256 matched.
  - **What ran:** only C0 and C6, one scored pass each.

  | Config | Recall@5 | Hit@5 | MRR@5 | p50 / p95 ms |
  |---|---|---|---|---|
  | C0 dense | 0.900 | 0.900 | 0.850 | 46.5 / 60.7 |
  | C6 hybrid stopwords k=20 | 0.875 | 0.950 | 0.867 | 65.5 / 72.5 |

  - **The development Recall@5 gain did not generalize:** −0.025 on held-out against +0.075 on development. The paired 95% CI is [−0.125, +0.050].
  - **Per query:** 1 better, 2 worse, 17 unchanged. Hit@5 is +0.050 and MRR@5 +0.017.
  - **Exact-term subgroup** (m2h-06/11/12): 1.000 for both configurations, a ceiling.
  - **Failure mode:** the stopword BM25 leg promotes HDFC form chunks, one of them OCR-uncertain, over policy and FAQ answers (m2h-03, m2h-18).
  - **What follows:** as pre-registered, the result is reported as is and does not reopen tuning. The A3 decision record is unchanged, and whether to keep C6 as the configured default is the project owner's call.
- **Operational default after the held-out review** (a separate decision; `docs/milestones/M2_RETRIEVAL_OPERATIONAL_DECISION.md`):
  - **Default:** `RETRIEVAL_MODE=dense`.
  - **C6:** stays an explicitly selectable experimental mode (`--mode hybrid` with `RRF_K=20`, `BM25_STOPWORDS=true`).
  - **Why:**
    - Recall@5 is not better on held-out (−0.025);
    - the Hit@5 (+0.050) and MRR@5 (+0.017) gains are one query or less;
    - no difference is statistically significant;
    - C6 adds +19 ms at p50;
    - C6 has a documented bias towards form and OCR-uncertain chunks, plus the negation risk of the stopword list.
  - **Records unchanged:** the A3 record and the held-out results are unchanged.
  - **Arithmetic check:** a post-evaluation review recomputed every reported figure from the raw results and found no arithmetic error; only wording was clarified.
- **M2 B2: reranker diagnostic.** This is a diagnostic on a **previously used** development set: **not independent validation and not evidence of generalization.**
  - **Run:** once, 2026-11-10, `eval/m2_b2_rerank_diagnostic.py`; record `data/eval/m2_b2_rerank_diagnostic.json`.
  - **Setup:**
    - R0: dense, top 5.
    - R1: dense, 20 candidates → `cross-encoder/ms-marco-MiniLM-L6-v2` @ `233902d2…` on CPU → top 5.
    - The M1 development set only (`m1_retrieval.jsonl`, 20 queries); 1 scored pass + 3 timed passes; nothing tuned.

  | | Recall@5 | Hit@5 | MRR@5 | total p50 / p95 ms | rerank p50 / p95 ms |
  |---|---|---|---|---|---|
  | R0 dense | 0.825 (33/40) | 0.900 | 0.775 (31/40) | 42.9 / 52.9 | — |
  | R1 dense + rerank | **0.950** (19/20) | **1.000** | **0.796** (191/240) | **1131.1 / 1447.4** | **1072.9 / 1388.1** |
  | Δ R1 − R0 | +0.125 (+1/8) | +0.100 | +0.021 (+1/48) | about 26× slower | |

  - **Ceiling:** dense Recall@20 is **1.000**, so every labelled target was among the 20 candidates and the reranker could in principle recover all of them.
  - **Model load:** 1,675 ms, once, measured separately on a warm-up text.
  - **Per query (Recall@5):** 4 wins, 1 loss, 15 unchanged.
    - Wins: m1-01 (0 → 1, rank 3), m1-05 (0.5 → 1), m1-06 (0.5 → 1, rank 3 → 2), m1-20 (0 → 1, rank 2).
    - Loss: m1-04 (1 → 0.5, rank 1 → 2). The NRI re-KYC "Form 60" target (dense rank 5) was displaced by RBI para 43, an unlabelled passage that also concerns Form No. 60.
    - Other first-correct-hit rank change: m1-19, rank 3 → 4 (same recall).
  - **Reading:**
    - On this reused set, the reranker recovers most targets dense ranked 6–20. The Recall and Hit gains are large; the MRR gain is small.
    - **On this laptop CPU, reranking 20 candidates costs about 1.1 s per query (p50), against about 43 ms for dense.**
    - No generalization claim is made: M2 A3 showed how a dev-set gain can fail to hold on fresh data.
  - **Decision:** `RERANK_ENABLED` stays `false` and dense remains the operational default. Reranking stays opt-in (`--rerank`). Nothing was tuned: the candidate depth, model and batch size are as registered in B1.
- **Limitations:**
  - **Single annotator:** all held-out labels were drafted by the implementing assistant and reviewed by the project owner. There was no independent annotation or inter-annotator validation, and no such claim is made.
  - **Resolution:** with 20 queries, one query moves mean Recall@5 by about 0.05, so confidence intervals are wide and "no measurable difference" is a valid outcome.

## M2 outcome (close-out, 2026-11-11)
- **Retrieval decision:**
  - **What ships:** dense BGE-small stays the operational default. BM25 and hybrid (RRF) are implemented and selectable (`--mode bm25|hybrid`).
  - **Why:** on the frozen held-out set the dev-selected hybrid C6 did not beat dense by the pre-registered margin, and every difference was inside the uncertainty of a 20-query single-annotator set (`docs/milestones/M2_HELDOUT_EVALUATION.md`, `M2_RETRIEVAL_OPERATIONAL_DECISION.md`). This meets the roadmap criterion "hybrid ≥ dense baseline (or documented why not)" by the second branch.
- **Reranker:** an optional MiniLM cross-encoder (`--rerank`), off by default. One diagnostic on the reused dev set only (above); it is not a validation.
- **Form extraction:** the roadmap's "form-extraction accuracy" metric is **not** measured. There is no labelled field set. What is reported is per-form corroboration of model labels against the stored M1 page text (docs/04 live tables). That shows a label appears on the form; it does not show correctness or completeness.
- **Regression gates:** `make check-dense-golden` stays green for every M2 change; the M1 dense results are unchanged.

Retrieval **Recall@5**, **citation precision**, **unsupported-claim rate**, claim-verification precision/recall, evidence coverage, false-rejection rate, entity-classification accuracy, form-extraction accuracy, **security attack success rate**, end-to-end latency, token usage.

## M4 claim-verification datasets (M4 S3)
Labelling rules, fields, perturbation tags, leakage controls and limitations: `docs/milestones/M4_LABELLING_GUIDELINES.md`.
- **Development set:** `data/eval/m4_claims_dev.jsonl`, 55 cases (15 entailed / 15 contradicted / 25 insufficient) after the owner review of 2026-11-12. Decisions are in the ledger `data/eval/m4_claims_dev_review.jsonl`. All S4 choices (NLI checkpoint, thresholds) are made on it under a rule registered before the run.
- **Held-out set (frozen):** `data/eval/m4_claims_heldout.jsonl`, 51 cases (17 / 17 / 17).
  - **SHA-256** (committed file, LF line endings): `eee0a575319d7df4bc3d8f5c0771a89e4c8bf9f68abe36eb56fcac3275d90203`. `tests/unit/test_m4_datasets.py` fails if the file changes.
  - **Use:** run once in S8, for the configuration already selected on dev. Never used to choose or tune anything. A disappointing result is reported as is.
  - **Label errors:** corrected in a new versioned file, never by editing this one. Owner verdicts and known issues go to the ledger `data/eval/m4_claims_heldout_review.jsonl`, so the frozen file never changes. Six issues under the revised rules 1 and 10 are logged there (m4h-007, 008, 009, 010, 011, 022) for a separately versioned correction at S8.
- **Leakage controls (tested):**
  - no evidence chunk is cited by both splits (dev 23 chunks, held-out 28);
  - no cross-split claim pair reaches token Jaccard 0.8 (maximum 0.786: the deliberate shared pattern "the X deed must be obtained for opening an account of a Y" on different entities and evidence);
  - no duplicate case within a split.
- **Provenance (tested against Postgres, `tests/integration/test_m4_dataset_labels.py`):**
  - every chunk exists, with the recorded document key and page;
  - every quote is an exact substring of the stored text;
  - cited chunks are retrievable units, never `context_only` or parents;
  - the tags `ocr_uncertain`, `source_status` and `form_only` match the stored metadata;
  - `expected_obligation` cases cite only primary, active, non-OCR-uncertain chunks.
- **Metrics to compute (S4 on dev, S8 on held-out):** per-class precision/recall, false-support rate (primary safety metric), false-contradiction rate, abstention and risk–coverage, obligation-from-form errors (must be 0), and the agreement of `expected_obligation` with the decision table's MANDATORY.
- **Limitations:** a single annotator (the implementing assistant) with owner review only, no inter-annotator agreement, 17 cases per class, synthetic claims, a corpus with one primary document. Entity-group cases are thin (1 per split). The dev classes are unbalanced after the owner's corrections, so S4 must report per-class rates, not accuracy alone.

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
