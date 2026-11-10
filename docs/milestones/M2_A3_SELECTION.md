# M2 A3: Retrieval Configuration Decision

**Decision: C6 = hybrid retrieval, BM25 with the registered stopword variant, RRF k = 20** (50 candidates per retriever, top-k 5). It was selected on the **development set only**, by the rule pre-registered in `docs/milestones/M2_PLAN.md` §6, from one run of `make eval-select` on 2026-11-10 at code commit `86d337e` (A3a).

This decision was committed **before** any held-out evaluation. The frozen held-out set (`data/eval/m2_heldout.jsonl`) was not read, loaded, queried, hashed or evaluated during A3, and its git history is unchanged (only `a99cc9a`). The exact-term held-out subgroup (m2h-06, m2h-11, m2h-12) is reported only in the separate held-out run.

- **Machine-readable record:** `data/eval/m2_a3_selection.json`. It holds the development set's SHA-256 (`fc5add26…ccedbb`), the code commit, the fixed parameters, every candidate's parameters and exact-fraction metrics, the qualifiers and the selection.
  - The per-candidate `parameters` objects were added after the run from the static registry `app.retrieval.selection.CANDIDATES` (noted in the record). No result field was changed.
- **Settings now default to C6:** `RETRIEVAL_MODE=hybrid`, `BM25_STOPWORDS=true`, `RRF_K=20` (`app/config.py`, `infra/.env.example`). A unit test keeps them equal to the record.
  - `--mode dense` remains the unchanged M1 path, verified by `make check-dense-golden`.
  - The code-level defaults stay as they were (plain tokenizer, RRF k = 60 constant); only the settings carry the selection.

## Results (development set, 20 queries; exact metrics in the record)

| ID | Configuration | Recall@5 | Hit@5 | MRR@5 | ΔRecall@5 vs C0 | 95% bootstrap CI | Wins / losses vs C0 | p50 / p95 ms |
|---|---|---|---|---|---|---|---|---|
| C0 | dense | 0.825 (33/40) | 0.900 | 0.775 | — | — | — | 34.3 / 44.0 |
| C1 | BM25 plain | 0.692 | 0.800 | 0.633 | −0.133 | [−0.333, +0.067] | 1 / 4 | 9.3 / 12.4 |
| C2 | BM25 stopwords | 0.717 | 0.800 | 0.696 | −0.108 | [−0.317, +0.100] | 2 / 4 | 8.8 / 12.0 |
| C3 | hybrid plain k=60 | 0.858 | 0.900 | 0.704 | +0.033 | [−0.183, +0.233] | 4 / 3 | 52.4 / 74.2 |
| C4 | hybrid plain k=20 | 0.875 (7/8) | 0.900 | 0.700 | **+0.050 (exactly 1/20)** | [−0.150, +0.250] | 4 / 2 | 49.1 / 56.4 |
| C5 | hybrid stopwords k=60 | 0.833 | 0.900 | 0.723 | +0.008 | [−0.217, +0.225] | 4 / 4 | 49.9 / 70.9 |
| **C6** | **hybrid stopwords k=20** | **0.900 (9/10)** | **0.950** | **0.735** | **+0.075 (3/40)** | [−0.125, +0.275] | 4 / 2 | 49.2 / 57.8 |

C1 (0.692 / 0.80 / 0.633) and C3 (0.858 / 0.90 / 0.704) reproduce the A1 and A2 measurements exactly.

## Why each configuration qualified or not

The rule: ΔRecall@5 ≥ 1/20 against dense **and** Hit@5 ≥ dense's 0.900.

- **C1 BM25 plain fails both conditions.** Recall@5 is 0.133 *below* dense, and Hit@5 drops to 0.80 (it misses m1-01, m1-04, m1-11 and m1-17).
- **C2 BM25 stopwords fails both.** Recall@5 is −0.108 and Hit@5 is 0.80.
- **C3 hybrid plain k=60 fails on Recall.** Its gain of +0.033 (1/30) is below 0.05; Hit@5 is equal.
- **C4 hybrid plain k=20 qualifies.** Its gain is exactly 1/20 (35/40 − 33/40) and Hit@5 is equal. It sits precisely on the boundary; the exact-fraction arithmetic made sure no float rounding decided it.
- **C5 hybrid stopwords k=60 fails on Recall.** Its gain of +0.008 (1/120) is below 0.05.
- **C6 hybrid stopwords k=20 qualifies.** Its gain is +0.075 (3/40), and Hit@5 *rises* to 0.950, which is allowed: the rule only forbids a decrease.
- **Ordering of the qualifiers:** C6 (Recall@5 9/10) ranks above C4 (7/8) on the **first** key, Recall@5. The later keys (MRR@5, simplicity, p50 latency, ID) were not needed. C6 also has the higher MRR@5 (0.735 vs 0.700), but that played no part in the decision.

