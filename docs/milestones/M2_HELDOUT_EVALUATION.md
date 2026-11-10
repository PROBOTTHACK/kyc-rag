# M2 Held-out Evaluation (one-time)

**Result: the development-set Recall@5 improvement of the selected configuration did not generalize.**
- On the frozen held-out set, C6 (hybrid, BM25 stopword variant, RRF k = 20) reaches Recall@5 **0.875**, against **0.900** for the dense reference C0 (−0.025).
- On the development set, the same comparison was +0.075.
- C6 is slightly better on Hit@5 (+0.050, one query) and MRR@5 (+0.017).
- Every difference lies within the uncertainty of a 20-query, single-annotator benchmark.

**Protocol** (frozen in `docs/milestones/M2_PLAN.md` §6 and `docs/10-evaluation-plan.md`):
- run **once**, 2026-11-10 09:49 UTC, `make eval-heldout` at commit `b701baa`;
- only C0 (the fixed reference) and C6 (selected by the committed A3 record `data/eval/m2_a3_selection.json`, decision commit `0a51add`) were evaluated, with one scored pass each;
- C1–C5 were not evaluated, and nothing was tuned or re-selected.

**Pre-run validation:**
- `tests/unit/test_m2_heldout.py` 4/4 passed; `tests/integration/test_m2_heldout_labels.py` 3/3 passed.
- The held-out SHA-256 `55ba14d9…0210c` matched in the committed blob, the normalised working copy, `docs/10` and the test constant.
- Git history: `a99cc9a` only.

**Records:**
- `data/eval/m2_heldout_results.json`: exact-fraction metrics, per-query results, provenance, retrieved IDs, alternative-evidence flags.
- The generated report is reproduced verbatim below.
- The A3 decision record and `docs/milestones/M2_A3_SELECTION.md` are unchanged.

## Results

| Metric | C0 dense | C6 hybrid | Δ absolute | Δ relative | Development Δ (A3) |
|---|---|---|---|---|---|
| Recall@5 | 0.900 | 0.875 | **−0.025** | −2.8% | +0.075 |
| Hit@5 | 0.900 | 0.950 | +0.050 | +5.6% | +0.050 |
| MRR@5 | 0.850 | 0.867 | +0.017 | +2.0% | −0.040 |
| p50 latency | 46.5 ms | 65.5 ms | +19.0 ms | +41% | +14.9 ms |
| p95 latency | 60.7 ms | 72.5 ms | +11.8 ms | +19% | +13.8 ms |

- **Bootstrap CI:** the paired 95% CI of ΔRecall@5 is **[−0.125, +0.050]**. It includes zero, so there is no evidence that C6 differs from dense on Recall.
- **Per query (Recall@5):** 1 better (m2h-01), 2 worse (m2h-03, m2h-18), 17 unchanged.
  - Within the unchanged queries, the first correct hit moved **up** in 2 (m2h-05, m2h-14: rank 2 → 1) and **down** in 2 (m2h-08, m2h-20: rank 1 → 2).
- **Exact-term subgroup** (m2h-06 UCIC, m2h-11 UNSCR 1718, m2h-12 shell bank): **1.000 / 1.000 / 1.000 for both** C0 and C6. Dense already finds all three at rank 1, so the expected lexical advantage of BM25 could not show.
- **Development vs held-out:**
  - Dense scores *higher* on held-out (Recall@5 0.900 vs 0.825; MRR@5 0.850 vs 0.775), leaving less room to improve.
  - C6 is lower on held-out Recall (0.875 vs 0.900 on dev), and its MRR advantage over dense *reverses sign* between the two sets.
- **Latency:** comes from one pass per configuration, after a warm-up that used no held-out queries, so it is noisier than the A3 measurements (three timed passes). The hybrid costs about 12–19 ms per query because its two retrievers run sequentially.

## What changed, query by query (from the recorded top-5 results)

