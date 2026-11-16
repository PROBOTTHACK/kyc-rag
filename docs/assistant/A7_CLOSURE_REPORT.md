# A7: Final Evaluation and Closure Report

**Product:** Account Opening Assistant, an independent prototype built on public HDFC Bank forms. It is not affiliated with or endorsed by HDFC Bank or the RBI, it is not a KYC or eligibility decision, and it uses synthetic data only.
**Branch:** `feat/account-opening-assistant`. Nothing was pushed, merged, rebased or tagged, and `main` is untouched.
**Plan:** [`A7_PLAN.md`](A7_PLAN.md), approved by the owner on 2026-11-15. A7 is the final milestone; there is no A8.

## 1. Verdict
A7 is **complete as an evaluation**, but the assistant is **not release-ready**, and this report does not claim that it is. Two items stay open by owner instruction (§6):
1. **The 30-item citation review is not signed off.** All 30 rows still read *Pending human review*. Citation accuracy is verified **automatically** (1,332 of 1,332 citations re-resolve), but it is **not human-verified**.
2. **The deterministic latency target is missed:** p95 899 ms in the final run against the 750 ms target (867 ms at A7.4, 948 ms in the fresh clone).

The live-LLM voice also missed its soft validity target once the voice check was fixed (55 % against 80 %, §3.2). Every rejected reply falls back to the deterministic reply.

## 2. Stages and commits
| Stage | Commits | Result |
|---|---|---|
| A7.1 harness and datasets | `e306f3d` | Done |
| A7.2 coverage, conversion, evidence (E1, E2, E7) | `4809168`; fix `36ed80c` | PASS |
| A7.3 conversations, memory, scenarios (E3–E6) | `38965f3`; fixes `291b2c7`, `cfa4291` | PASS |
| A7.4 privacy, isolation, idempotency, faults, latency (E8, E9) | `ea602f7` | PASS, except the latency target (§6) |
| LangSmith observability and evaluation (owner requirement) | `eee5368`, `f9da526` | PASS. Traces were sent. The LangSmith experiment was not run: the key is tracing-only (§3.3). |
| A7.5 known failures K7, K7b, K8 | `64e3e01`, `15e39e8` | All three fixed (owner decisions). A7.7 later found a pre-existing legacy timing flake (K22, open) |
| A7.6 browser, demo, live LLM (E10–E12) | `9d6976e`, `a59583c`; fixes `c0ea5e5`, `29ea27e`, `d7fc99b` | Browser PASS; demo delivered; live run done (§3.2) |
| A7.7 fresh-clone reproducibility (E13) | this closure commit | Evaluation results identical; 3 documented test differences (K22, K23, 2 data-dependent skips) |
| A7.8 closure (E14) | this closure commit | This report |

**Defects found by the evaluation and fixed** (each in its own commit, with a regression test):

| Commit | Defect | Found by |
|---|---|---|
| `36ed80c` | The FCNR item carried a condition printed for the RFC deposit | A7.2 evidence audit |
| `291b2c7` | A separate request accepted a pending account offer | A7.3 conversations |
| `cfa4291` | An unreachable conversation store returned 500 instead of 503 | A7.4 faults |
| `15e39e8` | Dense search ordered exact-score ties by Qdrant's internal layout (K8) | A7.5 |
| `c0ea5e5` | A `/favicon.ico` 404 was logged on every page load | A7.6 browser matrix |
| `29ea27e` | A scenario-offer choice showed an empty or "0" customer bubble | A7.6 recorded demo |
| `d7fc99b` | The voice check accepted "you'll need" obligation wording and an unsupported profile claim | A7.6 live run, stricter offline audit |

## 3. Results against the plan's thresholds (A7_PLAN §3)