## The Recall@5 / MRR@5 trade-off

All four hybrids have a **higher Recall@5 but a lower MRR@5** than dense (0.700–0.735 vs 0.775). The per-query evidence in the report below shows why.

- **Agreement between the retrievers lifts passages that dense alone ranked too low.**
  - The target of m1-01 is dense #10 and BM25 #7–9. Neither puts it in its own top 5, but its fused score lifts it to rank 3–4.
  - m1-20: dense #7, BM25 #1–2 → fused rank 3–4.
  - m1-06: dense #3, BM25 #1 → fused #1.
  - m1-19: the second target enters the top 5.

  These are the Recall gains, but they mostly arrive at ranks 3–4, so they add little to MRR.
- **A correct dense #1 that BM25 ranks poorly is pushed down.**
  - m1-17: dense #1, BM25 #13 (plain) or #24 (stopwords) → fused rank 3 or 5, and out of the top 5 entirely in C5.
  - m1-15: dense #1, BM25 #3 → rank 2 in the plain hybrids.
  - m1-04: dense #1, BM25 #31 → rank 4–5 with stopwords and out of the top 5 with the plain tokenizer.
  - **m1-11:** dense's rank-2 target is out of the top 5 in every hybrid, the one query hybrid loses completely in all four configurations.

  Each such demotion turns a reciprocal rank of 1 into 1/3 to 1/5, so MRR falls even when Recall holds.
- **Why k=20 beats k=60:** RRF gives 1/(k + rank). A smaller k widens the gap between a retriever's top ranks and its lower ranks, so one retriever's confident #1 counts for more. With k=20, m1-12 keeps all three targets (0.67 at k=60) and m1-17 stays in the top 5 with stopwords. That's why C4 and C6 beat C3 and C5 on Recall.
- **Stopwords:** removing the listed function words (for example "is", "the", "a", "of"; "what" and "how" are *not* in the list) stops them contributing to BM25 scores. A plausible reading, not separately measured, is that this reduces matches driven by question wording. Here it partly recovers m1-04 (0.50, against 0.00 for the plain hybrids) and lifts BM25-only m1-05 to 1.00.
  - **Negation:** "no" in m1-04 ("A customer has **no** PAN card") is one of the removed words. In this query, removing it did not hurt.
  - **Limitation:** this one observation says nothing about negation in general. A query whose meaning depends on "no" or "not" is matched by BM25 as if the negation were absent. Dense retrieval and the later claim verification (M4) are unaffected, but this remains a known limitation of the selected configuration.

## Limitations of this decision (to be read with the numbers)

- **Small development set:** 20 queries, one annotator (the implementing assistant), labels reviewed by the project owner but not independently annotated. One query moves Recall@5 by about 0.05.
- **Wide confidence intervals:** every paired bootstrap 95% CI for ΔRecall@5 includes zero (C6: [−0.125, +0.275]). The rule selects C6, but on this data it is **not statistically distinguishable** from dense.
- **Exactly at the threshold:** C4 meets it with zero margin, and C6 clears it by 1.5 query-equivalents.
- **MRR cost:** C6 ranks its first correct passage lower than dense on average (MRR@5 0.735 vs 0.775), and loses m1-11.
- **Latency cost:** p50 +15 ms (49.2 vs 34.3 ms) and p95 +14 ms, because the two retrievers run one after the other.
- **Single run on one machine:** latency is from one run on this Windows laptop (CPU, local Docker) and is indicative only. It entered no decision, since Recall@5 alone separated the qualifiers.
- **The test still ahead:** the held-out set is the only check of whether this choice generalises. It will be run **once**, for C6 with C0 dense as the fixed reference, and reported as is, including the exact-term subgroup. A disappointing result will not reopen tuning.

---

# Generated selection report (verbatim output of `make eval-select`)

## M2 A3: retrieval configuration selection (development set only)