- **m2h-01 (win, 0.00 → 0.50).** "For which customers and purposes can a bank use video-based customer identification?" Dense ranked FAQ Q21 at #23; BM25 ranked it #1, so the fused result has it at rank 3. Neither configuration found the second target, 169MD para 26.
- **m2h-03 (loss, 1.00 → 0.50).** Trust account documents. Both configurations keep 169MD para 35 at rank 1. C6 loses the HDFC KYC-FAQ trust row (Row 4.0); in its place, at rank 2, is a chunk of the **OCR-uncertain non-individual account opening form** (`hdfc_non_individual_account_opening` p.1). Its noisy OCR text overlaps lexically with the query.
- **m2h-18 (loss, 1.00 → 0.50).** Second account at the same bank. Both keep RBI FAQ Q4 at rank 1. C6 loses the HDFC FAQ "When does KYC apply?" passage to RBI paragraphs 17, 29 and 65.
- **m2h-08 and m2h-20 (same recall, rank 1 → 2).** An HDFC **form** page takes rank 1:
  - for m2h-08 (address change), the NRI re-KYC form's address-update section;
  - for m2h-20 (is Aadhaar compulsory), the Aadhaar consent page of the re-KYC form, which then also fills ranks 3 and 5.
- **m2h-02 (both miss the scoring target).** Both retrieve the *alternative* evidence, RBI FAQ Q20, which says the same thing (C0 at rank 1, C6 at rank 4), but not the canonical scoring target, 169MD para 27(1)(v) (p.31). Both return a neighbouring para 27 chunk (pp.32–33) instead. As pre-registered, the alternative hit is reported and **not** counted, so both configurations score 0 here.
- **m2h-16 and m2h-17:** both configurations find the scoring target at rank 1, and also retrieve the duplicate declaration in the other form (alternative evidence).

**Pattern:**
- **Losses:** in the stopword variant, BM25 promotes HDFC *form* chunks, which share everyday words such as "account", "address", "Aadhaar" and "trust" with customer-style questions, above the HDFC policy FAQ and RBI FAQ answers the labels target. One of them is uncertain OCR text.
  - On the development set, the same mechanism helped by recovering lexically matched passages dense had missed.
  - Here, dense's top results were already correct, so the promoted form chunks displaced correct answers.
- **Gain:** the single gain (m2h-01) is the hybrid mechanism working as intended: BM25 contributes a passage dense ranked poorly.

## Interpretation

- **The A3 development improvement did not generalize on Recall@5.** C6 is 0.025 below dense on held-out, after being 0.075 above it on development.
- **Hit@5 and MRR@5 move slightly in C6's favour,** but by one query's worth or less. The Recall CI includes zero.
- **The fair reading:** on this corpus and these 40 labelled queries, hybrid retrieval with C6 is **not measurably better than dense**. It costs about 12–19 ms more per query and introduces a specific failure mode: lexical promotion of form and OCR chunks.
- **Why this is plausible:** the development gain was 1.5 query-equivalents with a CI of [−0.125, +0.275]. Regression on a fresh sample of this size is consistent with the gain having been partly noise or specific to the development set's queries.
- **Protocol consequence:** per M2_PLAN §6 and `docs/10`, the result is reported as is and does **not** reopen tuning. The A3 decision (C6 as the configured default) stands as recorded, unless the project owner decides otherwise. **That decision is the owner's.** The relevant facts:
  - dense is equal or better on held-out Recall@5 and cheaper;
  - C6 finds a passage dense ranks far down (m2h-01);
  - C6 has a known lexical bias towards form chunks.

## Limitations

- **Small single-annotator benchmark:** 20 held-out queries labelled by the implementing assistant and reviewed by the project owner. One query moves Recall@5 by 0.05.
- **Ceiling effects:** dense scores 0.900 and the exact-term subgroup is at 1.000 for both, so the benchmark can't show a BM25 advantage on rare terms.
- **Alternative evidence scored as a miss by design:** m2h-02's alternative-evidence hits show both configurations reach an equivalent answer that the canonical label doesn't credit. The scoring is unchanged.
- **Latency:** one pass per configuration on a Windows laptop CPU with local Docker; indicative only.
- **Negation:** the stopword variant removes "no" and "not" (registered limitation). No held-out query here depended on negation for its result.
- **One-time:** this result cannot be re-run on the same held-out set without losing its value as an unbiased estimate. Any future comparison needs new labelled data.

