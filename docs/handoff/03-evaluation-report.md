# Evaluation Report (M1–M4)

> **What the numbers mean.** Every figure below is labelled as one of:
> - **measured**: produced by the real system on the real stored corpus;
> - **scripted (test-only)**: produced with a scripted fake scorer or LLM to exercise a code path; it says nothing about model quality;
> - **unavailable**: a production capability that is not active, so nothing can be measured.
>
> The datasets are small and single-annotator. Treat every quality number as indicative.

## 1. M1: retrieval baseline (measured, 2026-11-08)
- **Corpus:** 9 PDFs; 456 chunks (332 single, 27 parent, 97 child); 429 embedded Qdrant points; Postgres and Qdrant consistent.
- **Dense BGE-small with eligibility filters,** on 20 hand-labelled development queries (28 target passages): **Recall@5 0.825, Hit@5 0.90, MRR@5 0.775**.

## 2. M2: retrieval selection and held-out (measured)
| Set | Configuration | Recall@5 | Hit@5 | MRR@5 |
|---|---|---|---|---|
| Dev (20) | C0 dense | 0.825 | 0.900 | 0.775 |
| Dev (20) | C6 hybrid (dev-selected) | 0.900 | 0.950 | 0.735 |
| Held-out (20, frozen, run once) | C0 dense | **0.900** | 0.900 | 0.850 |
| Held-out (20, frozen, run once) | C6 hybrid | **0.875** | 0.950 | 0.867 |

- The 95% CI for ΔRecall@5 is [−0.125, +0.050]: no configuration is distinguishable from dense. **Dense stays the default.**
- The reranker diagnostic (dev, reused set) is not a validation.
- **Form extraction:** 6 deterministic schemas, and 5 approved public blank forms extracted with Gemini (latest records complete). Labels are corroborated against stored text, not verified for accuracy.

Source: `docs/milestones/M2_HANDOFF.md`.

## 3. M3: workflow (measured)
- **End of M3:** unit 969, integration 267.
- **CI end to end** (LLP, real corpus, scripted LLM): pauses for `account_type`, resumes, and passes `check_report`.
- **The one owner-authorised live Groq run** (2026-11-12):
  - two calls (`openai/gpt-oss-120b`), outcome `CHECKLIST_REVIEW_REQUIRED`, evidence `partial`;
  - the model proposed 2 MANDATORY items, and the deterministic validator downgraded both to CONDITIONAL (form/supporting evidence only).

Source: `docs/milestones/M3_HANDOFF.md`.

## 4. M4: claim verification, stage by stage
| Stage | Result |
|---|---|
| S0 | Plan locked; 4 DeBERTa-v3 NLI candidates pinned (licence, revision, label map) |
| S1 | Claim and report-v2 models; 8 committed v1 report fixtures keep validating unchanged |
| S2 | Deterministic foundation (segmentation, premises from stored text, statement analysis, decision table), tested with a scripted scorer, including a 300-case seeded never-upgrade sweep |
| S3 | **Dev set: 55 cases** after owner review (15 entailed / 15 contradicted / 25 insufficient; 43 accepted, 12 changed). **Held-out set: 51 cases, frozen**, SHA-256 `eee0a575…d90203`. Labelling guidelines committed before any model run. |
| S4.1 | `CrossEncoderNLI` (pinned, label map from config, sanity triplet, never truncates) and the registered selection rule `m4-s4-v1`, committed before any run |
| S4.2 | One registered dev run: **0 of 140 configurations qualified** (§5) |
| S5 | `claim_verification` node with NLI disabled: every claim `not_verified`, DEGRADED, never MANDATORY |
| S5 follow-up | `check_report` validates the whole report before any rule |
| S6 | Bounded source-conflict scan plus supersession rule; `not_run` in production |
| S7 | Open review cases (insert-only) and their read endpoint |
| S8 | M4 acceptance rules for every verification state; real-corpus end to end; latency; the held-out decision |

## 5. S4 NLI selection: the official result (measured, dev set only)
- **Protocol:** `m4-s4-v1`, registered in S4.1 and run once at commit `0c4cc49`.
- **Record:** `data/eval/m4_nli_selection.json`, write-once, SHA-256 `f80f8b3f…bdcdebb4`.
- **Result: 0 of 140 configurations qualified** (4 candidates × 35 threshold pairs). No NLI configuration is selected, and **NLI is disabled by default**.

