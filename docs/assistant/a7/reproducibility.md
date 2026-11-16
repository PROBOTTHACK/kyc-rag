# A7.7: fresh-clone reproducibility (E13)

Run on 2026-11-16. The branch `feat/account-opening-assistant` at `f9da526` was cloned into a temporary directory (`%TEMP%/a7-fresh`), with:
- a **separate Docker Compose project**, `kyc_a7_fresh`, with its own volumes and host ports 15433 / 16379 / 16333. The development stack (`infra-*`, ports 5433 / 6379 / 6333) was never touched;
- a **clean process environment:** only OS essentials, with settings from the clone's own `.env` copied from `infra/.env.example` with the ports changed. This shell exports the development settings (`DATABASE_URL`, `QDRANT_URL`, `GROQ_*` …), and those outrank `.env`, so a clean environment was needed;
- **live LLMs off:** `GROQ_LIVE_ENABLED=false`, `GEMINI_LIVE_ENABLED=false`, and no keys.

Steps followed the documented setup:
- `python -m venv` → `pip install -r backend/requirements.lock` → `pip install --no-deps -e backend[dev]` (`pip check`: no broken requirements);
- `npm ci`;
- `alembic upgrade head` and `alembic check` (no drift);
- copy the 9 source PDFs (the owner's local files, gitignored) → `validate-manifest` (**9/9 SHA-256 match**) → `check-ocr`;
- `ingest` → `forms extract` (deterministic) → every check and evaluation.

## 1. Corpus
| Check | Fresh clone | Development | Same? |
|---|---|---|---|
| Documents completed | 9/9 | 9/9 | yes |
| Chunks | 456 | 456 | yes |
| Chunk ids and text (MD5 over id + MD5(content), ordered by id) | `7b427626…eb3b` | `7b427626…eb3b` | **identical**, OCR text included |
| Qdrant points / Postgres ↔ Qdrant consistency | 429 / CONSISTENT | 429 | yes |
| Deterministic form schemas (`m1_chunks`) | 6 | 6 | yes |
| Gemini form schemas (`gemini_multimodal`) | **0** | present (M2 live extraction) | **no**: see §4 |

## 2. Original results (first run, preserved unchanged)
| Check | Result |
|---|---|
| `alembic check`, ruff, black, mypy | pass |
| Unit tests | **2,109 passed, 1 failed, 1 skipped** (2,111) |
| Integration tests | **516 passed, 2 failed, 1 skipped** (519) |
| Frontend: Vitest / lint / typecheck / build / `check:api` | 197 passed / pass / pass / pass / pass |
| A7 suites (`runner all`) | coverage, citation_review, conversations, memory, scenarios, privacy, observability **PASS**; reliability **FAIL**: only the known latency miss, p95 948 ms |
| `assistant-eval` Recall@5 (dense / BM25 / hybrid) | 0.733 / 0.613 / 0.793 |

## 3. Investigation (additional runs, recorded separately)
**Unit failure:** `test_events.py::test_every_exit_path_releases_the_subscription[cancel]`, a legacy M3 SSE test.
- **Cause:** a timing race in the test. The stream's idle timeout (`idle_timeout_s=0.05`) equals the test's sleep before `task.cancel()`. If the stream's idle check (`app/workflow/stream.py:247`) fires first, the stream ends normally and `CancelledError` is never raised. The subscription is released either way, and that is the property under test.
- **Additional runs:**
  - full unit suite in the clone, run A: 2,110 passed, 0 failed (1 skipped);
  - the test alone, 3× in the clone and 3× on the branch: **6/6 passed**;
  - the cancel case called 200× in-process: **1 failure** without load, **17 failures (8.5 %)** with 16 CPU-burning workers.
- **Classification: pre-existing timing flake in legacy test code.** It is the same code on the branch and in the clone, and it is load-dependent. It is not an assistant defect and not caused by the clone. It is recorded as **K22**. The test is not modified (legacy code; owner rules).
- **Proposed fix (owner decision):** a longer idle timeout in the `cancel` case only, with the assertions unchanged.

**Integration failures:** both are caused by the missing Gemini form schemas.
- `test_assistant_retrieval_dev.py::test_every_cited_chunk_resolves_to_the_claimed_hdfc_document_and_page`:
  - the chunk checks pass;
  - the provenance check of one coverage evidence entry fails. That entry references the field "Type of Entity:-" in the **Gemini-extracted** schema (`hdfc_non_individual_account_opening`, `gemini_multimodal` v2; `coverage.yaml:122`).
- `test_graph_s4_dev.py::test_trust_finds_rbi_para_35_and_uses_the_gemini_form_schema` (legacy M3): expects that same Gemini schema.
- **The assistant never reads `form_schemas` at runtime** (`coverage.py` only validates the reference's form key). That is why the clone's coverage suite passes: all 1,332 citations re-resolve.
- Recreating the Gemini rows needs a live Gemini extraction, which is not approved. **Classification: reproducibility gap (K23)**, not a defect.

**Skipped tests:** both need local legacy data, confirmed with `pytest -rs`.
- `test_m4_models.py:269`: "no local M3 demo records (gitignored)".
- `test_m4_report_compat.py:99`: "the developer database holds no stored reports".

They run on the development machine because that data exists there.

## 4. Branch vs fresh clone
| Evaluation | Branch | Fresh clone | Same? |
|---|---|---|---|
| Coverage report: 55/55, 28/28, 12/12 + 6/6, 1,332/1,332 citations, golden item ids | PASS | PASS | **identical report** |
| Conversations (91/91), memory, scenarios (0 leaks), citation-review sample (same 30 items) | PASS | PASS | **identical reports** |
| Privacy: refusals 29/29, redactions 7/7, controls 15/15, 0 leaks | PASS | PASS | identical, except the number of log records captured (195 vs 140; identifiers 0 in both) |
| Observability | PASS | PASS | same checks. Run-dependent: overhead, passage sample size; no LangSmith key in the clone |
| Reliability: functional checks | all pass | all pass | yes |
| Deterministic latency p95 (machine-dependent) | 869 ms | 948 ms | both miss 750 ms (K18) |
| Retrieval Recall@5, dense / BM25 / hybrid | 0.733 / 0.613 / 0.793 | 0.733 / 0.613 / 0.793 | **identical** |
| Test totals: unit / integration / frontend | 2,111 / 519 / 197 | 2,111 / 519 / 197 | same tests |

## 5. Verdict
**The evaluation results reproduce:** the same chunks, item ids, citations, pass rates and retrieval figures (A7_PLAN §3 "identical"). The fresh-clone test suite is **not fully green**, for documented reasons:
- the 2 integration tests that need the M2 Gemini schemas (K23);
- 1 load-dependent legacy timing flake (K22);
- 2 skips that need gitignored or local legacy data.

None of these affects the assistant's answers.

**Prerequisites a fresh clone needs and Git does not provide:**
- the 9 source PDFs (hashes in `manifest.yaml`);
- for the 2 Gemini-dependent tests, an approved live Gemini extraction;
- for the 2 skipped legacy tests, the local M3 records.