---

# Generated held-out report (verbatim output of `make eval-heldout`)

## M2 held-out evaluation (one-time)

- Dataset: `data/eval/m2_heldout.jsonl` (20 queries), SHA-256 `55ba14d9893333b89cc260ac0ece3ac9115ebede28781aa98adfee4d54b0210c` (matches the frozen value)
- Run: 2026-11-10T09:49:14+00:00
- Configurations: C0 (dense), C6 (hybrid stopwords k=20); selected by the committed A3 record (code commit 86d337e)
- One scored pass per configuration; per-query latency measured within that pass (services warmed beforehand without held-out queries).
- Alternative (non-scoring) evidence is reported separately and never counted.

| Metric | C0 | C6 | Δ (abs) | Δ (rel) |
|---|---|---|---|---|
| Recall@5 | 0.900 | 0.875 | -0.025 | -2.8% |
| Hit@5 | 0.900 | 0.950 | +0.050 | +5.6% |
| MRR@5 | 0.850 | 0.867 | +0.017 | +2.0% |
| p50 latency ms | 46.5 | 65.5 | | |
| p95 latency ms | 60.7 | 72.5 | | |

- ΔRecall@5 paired bootstrap 95% CI (10,000 resamples, seed 20261110): [-0.125, +0.050] (reported only)
- Per query on Recall@5, C6 vs C0: 1 better, 2 worse, 17 unchanged

### Per query

| id | C0 recall (rank) | C6 recall (rank) | change | scoring target hit | alternative evidence hit | C6 first match: dense / bm25 rank |
|---|---|---|---|---|---|---|
| m2h-01 | 0.00 (-) | 0.50 (3) | win | no / yes | n/a / n/a | 23 / 1 |
| m2h-02 | 0.00 (-) | 0.00 (-) | unchanged | no / no | yes / yes | - |
| m2h-03 | 1.00 (1) | 0.50 (1) | loss | yes / yes | n/a / n/a | 1 / 2 |
| m2h-04 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-05 | 1.00 (2) | 1.00 (1) | same recall, rank up | yes / yes | n/a / n/a | 2 / 2 |
| m2h-06 (exact-term) | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-07 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-08 | 1.00 (1) | 1.00 (2) | same recall, rank down | yes / yes | n/a / n/a | 1 / 27 |
| m2h-09 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 2 |
| m2h-10 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 15 |
| m2h-11 (exact-term) | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-12 (exact-term) | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-13 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-14 | 1.00 (2) | 1.00 (1) | same recall, rank up | yes / yes | n/a / n/a | 2 / 4 |
| m2h-15 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 1 |
| m2h-16 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | yes / yes | 1 / 2 |
| m2h-17 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | yes / yes | 1 / 1 |
| m2h-18 | 1.00 (1) | 0.50 (1) | loss | yes / yes | n/a / n/a | 1 / 2 |
| m2h-19 | 1.00 (1) | 1.00 (1) | unchanged | yes / yes | n/a / n/a | 1 / 2 |
| m2h-20 | 1.00 (1) | 1.00 (2) | same recall, rank down | yes / yes | n/a / n/a | 1 / 5 |

### Exact-term subgroup (m2h-06, m2h-11, m2h-12)

| Config | Recall@5 | Hit@5 | MRR@5 |
|---|---|---|---|
| C0 | 1.000 | 1.000 | 1.000 |
| C6 | 1.000 | 1.000 | 1.000 |

### Development vs held-out

| Config | dev Recall@5 | dev Hit@5 | dev MRR@5 | held-out Recall@5 | held-out Hit@5 | held-out MRR@5 |
|---|---|---|---|---|---|---|
| C0 | 0.825 | 0.900 | 0.775 | 0.900 | 0.900 | 0.850 |
| C6 | 0.900 | 0.950 | 0.735 | 0.875 | 0.950 | 0.867 |