- Dataset: `data/eval/m1_retrieval.jsonl` (20 queries), SHA-256 `fc5add26521e234ab8f356e024a45fb3bd76e10d9819a649b5367e2ef2ccedbb`
- Run: 2026-11-10T09:23:56+00:00
- Fixed: candidates_per_retriever = 50; top_k = 5; bm25 = rank_bm25 BM25Okapi k1=1.5 b=0.75; embedding = pinned EMBEDDING_MODEL @ EMBEDDING_MODEL_REVISION; filters = production eligibility filters + each case's entity terms
- Rule (pre-registered, M2_PLAN section 6): A non-dense configuration qualifies only if mean Recall@5 improves on dense by at least 0.05 and Hit@5 does not decrease. Qualifiers are ordered by highest Recall@5, then highest MRR@5, then simplicity, then lower p50 latency, then configuration id. If none qualifies, dense (C0) remains selected.
- The frozen held-out set was not read, loaded, queried, hashed or evaluated.

| ID | Configuration | Recall@5 | Hit@5 | MRR@5 | ΔRecall@5 vs C0 | 95% CI (bootstrap) | wins / losses vs C0 | p50 ms | p95 ms | Qualifies? |
|---|---|---|---|---|---|---|---|---|---|---|
| C0 | dense | 0.825 | 0.900 | 0.775 | +0.000 | - | - | 34.3 | 44.0 | baseline |
| C1 | bm25 plain | 0.692 | 0.800 | 0.633 | -0.133 | [-0.333, +0.067] | 1 / 4 | 9.3 | 12.4 | Recall@5 gain -0.1333 < 0.05; Hit@5 0.800 below dense 0.900 |
| C2 | bm25 stopwords | 0.717 | 0.800 | 0.696 | -0.108 | [-0.317, +0.100] | 2 / 4 | 8.8 | 12.0 | Recall@5 gain -0.1083 < 0.05; Hit@5 0.800 below dense 0.900 |
| C3 | hybrid plain k=60 | 0.858 | 0.900 | 0.704 | +0.033 | [-0.183, +0.233] | 4 / 3 | 52.4 | 74.2 | Recall@5 gain +0.0333 < 0.05 |
| C4 | hybrid plain k=20 | 0.875 | 0.900 | 0.700 | +0.050 | [-0.150, +0.250] | 4 / 2 | 49.1 | 56.4 | qualifies |
| C5 | hybrid stopwords k=60 | 0.833 | 0.900 | 0.723 | +0.008 | [-0.217, +0.225] | 4 / 4 | 49.9 | 70.9 | Recall@5 gain +0.0083 < 0.05 |
| C6 | hybrid stopwords k=20 | 0.900 | 0.950 | 0.735 | +0.075 | [-0.125, +0.275] | 4 / 2 | 49.2 | 57.8 | qualifies |

### Per query: Recall@5 (first matching rank)

| id | C0 | C1 | C2 | C3 | C4 | C5 | C6 |
|---|---|---|---|---|---|---|---|
| m1-01 | 0.00 (-) | 0.00 (-) | 0.00 (-) | 1.00 (3) | 1.00 (4) | 1.00 (4) | 1.00 (4) |
| m1-02 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-03 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-04 | 1.00 (1) | 0.00 (-) | 0.00 (-) | 0.00 (-) | 0.00 (-) | 0.50 (5) | 0.50 (4) |
| m1-05 | 0.50 (1) | 0.50 (1) | 1.00 (1) | 0.50 (1) | 0.50 (1) | 0.50 (1) | 0.50 (1) |
| m1-06 | 0.50 (3) | 0.50 (1) | 0.50 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-07 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-08 | 1.00 (3) | 1.00 (3) | 1.00 (3) | 1.00 (3) | 1.00 (3) | 1.00 (3) | 1.00 (3) |
| m1-09 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-10 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-11 | 1.00 (2) | 0.00 (-) | 0.00 (-) | 0.00 (-) | 0.00 (-) | 0.00 (-) | 0.00 (-) |
| m1-12 | 1.00 (1) | 0.33 (1) | 0.33 (1) | 0.67 (1) | 1.00 (1) | 0.67 (1) | 1.00 (1) |
| m1-13 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-14 | 1.00 (1) | 1.00 (4) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-15 | 1.00 (1) | 1.00 (3) | 1.00 (3) | 1.00 (2) | 1.00 (2) | 1.00 (1) | 1.00 (1) |
| m1-16 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-17 | 1.00 (1) | 0.00 (-) | 0.00 (-) | 1.00 (3) | 1.00 (3) | 0.00 (-) | 1.00 (5) |
| m1-18 | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| m1-19 | 0.50 (3) | 0.50 (4) | 0.50 (4) | 1.00 (3) | 1.00 (3) | 1.00 (3) | 1.00 (3) |
| m1-20 | 0.00 (-) | 1.00 (2) | 1.00 (1) | 1.00 (4) | 1.00 (4) | 1.00 (3) | 1.00 (3) |