### 3.1 Deterministic (hard gates)
| Metric | Result | Threshold | Report |
|---|---|---|---|
| Coverage: complete covered profiles | 55/55 (26 cells; 68 runs including "not sure" facets) | 100 % | [coverage](a7/coverage.md) |
| Not-established cells: verbatim fallback | 28/28 | 100 % | coverage |
| Conversion routing: positives / near-misses | 12/12 and 6/6 | 100 % | coverage |
| Citations re-resolved in PostgreSQL (document, page, passage, reliability, HDFC only) | 1,332/1,332 | 100 % | coverage |
| `policy_supported` / inferred document / organisation photo items | 0 / 0 / 0 | 0 | coverage |
| Golden checklist item ids | identical | no drift | coverage |
| Conversation turn accuracy (70 conversations, 241 turns) | 91/91 checked turns | 100 % | [conversations](a7/conversations.md) |
| Greeting safety | 0 of 23 social turns with a fallback or a repeated checklist | 0 | conversations |
| Context recall / exclusions | 20/20 / 0 of 7 | 100 % / 0 | [memory](a7/memory.md) |
| Context within the token budget, messages complete | 31/31 | 100 % | memory |
| Summary fidelity / context identical after a restart | 2/2 / 15/15 | 100 % | memory |
| Deleted conversation unrecoverable (rows, summary, Redis, API 404) | yes | yes | memory |
| Scenario isolation: 200 seeded sequences (1,939 turns) | 0 leaks | 0 | [scenarios](a7/scenarios.md) |
| Identifiers refused / e-mail and phone redacted / negative controls allowed | 29/29 / 7/7 / 15/15 | 100 % / 100 % / 0 false positives | [privacy](a7/privacy.md) |
| Identifiers in answers, database, 140 log records (final run), LLM requests | 0 | 0 | privacy |
| Cross-session reads / unknown ids answered 404 / collisions in 10,000 ids | none / 20/20 / 0 | 0 | privacy |
| Idempotent replay, 409 on key reuse, 10 concurrent duplicates → 1 turn | pass | 100 % | [reliability](a7/reliability.md) |
| Store failure, LLM error / 429 / timeout, expiry 410, database down 503 | pass | 100 % | reliability |
| **Deterministic latency, 500 turns** | **p50 66 ms, p95 899 ms, p99 977 ms** (final run; A7.4: p95 867 ms) | p95 ≤ 750 ms | reliability: **MISSED** |
| Browser: 4 widths × (22-step main flow, keyboard only, reduced motion), layout at every step | all pass, 0 console errors | 0 failures | [browser](a7/browser.md) |
| Tracing: one trace per turn, spans closed, identifiers or passages uploaded, answers unchanged when LangSmith fails | 286/286, yes, 0, yes | as specified | [observability](a7/observability.md) |

### 3.2 Live LLM (E11; opt-in by the owner; synthetic only)
Groq `openai/gpt-oss-120b` (fallback `gpt-oss-20b`), over the 70 conversations and the 51-message PII set.

**Budget:** 572,718 tokens were printed before the run and enforced. The run reserved 522,991; Groq reported 187,424 input and 47,935 output tokens. It completed in 182 min with no abort.

| Metric | Result | Threshold |
|---|---|---|
| Voice validity, with the check as it was at run time | 116/119 (97 %) | ≥ 80 % (soft) |
| Factual-safety violations in accepted replies (stricter offline audit, read by hand) | **about 48 at run time**: "you'll need" obligation wording, plus one unsupported profile claim. **Hard gate failed**; the defect was fixed in `d7fc99b` | 0 (hard) |
| The same replies re-checked offline with the fixed check | 66/119 accepted (55 %), **0 confirmed violations** | soft target **missed** |
| Turn time with the live voice | p50 2.2 s, p95 10.3 s (40 voice calls hit the 10 s timeout) | p95 ≤ 12 s |
| Identifiers in LLM requests / blocked messages reaching the LLM | 0 / 0 | 0 |
| LLM proposals committed without a confirmation | 0 (0 accepted proposals) | 0 |

The fixed prompt (voice v2) was **not** re-measured live: a second run would exceed the budget. Report: [live_llm_report](a7/live_llm_report.md).