| Candidate | Failed gates (all 35 pairs) | MANDATORY false positives | False-support range | Best entailed recall | Determinism (max abs logit diff, gate 1e-6) |
|---|---|---|---|---|---|
| N1 `cross-encoder/nli-deberta-v3-xsmall` | MANDATORY FP, non-deterministic | 2 | 1/20 – 1/10 | 0.40 | 4.9e-6 |
| N2 `cross-encoder/nli-deberta-v3-small` | MANDATORY FP, non-deterministic | 3 | 3/40 | 0.40 | 2.9e-6 |
| N3 `cross-encoder/nli-deberta-v3-base` | MANDATORY FP, non-deterministic | 3 | 3/40 | 0.40 | 6.0e-6 |
| N4 `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | MANDATORY FP, non-deterministic | 3 | 1/20 | 0.40 | 1.07e-2 |

**CPU cost (measured, one 16-logical-core laptop; indicative, not guaranteed):**

| Candidate | p50 per pair | p95 per pair | Typical per case (mean pairs × p50) | Conservative per request (400 × p95) |
|---|---|---|---|---|
| N1 | 41 ms | 63 ms | 0.8 s | 25 s |
| N2 | 62 ms | 86 ms | 1.2 s | 35 s |
| N3 | 117 ms | 177 ms | 2.2 s | 71 s |
| N4 (float16 on CPU) | 1,877 ms | 2,779 ms | 35.7 s | ~18.5 min |

Total S4.2 compute was 62.4 min for the registered run plus 65.6 min for a first attempt that crashed before writing anything. The diagnosis of the MANDATORY false positives (a read-only rule trace) is in docs/10 §"M4 S4.2" and in the known-issues register.

### Ablation (copied from the committed S4 record; dev, 55 cases; diagnostic only)
| Configuration | Accuracy | Macro-F1 | False support | Entailed recall | MANDATORY FP |
|---|---|---|---|---|---|
| Rules only (no scorer) | 25/55 | 0.208 | 0 | 0.00 | 0 |
| NLI only (argmax), N1 / N2 / N3 / N4 | 30 / 31 / 30 / 33 of 55 | 0.538 / 0.560 / 0.539 / 0.605 | 4/40 each | 0.67 / 0.67 / 0.53 / 0.73 | 0 (cannot claim MANDATORY) |
| Combined at τ_e = τ_c = 0.9, N1 / N2 / N3 / N4 | 36 / 34 / 32 / 32 of 55 | 0.619 / 0.583 / 0.554 / 0.543 | 2/40, 3/40, 3/40, 2/40 | 0.40 / 0.40 / 0.40 / 0.33 | 2 / 3 / 3 / 3 |

## 6. Held-out claim set: `held_out_status: frozen_unused`
- **Reason:** the held-out run would estimate the verifier selected in S4, and none was selected. With NLI disabled, the shipped verifier's held-out outcome is fixed by construction (every claim `not_verified`). Running it would spend the only unseen set without measuring a model.
- **Protection:**
  - not read for scoring, loaded for evaluation, relabelled or corrected; SHA-256 `eee0a575319d7df4bc3d8f5c0771a89e4c8bf9f68abe36eb56fcac3275d90203` unchanged (verified from the bytes at M4 close);
  - a guard test allows the file name in `app/` only in the dev loader's refusal and the selection CLI's hash check;
  - the S3 integrity tests check its structure and hash and never score it (kept by owner decision).
- **The 6 annotation issues** (m4h-007, -008, -009, -010, -011, -022) are preserved for a future, separately approved protocol.

## 7. Runtime and end-to-end results
- **Latency with NLI off, real corpus, 7 runs (measured, indicative; no gate):**
  - `claim_verification` node: p50 **47.2 ms**, p95 **49.4 ms**;
  - whole graph run (scripted LLM, BM25): p50 **503.9 ms**, p95 **536.2 ms**.
- **Real-corpus end to end with NLI off (measured):**
  - DEGRADED, report v2, every claim `not_verified`, conflict scan `not_run`;
  - one open `not_verified` review case per item;
  - `check-workflow` clean.
  - `check_report` reports only the known pre-existing M3 `citation_mismatch` (child chunk cited with its parent's pages).
- **Enabled path (scripted, test-only):** premises from real stored chunks, a `bounded` scan and a consistent report and review cases. **It is not a quality result.**
- **Unavailable in production:** semantic claim verification (NLI), the automated conflict scan, conflict-detection metrics, and held-out verification metrics.

## 8. Final test and check results (M4 close)
| Check | Result |
|---|---|
| Unit (`make test`) | 1,492 passed, 1 failed (known K7), 510 deselected |
| Integration (full) | 504 passed, 2 failed (known K8: `test_dense_golden`, `test_hybrid_dev`), 4 deselected |
| `make lint` (ruff, black, strict mypy) | clean (ruff; black: 241 files unchanged; mypy: no issues in 124 source files) |
| `make check-schema` | clean ("No new upgrade operations detected") |
| `make check-workflow` | `"ok": true`, no review-case problems |
| `git diff --check` | clean |
| Secret/configuration scan (tracked files) | no tracked `.env`; no credential-like strings except one deliberately fake test literal; example env holds no secrets and keeps every live gate off |
| Held-out SHA-256 / S4 record SHA-256 | held-out `eee0a575…d90203` unchanged; S4 record `f80f8b3f…bdcdebb4` unchanged |

**Known baseline failures** (documented, not caused by M4 code changes, not modified):
1. `tests/unit/test_groq_provider.py::test_live_calls_and_cache_are_off_by_default`: the local environment carries the owner's Groq terms-review date.
2. `tests/integration/test_dense_golden.py::test_dense_retrieval_reproduces_the_m1_golden_snapshot`: Qdrant tied scores on query m1-14.
3. `tests/integration/test_hybrid_dev.py::test_hybrid_on_the_dev_stack_preserves_dense_provenance`: the same tie.

**The suite is not entirely green:** these three failures remain.

## 9. M5: frontend (S7 final run, 2026-11-14)
Exact results, measured on the M5 branch (`feat/m5-s7-closure`). No backend code changed after S1. The frontend has no evaluation metric of its own: these are correctness checks, not quality results.

| Check | Result |
|---|---|
| Frontend `make web-test` (Vitest, jsdom, no network) | **120 passed / 120**, 3 consecutive runs (13 test files) |
| Frontend `make web-lint` (tsc strict, ESLint, Prettier, `openapi-typescript --check`) | clean |
| Frontend `make web-build` (Vite production build) | pass (JS 348.9 kB, 107.7 kB gzipped) |
| Fresh clone of `feat/m5-s7-closure` (at `cefb4b7`) | `npm ci`, type-check, ESLint and Prettier, `check:api`, **120/120** tests (13 files) and the build all pass; `npm audit --omit=dev` 0. The backend contract tests (13) and ruff also pass from the clone, using the main checkout's virtualenv. |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `npm audit` (full tree) | 10 moderate, all GHSA-g3hv-75wg-m249 (minimatch via ESLint/typescript-eslint; dev tooling only; accepted, K16) |
| Backend unit (`make test`) | 1,505 passed, 1 failed (**K7**, environment-dependent), 512 deselected |
| Backend integration (`make test-integration`) | 507 passed, 1 failed (**K8**: `test_dense_golden`, the known tied pair at m1-03 ranks 5/6; `test_hybrid_dev` passed this run), 1,510 deselected |
| `make lint` (ruff, black, strict mypy on 126 files) | clean |
| `make check-schema` | "No new upgrade operations detected" |
| `make check-workflow` | `"ok": true`, no review-case problems |
| Browser smoke test (headless Chrome 157, real backend, no live LLM) | **14/14** steps (details: `docs/milestones/M5_PLAN.md`, S7 outcome) |
| Accessibility audit (same browser; 6 screens) | 0 unnamed controls, 0 unlabelled inputs, 1 `h1` and no skipped levels per page, landmarks present, every Tab stop with a visible focus ring, WCAG AA contrast (minimum 4.55), full flow by keyboard |

**Known failures (none new):**
- **K7:** the Groq environment test, failing because the local environment carries the owner's terms-review date;
- **K8:** a Qdrant tied-score order. The flaky query and test vary between runs (m1-14 at M4 close; m1-03 in S1 and S7).

Neither is caused by M5 code.

**Growth of the frontend suite by stage:** S2 15 → S3 52 → S4 67 → S5 100 → S6 117 → S7 120.