### Recall@5 / MRR@5 trade-off evidence

**C3 (hybrid plain k=60) vs C0**

| id | C0 rank | rank | C0 recall | recall | its first match: dense / bm25 rank | C0's first match: rank here / in BM25 top 5 |
|---|---|---|---|---|---|---|
| m1-01 | - | 3 | 0.00 | 1.00 | 10 / 7 | - |
| m1-04 | 1 | - | 1.00 | 0.00 | - | - / - |
| m1-06 | 3 | 1 | 0.50 | 1.00 | 3 / 1 | 1 / 1 |
| m1-11 | 2 | - | 1.00 | 0.00 | - | - / - |
| m1-12 | 1 | 1 | 1.00 | 0.67 | 1 / 1 | 1 / 1 |
| m1-15 | 1 | 2 | 1.00 | 1.00 | 1 / 3 | 2 / 3 |
| m1-17 | 1 | 3 | 1.00 | 1.00 | 1 / 13 | 3 / - |
| m1-19 | 3 | 3 | 0.50 | 1.00 | 6 / 4 | 4 / - |
| m1-20 | - | 4 | 0.00 | 1.00 | 7 / 2 | - |

**C4 (hybrid plain k=20) vs C0**

| id | C0 rank | rank | C0 recall | recall | its first match: dense / bm25 rank | C0's first match: rank here / in BM25 top 5 |
|---|---|---|---|---|---|---|
| m1-01 | - | 4 | 0.00 | 1.00 | 10 / 7 | - |
| m1-04 | 1 | - | 1.00 | 0.00 | - | - / - |
| m1-06 | 3 | 1 | 0.50 | 1.00 | 3 / 1 | 1 / 1 |
| m1-11 | 2 | - | 1.00 | 0.00 | - | - / - |
| m1-15 | 1 | 2 | 1.00 | 1.00 | 1 / 3 | 2 / 3 |
| m1-17 | 1 | 3 | 1.00 | 1.00 | 1 / 13 | 3 / - |
| m1-19 | 3 | 3 | 0.50 | 1.00 | 6 / 4 | 4 / - |
| m1-20 | - | 4 | 0.00 | 1.00 | 7 / 2 | - |

**C5 (hybrid stopwords k=60) vs C0**

| id | C0 rank | rank | C0 recall | recall | its first match: dense / bm25 rank | C0's first match: rank here / in BM25 top 5 |
|---|---|---|---|---|---|---|
| m1-01 | - | 4 | 0.00 | 1.00 | 10 / 9 | - |
| m1-04 | 1 | 5 | 1.00 | 0.50 | 1 / 31 | 5 / - |
| m1-06 | 3 | 1 | 0.50 | 1.00 | 3 / 1 | 1 / 1 |
| m1-11 | 2 | - | 1.00 | 0.00 | - | - / - |
| m1-12 | 1 | 1 | 1.00 | 0.67 | 1 / 1 | 1 / 1 |
| m1-17 | 1 | - | 1.00 | 0.00 | - | - / - |
| m1-19 | 3 | 3 | 0.50 | 1.00 | 3 / 6 | 3 / - |
| m1-20 | - | 3 | 0.00 | 1.00 | 7 / 1 | - |

**C6 (hybrid stopwords k=20) vs C0**

| id | C0 rank | rank | C0 recall | recall | its first match: dense / bm25 rank | C0's first match: rank here / in BM25 top 5 |
|---|---|---|---|---|---|---|
| m1-01 | - | 4 | 0.00 | 1.00 | 10 / 9 | - |
| m1-04 | 1 | 4 | 1.00 | 0.50 | 1 / 31 | 4 / - |
| m1-06 | 3 | 1 | 0.50 | 1.00 | 3 / 1 | 1 / 1 |
| m1-11 | 2 | - | 1.00 | 0.00 | - | - / - |
| m1-17 | 1 | 5 | 1.00 | 1.00 | 1 / 24 | 5 / - |
| m1-19 | 3 | 3 | 0.50 | 1.00 | 3 / 6 | 3 / - |
| m1-20 | - | 3 | 0.00 | 1.00 | 7 / 1 | - |

### Decision

- Qualifying configurations (in rule order): C6, C4
- **Selected: C6 (hybrid stopwords k=20)**