### 3.3 LangSmith (owner requirement)
- **Traces:** the tracing code was validated on all 286 turns with a capturing client. A live upload of 3 synthetic turns to `account-opening-assistant-eval` reported no ingest error.
- **Read-back and experiment:** both were refused (403). The configured key is a **tracing-only** key.
- **Next step:** the LangSmith experiment (`make a7-eval SUITE=langsmith_eval`: the 70 conversations as a dataset, with turn-accuracy evaluators) needs a personal access token or a service key. It was **not run**, and no result is claimed for it. Report: [langsmith_eval](a7/langsmith_eval.md).

### 3.4 Retrieval (comparison with A2, not a gate)
| Mode | Recall@5 | Hit@5 | MRR@5 |
|---|---|---|---|
| dense (default) | 0.733 | 0.880 | 0.599 |
| BM25 | 0.613 | 0.800 | 0.653 |
| hybrid (RRF) | 0.793 | 0.920 | 0.727 |

These are identical to A2: 25 cases, a single annotator, development set only. Report: [retrieval](a7/retrieval.md).

### 3.5 Citation review (E7, human)
The 30 rows are seeded: 15 from NRI checklists and 15 from organisation checklists. The automated pre-check searched the PDF text layer:
- **19** rows: the wording was found on the cited page;
- **1** row: not found (row 15, "NRE Savings / NRE Current"; needs a visual check);
- **10** rows: scanned pages with no text layer (need a visual check).

**Owner sign-off: not given (0/30). The citations are not claimed as human-verified.** Sheet: [citation_review](a7/citation_review.md).

## 4. Known failures (A7.5)
| Id | Cause (confirmed) | Outcome |
|---|---|---|
| K7, K7b | The shell or the local `.env` switched live Groq on, and the process environment outranks env files | The tests now isolate their environment; assertions unchanged (`64e3e01`) |
| K8 | Exact-score ties in dense search were ordered by Qdrant's index layout, and the golden file recorded that order | Deterministic tie-break, plus a one-pair fixture correction (owner-approved, `15e39e8`) |

Details: [known_failures](a7/known_failures.md).

## 5. Reproducibility (E13, A7.7)
**Setup:**
- a fresh clone of `f9da526` in a temporary directory, on an isolated Docker project (`kyc_a7_fresh`: its own volumes and ports; development data never touched);
- a clean environment, with live LLMs off and no keys;
- a venv from `requirements.lock`, `npm ci`, the owner's 9 PDFs (**9/9 SHA-256 match**), ingest, deterministic form schemas, then every check.

| Result | Detail |
|---|---|
| **Corpus identical** | 456 chunks: the same ids and text (MD5 `7b427626…eb3b`, OCR included); 429 vectors, consistent |
| **Evaluation results identical** | Coverage report (golden item ids, 1,332 citations), conversations, memory, scenarios, citation sample and retrieval (0.733 / 0.613 / 0.793) all identical. Privacy and observability: identical checks, with run-dependent counts only |
| Latency | p95 948 ms in the clone (869 ms on the branch): the miss reproduces (K18) |
| Same test totals | Unit 2,111, integration 519, frontend 197 |
| Fresh-clone test outcome | Unit: 1 failure, a **load-dependent legacy timing flake** (K22: 1/200 unloaded, 17/200 under load, same on the branch; 6/6 targeted reruns pass). Integration: 2 failures that need the M2 **Gemini** form schema, which a clone cannot recreate without an approved live Gemini call (K23); the assistant doesn't read it at runtime. Skips: 2, which need gitignored or local legacy M3 data |

**Verdict:**
- **The evaluation results reproduce identically.**
- The clone's test suite is not fully green, for the three documented, non-assistant reasons above.
- The original results are preserved, and the additional runs are recorded separately in [reproducibility](a7/reproducibility.md).

