# Setup and Reproducibility Guide

> **Independent educational prototype.** Not an HDFC Bank or RBI system. Use public, authorised or synthetic documents and **synthetic data only**. Never put real customer data, real credentials or API keys in the repository, in issues or in logs.

## 1. Supported environment
- **Verified on:** Windows 11 (CPU only), Python 3.13.5 (the project requires ≥ 3.11), Docker 28.x, GNU Make 4.4.
- **Also supported:** the Makefile works from PowerShell/CMD (GNU Make runs recipes with `cmd.exe`), Git Bash, Linux and macOS. Repo paths with spaces are fine.
- **Prerequisites:**
  - Docker (Postgres 16, Redis 7, Qdrant 1.20 via `infra/docker-compose.yml`);
  - Python 3.11+;
  - GNU Make (Windows: `winget install ezwinports.make`);
  - Tesseract OCR 5.x (Windows: `winget install UB-Mannheim.TesseractOCR`), for scanned pages;
  - roughly 2 GB free disk for Docker volumes and model caches;
  - network access on the first run, to download the BGE-small embedding model (~135 MB) and Python packages.
- **No API key is needed** for ingestion, retrieval, form schemas from stored chunks, the workflow without a live LLM, or any test. Groq, Gemini and LangSmith keys are optional and their live use is gated off by default.

## 2. From a clean checkout
```bash
git clone <repo-url> && cd <repo>
cp infra/.env.example .env      # optional; .env (gitignored) overrides infra/.env.example
make venv install               # creates .venv, installs the backend with dev tools
#   or: make venv install-locked    (exact tested versions, backend/requirements.lock)
make up                         # Postgres on host port 5433, Redis 6379, Qdrant 6333/6334
make migrate                    # Alembic 0001 → 0008
make check-schema               # ORM == migrations ("No new upgrade operations detected")
```
**Configuration rules:**
- `.env` is machine-specific and must never be committed.
- Keep secrets (`GROQ_API_KEY`, `GEMINI_API_KEY`, `QDRANT_API_KEY`, `LANGSMITH_API_KEY`) only there.
- Leave `GROQ_LIVE_ENABLED=false` and `GEMINI_LIVE_ENABLED=false` unless you deliberately opt in after reviewing the providers' data terms (`docs/02`).
- Leave `NLI_MODEL` empty: M4 ships with NLI disabled (no configuration qualified).

## 3. Corpus and ingestion
The source PDFs are not in Git. Place the 9 documents listed in `data/raw_pdfs/manifest.yaml` under `data/raw_pdfs/{regulatory,policy,forms,reference}/` (see `docs/14-source-corpus.md`). Then:
```bash
make check-corpus               # manifest + SHA-256 of every PDF + Tesseract smoke test
make ingest-dry                 # optional: what would change; writes nothing
make ingest                     # validate → extract → OCR → chunk → Postgres → embed → Qdrant → reconcile
make reconcile                  # Postgres ↔ Qdrant consistency (read-only)
make forms                      # deterministic form schemas from stored chunks (no API key)
make search Q="documents for opening an account of a partnership firm"
make eval                       # M1 Recall@5 on the development retrieval set
```
**Behaviour:**
- Ingestion is idempotent: a second run writes and embeds nothing unless a source changed.
- A failing document is reported with its stage and is never marked `completed`. Fix the cause, then `make retry`.

## 4. Running the application
```bash
make api                        # FastAPI on http://localhost:8000 (docs at /docs, health at /api/v1/health)
make demo                       # second terminal: the synthetic LLP demo through the running API
```
**What to expect:**
- **Without a live LLM**, intake asks every clarification question and the report is honestly **DEGRADED**.
- **With NLI disabled** (the M4 default), every report that has a checklist is DEGRADED, every claim is `not_verified`, and every item has an open `not_verified` review case: `GET /api/v1/reports/{id}/review-cases`.
- **`make demo-live`** requires the owner's live Groq gate and the terms date in `.env`. Set the gate back to `false` afterwards.
- **`make web`** refuses: the frontend is not built (M5).

## 5. Tests and checks
| Command | What it runs | Expected at M4 close |
|---|---|---|
| `make test` | unit tests (markers `integration` and `nli` excluded) | all pass except the known environment-dependent `test_live_calls_and_cache_are_off_by_default` (see §6) |
| `make test-integration` (or `cd backend && python -m pytest -m integration tests/integration`) | integration tests on throwaway databases plus the dev stack (needs `make up` and an ingested corpus) | all pass except the 2 known Qdrant tied-score tests |
| `make test-nli` | the `nli` marker: one pinned checkpoint, offline only, skipped unless cached | skipped on a clean machine (no model is downloaded by tests) |
| `make lint` | ruff + black --check + strict mypy (`mypy app`) | clean |
| `make check-schema` | `alembic check` | "No new upgrade operations detected" |
| `make check-workflow` | read-only retention and review-case integrity report | `"ok": true` |
| `make check-dense-golden` | M1 dense regression against the golden snapshot | see the Qdrant note in §6 |
| `make reconcile` | Postgres ↔ Qdrant | CONSISTENT |

Exact counts from the final M4 run are in `docs/handoff/03-evaluation-report.md` §7.

## 6. Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| `test_live_calls_and_cache_are_off_by_default` fails with `GROQ_DATA_TERMS_VERIFIED_ON = <a date>` | The test builds `Settings` from `infra/.env.example`, but process environment variables take precedence. The local environment carries the owner's Groq terms-review date from the M3 live run. Live calls are still off (the `GROQ_LIVE_ENABLED` assertion passes). | Environment-dependent, not a code defect. Run the suite in an environment without `GROQ_DATA_TERMS_VERIFIED_ON`. Do not edit the test. |
| `test_dense_golden` / `test_hybrid_dev` fail on query `m1-14` | Two candidates have tied scores in Qdrant, and their order is not deterministic on this stack | Known baseline issue; documented, not modified |
| Postgres connection refused on 5432 | Postgres is mapped to host port **5433** to avoid clashing with a native install | Use the `DATABASE_URL` from `infra/.env.example` |
| OCR errors / `tesseract not found` | Tesseract not on `PATH` or in the standard install folder | Set `TESSERACT_CMD` (and optionally `TESSDATA_DIR`) in `.env` |
| The first ingest or search is slow | The BGE-small model downloads on first use | Wait once; later runs use the local Hugging Face cache |
| `make` is not recognised (Windows) | GNU Make missing | `winget install ezwinports.make`, or run the command behind each target from `backend/` with the venv interpreter |
| A migration test reports drift at an old revision | `alembic check` compares the ORM (head) with the database | Run checks at `head` (the migration tests already do) |
| A report is DEGRADED with `verification` | Expected: NLI is disabled in M4 | Not an error; see the limitations register |
| `check_report` reports `citation_mismatch` on a real trust run | Known pre-existing M3 issue: a child-chunk citation carries its parent paragraph's page range | Documented in the known-issues register; not fixed in M4 |

## 7. Reproducibility notes
- **Evaluation datasets and records** are tracked under `data/eval/`, including the S4 NLI selection record (`m4_nli_selection.json`, write-once). The M4 held-out claim set is frozen (SHA-256 `eee0a575…d90203`) and unused.
- **The S4 NLI selection is not re-runnable by design:** `make eval-claims-select` refuses because the record exists. Re-running it would also download about 2 GB of checkpoints. Do not re-run it to "check" a result; the committed record is the result.
- **No test downloads a model or calls an external LLM.** Live providers are gated and are fakes in CI.
- **Timings** in the evaluation report are indicative measurements on one laptop CPU.