## 6. Open limitations (honest list)
1. **The 30-item citation review is unsigned.** The citations are automatically re-resolved but **not human-verified**.
2. **Deterministic latency:** p95 899 ms (final run; 867–948 ms across runs) against the 750 ms target. Checklist turns form the tail: an NRI build embeds about 18 item queries one by one. The recommended fix (batch or cache the query embeddings) was not applied, because the architecture is frozen in A7.
3. **Live voice:**
   - validity is 55 % with the fixed check (soft target 80 %); prompt v2 is unmeasured live;
   - 25 % of voice calls hit the 10 s timeout on the free tier;
   - the voice is optional and every failure falls back to the deterministic reply.
4. **LangSmith evaluation not run:** it needs a key with dataset permissions.
5. **Resident individuals are unsupported by the corpus:** it has no HDFC opening form for individuals living in India, so every resident combination gets the prescribed fallback.
6. **Not-established cells:** salary accounts, and savings accounts for organisations.
7. **Dated and scanned sources:** the organisation form is a scanned 2015 print, so OCR caveats apply to all 21 organisation cells. No current HDFC policy is in the corpus.
8. **Corpus provenance and fresh setup:** the source PDFs are gitignored, and the manifest records SHA-256 hashes but no source URLs, so a fresh clone needs the owner's PDFs. Two integration tests also need the M2 Gemini form schema (K23). One legacy SSE test is a load-dependent flake (K22).
9. **Retrieval evaluation:** 25 single-annotator cases, with no held-out split.
10. **No authentication and no automatic retention job.** Fonts load from Google Fonts. The demo video is kept outside Git (checksum in [A7_DEMO.md](A7_DEMO.md)).
11. **Not production-ready, not endorsed, not a KYC or eligibility decision.**

## 7. Final validation (A7.8)
Run on the branch at `f9da526`, with only the closure documents uncommitted, on the development stack (`scratchpad/final_results`, kept locally):

| Check | Result |
|---|---|
| `make check-schema` | no drift |
| `make lint`: ruff / black / mypy | pass / pass / pass (167 files) |
| `make test` | **2,111 passed**, 0 failed, 0 skipped |
| `make test-integration` | **519 passed**, 0 failed, 0 skipped |
| `make web-test` / `make web-lint` / typecheck / `make web-build` / `check:api` | **197 passed** / pass / pass / pass / pass |
| `make a7-eval` (regenerates the reports) | coverage, citation_review, conversations, memory, scenarios, privacy, observability: **PASS**. Reliability: **FAIL on the latency target only** (p50 66 ms, p95 899 ms, p99 977 ms); its other 10 checks pass |

**The regenerated reports match the committed ones in every result.** Only run-dependent counts changed: the log records captured (140; 0 identifiers), the observability passage sample (24; 0 uploaded), and timings.

**A7_PLAN §11 acceptance criteria:**

| # | Criterion | Status |
|---|---|---|
| 1 | Every hard §3 threshold met, or each miss documented with a fix, re-run and regression test | **Not fully met.** Latency is missed and documented (K18); the fix is recommended, not applied (architecture frozen). The live factual-safety gate failed at run time, then the check was fixed with regression tests and re-checked offline: 0 violations |
| 2 | Owner sign-off of the 30-item citation review | **Not met:** 0/30 signed (K19) |
| 3 | All checks pass; only K7, K7b and K8 may fail | **Met on the branch:** every check passes, and K7, K7b and K8 are fixed |
| 4 | Browser matrix at 4 widths with 0 console errors; demo video and `A7_DEMO.md` | **Met** |
| 5 | Fresh clone gives identical evaluation results | **Met** for the evaluation results; 3 documented test differences (§5) |
| 6 | Live run: 0 factual-safety violations, or the report says why | Violations found at run time and the check fixed (`d7fc99b`); 0 with the fixed check (§3.2) |
| 7 | Closure report, known issues, README and spec status updated; the name used consistently; no endorsement wording | **Met** |
| 8 | All commits on the branch; nothing pushed, merged, rebased or tagged; `main` untouched | **Met** |

So A7 closes as an **evaluation with documented open items** (criteria 1 and 2), not as a release.
