# M1 Handoff: Document Intelligence Foundation

**Status: M1 complete (Stages A–F).** Branch `feat/m1-document-ingestion`. Stages A–E are in commit `e71ba72`. Stage F, the Windows-compatible Makefile and this handoff are in the follow-up commit "M1: complete ingestion pipeline and handoff" (local, not pushed).
Verified 2026-11-08 on Windows 11 (CPU only), Python 3.13.5, Docker 28.3.2, GNU Make 4.4.1. Every number below was measured on this repository state unless it is labelled *historical*.

> This is an independent, educational portfolio prototype. It is **not** an HDFC Bank or RBI system, it does not approve or reject customers, and it gives no legal or compliance advice. M1 proves that the source documents can be ingested faithfully, stored with provenance, and retrieved with page citations. It proves nothing yet about answer correctness or regulatory compliance.

---

## 1. Project and M1 objective
The Agentic KYC Compliance Engine (README, `docs/00`) will turn RBI KYC directions and HDFC Bank KYC material into cited, verified, human-reviewable checklists of documents and requirements. A later LangGraph workflow (M3) builds the checklists, and claim-level NLI verification (M4) checks them.

**M1's objective** (`docs/12` §M1, `docs/milestones/M1_PLAN.md`): build the document-intelligence foundation everything else depends on. That means Pipeline A (PDF → pages → OCR fallback → section-aware chunks → Postgres → BGE embeddings → Qdrant), a CLI (`make ingest`), a `search` command that returns the top 5 passages with document and page citations, and Recall@5 measured on at least 15 hand-labelled queries.

## 2. Scope
**In M1:** corpus inspection (`data/processed/corpus_report.md`); the manifest and its validation; extraction; OCR fallback; chunking; the evidence-eligibility and OCR-reliability policies; the Postgres schema and idempotent persistence; embeddings and Qdrant indexing; reconciliation and recovery; the unified CLI and Make targets; filtered dense search; the Recall@5 benchmark.

**Out of scope (by plan):**
- BM25/hybrid retrieval, reranking and Pipeline B (multimodal form-schema extraction): M2.
- FastAPI ingestion/query endpoints, the queue, the LangGraph workflow, LLM calls and Redis use: M3.
- Claim verification (NLI), the rules layer, security guardrails for LLM input and output, and human review: M4.
- Frontend: M5. Full 50-case benchmark and observability: M6. Deployment: M7.

M1 makes **no LLM calls and needs no API key.**

## 3. Architecture
```
manifest.yaml + PDFs ──validate (schema, SHA-256)──►
  A  PyMuPDF extraction: physical pages, bookmarks→sections, footnotes, ruled tables
  B  page quality → Tesseract OCR fallback (fold-out halves, on-disk cache)
  C  section-aware chunking: profile chunkers, parent/child split, OCR evidence policy
  E  one Postgres transaction per document (source of truth)
  D  BGE-small-en-v1.5 embeddings of embedding-eligible chunks, read back from Postgres
     → Qdrant `policy_chunks` (derived index) → per-document point check
  F  status + run log; whole-store integrity + bidirectional reconciliation
search: query → BGE (query instruction) → Qdrant filtered by eligibility → top 5 with citations
        (+ child → parent paragraph from Postgres)
```
- **PostgreSQL is the source of truth.** It stores all 456 chunks, the 27 parents included, with both raw and normalised text, every provenance and OCR field, a record for every page, the exclusions, and a log of every ingestion attempt.
- **Qdrant is a derived vector index.** It holds the 429 embedding-eligible chunks and is indexed only from committed Postgres rows. The two stores never share a transaction. Consistency is *verified* (`reconcile`) and *restored* (`retry`, `reconcile --repair`) instead of being assumed.
- **Models and tools:**
  - PyMuPDF 2.1 for extraction;
  - Tesseract 5.4.0 (system binary) for OCR;
  - `BAAI/bge-small-en-v1.5` pinned at revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` (384-dimensional, cosine, CPU), through sentence-transformers;
  - Qdrant v1.20.0;
  - Postgres 16;
  - SQLAlchemy 2 + Alembic.
- Module map: `docs/01-architecture.md` ("Document ingestion"). Full rules: `docs/04-ingestion-pipelines.md`.

## 4. Stages A–F (what was built)
| Stage | Implementation | Key behaviour |
|---|---|---|
| A: extraction | `app/ingestion/{pdf,structure}.py` | Every physical page gets a record; citations use **physical** page numbers. Bookmarks give section paths. Footnotes are separated only when a matching superscript marker exists. Running page numbers and contents pages are removed and counted. Ruled tables are detected (`rbi_directions` profile only). |
| B: quality + OCR | `app/ingestion/{quality,ocr,pages}.py` | OCR runs when a page has fewer than 50 text-layer characters **and** images cover more than 80% of it. The page is rendered at 300 dpi; Tesseract runs with PSM 3, `eng`, single thread (deterministic). Fold-out pages are read as separate left/right halves. Page status is `ok`, `low_quality`, `ocr_low_confidence` (mean word confidence < 70) or `failed` (always with an error). The OCR cache is keyed by PDF hash, page, region, DPI, PSM, language and Tesseract version. |
| C: chunking | `app/ingestion/{cleaning,chunking}.py`, `chunkers/{rbi_directions,rbi_faq,question_headings,form}.py` | One unit per numbered paragraph, FAQ question, policy question heading or table row; forms are chunked per section. Chunks hold at most 300 words. A longer paragraph becomes a **parent** (stored, never embedded) plus **children** split at sub-clauses; the children rebuild the parent exactly, and each child after the first carries its paragraph's lead as `context`. Ids are deterministic: `uuid5(sha256, chunk_index)`. |
| D: embeddings + Qdrant | `app/retrieval/{embeddings,qdrant_store,indexing,search}.py` | The embedding input is the child `context` plus the content (no title or section). Inputs over 512 tokens are an error, never truncated. Indexing is idempotent: changed input or model → re-embed; changed metadata only → overwrite the payload; unchanged → skip; stale points are deleted *after* the upserts. A collection with the wrong vector size is never recreated automatically. |
| E: Postgres persistence | `app/db/{models,corpus,persistence,repository,runs}.py`, migration `0002_m1_persistence` | Per-document upsert. Unchanged input writes nothing; changed rows are updated; removed chunks are deleted. Parent/child integrity is enforced before any write and by database constraints. `ingestion_runs` records each stage's outcome and survives a rolled-back first ingestion. Reconciliation runs in both directions. |
| F: integration | `app/ingestion/{pipeline,cli}.py`, `app/retrieval/evaluation.py`, `app/core/retry.py`, `app/db/migrations.py`, `Makefile` | `run` / `make ingest` covers validation, services, documents and verification. Also added: `--dry-run`, persist-only, `index --from-db`, `retry`, `search`, `evaluate`; bounded Qdrant backoff (2 retries); a preflight that refuses an unmigrated database; and the 20-query benchmark. |

## 5. Corpus and measured counts
9 PDFs (gitignored; not redistributed; layout and provenance in `docs/14-source-corpus.md`, `data/raw_pdfs/manifest.yaml`):

| document_key | Bank / type | Source status → eligibility | Quality | Pages | Chunks | Eligible |
|---|---|---|---|---|---|---|
| `rbi_kyc_directions_2025` (169MD, "Updated as on October 1, 2026") | RBI regulatory | active → **primary** | text | 101 | 229 (27 parents) | 202 |
| `rbi_kyc_faqs` (June 9, 2025) | RBI regulatory | dated → supporting | text | 10 | 37 | 37 |
| `hdfc_kyc_aml_faq` | HDFC policy | dated → supporting | text | 3 | 13 | 13 |
| `hdfc_nri_account_opening` | HDFC form | unknown → supporting | text | 6 | 68 | 68 |
| `hdfc_resident_to_nro_conversion` | HDFC form | unknown → supporting | text | 6 | 59 | 59 |
| `hdfc_nri_rekyc` | HDFC form | unknown → supporting | scanned | 3 | 12 | 12 |
| `hdfc_non_individual_account_opening` | HDFC form | unknown → supporting | scanned (fold-out) | 2 | 21 | 21 |
| `hdfc_resident_rekyc_dormant` | HDFC form | unknown → supporting | text | 2 | 9 | 9 |
| `hdfc_netbanking_corporate_application` | HDFC reference | unknown → **context_only** | text | 2 | 8 | 8 |

Totals, measured from Postgres and Qdrant:

| Metric | Value |
|---|---|
| Documents | **9**, all `completed` |
| Physical page records | **135**: 130 native text, **5 OCR**. Page status: 133 `ok`, 2 `ocr_low_confidence`, 0 `failed` |
| Chunks | **456** = **332 single + 27 parent + 97 child** |
| Embedding-eligible chunks / Qdrant points | **429 / 429** |
| Parent/child families | **27**, all intact (children rebuild each parent's text exactly) |
| Chunks by eligibility | 229 primary, 219 supporting, 8 context_only |
| Text reliability | 423 native, 10 OCR reliable, 23 OCR uncertain |
| Can support MANDATORY | 229 (all from the RBI Directions) |
| Document-level exclusions | 774; chunks with excluded OCR lines: 18 |
| Reconciliation | 0 missing, 0 without a Postgres chunk, 0 not eligible, 0 out of date |

## 6. Source authority, provenance and eligibility
- **Identity:** `document_key` names the logical document; `sha256` identifies the exact bytes, and all row and point ids derive from it. The manifest pins each SHA-256; changed bytes fail validation and are never ingested silently. **One version per key** in M1, enforced by the validator and by `uq_documents_document_key`. `supersedes` links come only from the manifest, never from a hash change.
- **Authority:**
  - `bank` (RBI or HDFC) is the corpus context.
  - `source_authority` stays `null` unless the issuer is verified.
  - `attribution_basis` (`document_text`, `document_visual` or `filename_only`) records how the issuer was established. A filename can never set an authority: `hdfc_kyc_aml_faq` is `filename_only`, so its authority is `null`.
  - Unknown dates and versions stay `null`; nothing is invented.
- **Evidence eligibility** is derived, not chosen: `derive_evidence_eligibility` in `app/schemas/corpus.py`.
  - Active regulatory or policy text is `primary`.
  - References are `context_only` and are excluded from search by default.
  - Everything else is `supporting`: forms (which show what a form asks for, never a legal obligation), and dated or unknown sources.
- **Can support MANDATORY** = `primary` **and** not OCR-uncertain. This is enforced by a database CHECK and is filterable in search (`--mandatory-only`). Vocabularies are in `docs/15-taxonomy.md`.
- **Provenance per chunk:**
  - physical page and `page_end`, printed page when reliably known;
  - section path, paragraph label, fold-out region, content kind;
  - footnotes, page status, OCR confidence;
  - raw text plus normalised content, and excluded OCR lines with their confidence.

## 7. OCR reliability
- Five pages need OCR: the NRI re-KYC form (3, clean scan) and the non-individual account form (2 two-up fold-out pages, about 100 dpi).
- `ocr_reliable` requires every page the chunk spans to be `ok` **and** the chunk's mean line confidence to be ≥ `OCR_RELIABLE_MIN_CONFIDENCE` (0.80; a manifest entry may override it). Otherwise the chunk is `ocr_uncertain`.
- Uncertain OCR text is kept, embedded by default (`OCR_EMBED_UNCERTAIN=true`) and flagged, but it can **never** support MANDATORY. Lines with mean word confidence < 40 are left out of the content and stored on the chunk.
- **Limitations:**
  - the fold-out form's pages are `ocr_low_confidence`, and its text is noisy;
  - printed page numbers come only from clean "Page N of M" reads;
  - the 0.80 threshold was chosen from this corpus, not validated generally;
  - there is no handwriting, checkbox or signature understanding (Pipeline B, M2).

## 8. PostgreSQL schema (Alembic)
- `0001_m1_corpus`: `documents`, `document_pages`, `chunks`, with TEXT + CHECK enums.
- `0002_m1_persistence` (additive; it backfills the promoted columns and downgrades without data loss):
  - `document_pages.metadata` (page record, OCR regions);
  - `chunks.{raw_text, chunk_role, text_reliability, embedding_eligible, can_support_mandatory, updated_at}`;
  - the same-document parent FK `(document_id, parent_chunk_id) → chunks(document_id, id)`, DEFERRABLE INITIALLY DEFERRED with no delete action, so a parent can't be deleted while children reference it;
  - CHECKs: exactly the children have a parent, parents are never embedded, a chunk isn't its own parent, `text` ⇔ `native`, and the `can_support_mandatory` rule;
  - `document_exclusions`;
  - `ingestion_runs` (per-attempt stage map, status, failed stage, error; no FK to `documents`).
- `documents.ingestion_status`:
  - `pending` = stored, not yet indexed;
  - `processing` = a run is in progress;
  - `completed` = every stage succeeded and the document's points match;
  - `partial` = every stage ran but some pages failed;
  - `failed` = the last run failed (earlier rows and vectors stay intact).
- Details: `docs/08-data-model.md`. Drift check: `make check-schema` (`alembic check`).

## 9. Qdrant
- Collection `policy_chunks` (`QDRANT_COLLECTION`). One unnamed vector of 384 dimensions, cosine distance. Point id = `chunk_id`.
- The payload is `ChunkMetadata.to_qdrant_payload()` plus an `index` object (model, revision, input version, input and payload SHA-256) used for idempotent re-indexing.
- Payload indexes:
  - keyword: `document_id`, `document_key`, `bank`, `document_type`, `source_status`, `evidence_eligibility`, `entity_types`, `residency_statuses`, `text_reliability`, `content_kind`, `chunk_role`;
  - boolean: `can_support_mandatory`.
- Search filters on eligibility (default primary + supporting) and optionally on entity types and groups, bank and document. Queries get the BGE instruction prefix; passages don't. Eligibility is a filter, never a ranking signal.
- Idempotent calls (upsert or delete by id, payload overwrite, reads) retry transient failures twice, after 0.5 s and 1 s (`app/core/retry.py`). `create_collection` is never retried. Points of documents missing from Postgres are deleted only on request (`--delete-unknown`, or `index --prune` in the Stage D in-memory mode).

## 10. CLI and Make targets
Run from the repo root. `make` (or `make help`) prints the target list. CLI equivalent: from `backend/`, `<venv python> -m app.ingestion.cli <command>`.

| Make | CLI | Purpose |
|---|---|---|
| `make ingest [DOC="k1 k2"]` | `run [--document K]… [--path DIR] [--force] [--dry-run]` (alias `ingest`) | Full workflow plus verification |
| `make ingest-dry` | `run --dry-run` | Stages A–C; the Postgres write is rolled back; Qdrant is compared read-only; nothing written |
| `make persist` | `persist` | A–C + Postgres only (documents end `pending`) |
| `make index` | `index --from-db` | Embed and index from committed rows (no PDF read). Plain `index` is the Stage D in-memory indexer, kept for experiments |
| `make retry` | `retry` | One recovery attempt per document that isn't `completed`/`partial` (index-only when the rows are current) |
| `make reconcile` / `make repair` | `reconcile [--repair [--delete-unknown]]` | Bidirectional check (read-only) / re-index inconsistent documents from Postgres |
| `make runs` | `runs [--document K] [--failed] [--limit N]` | Ingestion history with failed stage and error |
| `make search Q="…"` | `search QUERY [--top-k] [--entity] [--bank] [--document] [--include-context-only] [--mandatory-only] [--expand]` | Top-5 passages with citations |
| `make eval` | `evaluate [--dataset] [--top-k] [--report]` | Recall@5 benchmark (report written to `data/processed/m1_retrieval_report.md`, gitignored) |
| `make check-corpus` | `validate-manifest`, `check-ocr` | Manifest + SHA-256; real Tesseract smoke test |
| `make up` / `down` / `migrate` / `check-schema` | — | Docker services; Alembic upgrade; drift check |
| `make test` / `test-integration` / `lint` / `fmt` | — | pytest (unit / integration); ruff + black + mypy |

CLI exit codes: `0` ok; `1` a document failed or the stores are inconsistent; `2` invalid input or a service not ready (nothing written).

**Windows.** From PowerShell/CMD, GNU Make (`winget install ezwinports.make`) runs recipes with `cmd.exe`. The Makefile therefore calls the venv interpreter by an absolute, double-quoted path (`"<repo>\.venv\Scripts\python.exe"`; `"<repo>/.venv/bin/python"` elsewhere), which `cmd.exe` and `sh` both accept, including paths with spaces. `make` defaults to `help`. Open a new terminal after installing Make so PATH updates.

## 11. Retrieval evaluation
- **Data:** `data/eval/m1_retrieval.jsonl`, with **20 queries and 28 target passages**. The queries are natural questions on topics not used by the Stage D development probes. Each target is `(document_key, physical page[, paragraph][, text_contains])`.
  - I (the implementing assistant) labelled them from the source text and checked them against stored chunks before the first run.
  - Labels were **not** changed after results were seen.
  - A test asserts that every target matches an eligible chunk.
- **Method:** dense BGE-small search with production filters (primary + supporting), top 5. A hit matches a target when it's the same document, its page range covers the target page, and the paragraph and `text_contains` match when given.
  - Recall@5 = matched targets / targets, per query, averaged.
  - Hit@5 = at least one target in the top 5.
  - MRR@5 = mean of 1 / rank of the first matching hit.
- **Result** (re-run for this handoff): **Recall@5 0.825, Hit@5 0.90, MRR@5 0.775.** Misses:
  - m1-01: sole proprietorship, para 31 outranked by paras 33, 34 and 30;
  - m1-05: second page of the FPI table;
  - m1-06: small-account para 28;
  - m1-19: company beneficial-owner definition clause;
  - m1-20: right form and page, different chunk than the one labelled.
- **Caveat:** this is a **small initial labelled set from a single annotator**. It is a baseline for M2 comparisons, not a research-grade estimate. It needs independent review, a second annotator and more queries (including entity-filtered ones) before it supports stronger claims.

## 12. Failure handling, recovery and reconciliation
- **Per-document isolation:** one document's failure never stops or alters another.
- **A document is never `completed` after a failure.** Each attempt is an `ingestion_runs` row with a per-stage status (`extraction`, `ocr`, `chunking`, `persistence`, `embedding`, `indexing`: `ok`, `failed` or `skipped`) and the error.
- **Postgres first, Qdrant second.** A failed embedding or upsert never removes existing vectors, because stale deletes come only after successful upserts.
- **Verified end to end** (*historical*, 2026-11-08, Stage F, on a throwaway database and collection with the real model):

| Scenario | Outcome |
|---|---|
| Qdrant down before a run | Preflight retried twice, then refused: exit 2, nothing written |
| Qdrant stopped mid-run | 9/9 documents `failed` at `indexing` and recorded; Postgres intact (456 chunks) |
| Recovery with `retry` | 9/9 `completed` via index-from-Postgres, 0 re-embeddings; the next `run` was a no-op |
| 20 vectors deleted | `reconcile` reported exactly 20 missing, exit 1, deleted nothing; `repair` restored them |

- **Defect found and fixed in Stage F:** verification used to crash with a traceback when Qdrant was unreachable. It now reports "verification incomplete" and exits 1 (regression test included).
- **Integration tests also cover:**
  - partial upserts;
  - embedding failure;
  - failed stale delete;
  - a rolled-back first ingestion (still recorded);
  - extraction failure;
  - OCR unavailable → `partial`;
  - refused parent removal;
  - a second version under an existing key;
  - persist-only → `index --from-db`;
  - `retry` routing;
  - the unmigrated-schema preflight;
  - a diagnostic reconcile never deleting.

## 13. Test and tooling results (current, 2026-11-08, via the Make targets under `cmd.exe`)
| Check | Result |
|---|---|
| `make test` (unit) | **266 passed**, 70 deselected (38.4 s) |
| `make test-integration` (live Postgres + Qdrant, throwaway databases and collections) | **70 passed** (3 min 58 s) |
| `make lint`: ruff / black / mypy (strict) | All checks passed / 84 files unchanged / no issues in 54 source files |
| `make check-schema` (`alembic check`) | No new upgrade operations; database at `0002_m1_persistence` |
| `make check-corpus` | Manifest OK (9 documents, 0 errors, 0 warnings); OCR OK (Tesseract 5.4.0) |
| `make ingest` on the dev data | 9 completed, 0 rows written, 0 embeddings, CONSISTENT |
| `make reconcile` | CONSISTENT (429 = 429) |
| `make eval` | Recall@5 0.825, Hit@5 0.90, MRR@5 0.775 |
| `git diff --check` | clean |

**Makefile verification on Windows** (GNU Make 4.4.1):
- **Run under `cmd.exe`:** `make`/`help`, `ingest` (all documents and `DOC=` selection), `ingest-dry`, `up`, `migrate`, `check-schema`, `lint`, `test`, `test-integration`, `check-corpus`, `reconcile`, `runs`, `index`, `retry`, `search`, `eval`.
- **Run under Git Bash `sh`:** selected targets.
- **Linux/macOS branch:** simulated under `sh` with a stand-in interpreter and a path containing spaces. **Not run on real Linux or macOS.**
- **Intentional or not executed:** `web` exits non-zero on purpose (not implemented). `install`, `install-locked` and `lock` were only expanded with `make -n` (running them would modify the venv or lockfile). `api` and `down` were not run.

## 14. Performance (environment-specific: Windows 11 laptop CPU, Docker Desktop; indicative only)
| Run | Time |
|---|---|
| Fresh database and collection, 9 documents, warm OCR cache *(historical, Stage F)* | 64.0 s pipeline / 69.9 s wall clock; 169MD 19.8 s |
| Stages A–C with an **empty** OCR cache *(historical)* | 43.5 s (7.8 s warm) |
| Forced re-embed of 429 vectors *(historical)* | 58.1 s |
| `make ingest` on unchanged data (current) | 33.6 s pipeline, 40 s wall clock; most of it is the model load and verification |
| `make eval` (current) | 27 s, including the model load |

## 15. Known limitations and technical debt
- **Dense-only retrieval** (Recall@5 0.825). Hybrid BM25 + RRF and reranking are M2.
- **Small, single-annotator benchmark** (§11).
- **One version per `document_key`.** Multi-version history needs a migration plus `supersedes` handling.
- **OCR quality:** the fold-out form is noisy; there's no form-structure understanding.
- **`partial` documents** are not auto-retried. Fix the cause (for example, install Tesseract), then run `make ingest DOC=<key>`.
- **No concurrency lock:** two simultaneous runs on one document aren't serialised (the Redis lock arrives with the M3 queue). Database constraints prevent duplicates, but don't run ingestion in parallel.
- **The model loads on every write command** (about 10–15 s) to check the collection's vector size.
- **The Stage D in-memory `index`** (without `--from-db`) bypasses Postgres. It's kept for experiments; reconciliation catches any drift it causes.
- **No ingestion API or queue** (`POST /ingestion/run`): M3.
- **The Makefile's Linux/macOS branch** is not tested on a real Linux or macOS machine.
- **Dependencies:** `backend/requirements.lock` reflects the tested environment. `pyproject.toml` uses lower bounds.

## 16. Security and data handling
- **No secrets in the repo.** `.env` is gitignored, and `infra/.env.example` holds only blank keys and a local Docker development password. M1 needs no API keys. Connection strings are `SecretStr` and never logged.
- **Source PDFs are not committed or redistributed** (`data/raw_pdfs/**/*.pdf` ignored). Only `manifest.yaml` with hashes is tracked. The OCR cache and generated reports in `data/processed/` are ignored, except the reviewed `corpus_report.md`.
- **No customer data or PII:** the corpus is public regulatory and bank material; future customer data must be synthetic (CLAUDE.md).
- **Retrieved text is untrusted.** M1 only stores and returns it (no LLM), so it has no prompt-injection surface yet. M3/M4 must wrap evidence in delimiters and never treat it as instructions (`docs/06`).
- **Bounded external calls:** Postgres has a 5 s connect timeout; Qdrant has a 30 s client timeout plus at most 2 retries. Data lives in the Docker volumes `pgdata` and `qdrantdata`, outside the repo. Qdrant runs locally without an API key; set `QDRANT_API_KEY` for any non-local deployment.

## 17. Setup for a new developer
Prerequisites: Docker, Python 3.11+, GNU Make, Tesseract 5.x (Windows: `winget install ezwinports.make` and `winget install UB-Mannheim.TesseractOCR`), and about 135 MB for the embedding model download.
```
git clone <repo-url> && cd <repo>
git checkout feat/m1-document-ingestion
python -m venv .venv            # Linux/macOS: python3 -m venv .venv   (= make venv)
make install                    # or: make install-locked (exact tested versions)
# optional: copy infra/.env.example to .env to override settings (config falls back to .env.example)
make up                         # Postgres :5433, Redis :6379, Qdrant :6333
make migrate                    # Alembic 0001 + 0002
make check-schema
# obtain the 9 public PDFs and place them per data/raw_pdfs/manifest.yaml (docs/14)
make check-corpus               # hashes must match the manifest; Tesseract smoke test
make ingest-dry                 # optional preview
make ingest                     # about 1-2 min first time (model download extra)
make search Q="documents required to open an account for a partnership firm"
make eval
make test && make test-integration && make lint
```
**Environment variables** (`infra/.env.example`; `.env` overrides; process environment overrides both):

| Variable | Notes |
|---|---|
| `DATABASE_URL` | required |
| `REDIS_URL` | required by settings, unused by M1 |
| `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` | |
| `EMBEDDING_MODEL`, `EMBEDDING_MODEL_REVISION` | pinned |
| `CORPUS_DIR`, `CORPUS_MANIFEST` | |
| `TESSERACT_CMD`, `TESSDATA_DIR` | blank = PATH, then the standard install folder |
| `OCR_CACHE_DIR`, `OCR_RELIABLE_MIN_CONFIDENCE`, `OCR_EMBED_UNCERTAIN` | |

The Groq, Gemini, LangSmith and NLI variables exist but are unused until M2–M4.

**Troubleshooting:**
- "run `make migrate`" → the database schema is behind.
- "Qdrant … not ready" → `make up`.
- `make` not found on Windows → open a new terminal.
- Scanned pages `failed` → Tesseract is missing; `make check-corpus` confirms.

## 18. Git history and state
| Commit | Content |
|---|---|
| `a5fbbf7` M0: scaffold | tagged `v0.1.0-m0` |
| `b0b2b8e` docs: add M0 implementation handoff | |
| `811df69`, `3071fe5` | M1 corpus manifest, config, corpus report |
| `e71ba72` M1 Stages A–E | extraction, OCR, chunking, Qdrant indexing, Postgres persistence (migration 0002) |
| *(this commit)* M1: complete ingestion pipeline and handoff | Stage F: unified CLI/pipeline modes, retry/backoff, search/evaluate, benchmark, Windows-compatible Makefile, docs, this handoff |

The branch is `feat/m1-document-ingestion`, tracking `origin/feat/m1-document-ingestion`. `e71ba72` is on the remote. The final commit "M1: complete ingestion pipeline and handoff" is local until it is pushed. `main` and `origin/main` are at `a5fbbf7`; M1 is not merged into `main` yet.

## 19. M2 starting point
The foundation is ready: the chunk rows in Postgres (keyword source), the Qdrant index, filtered dense `search()`, the benchmark and the `evaluate` harness, and the run, retry and reconcile tooling.

Recommended order:
1. **BM25** (`rank_bm25`) built in-process from the Postgres chunk rows, filtered by metadata before scoring and rebuilt after ingestion. Add **RRF fusion** with the dense results. Compare dense, BM25 and hybrid with `make eval` against 0.825 and keep the winner. Target the m1-01/05/06 misses.
2. **Optional cross-encoder rerank** behind a flag. Measure before enabling it.
3. **Pipeline B:** the `MultimodalProvider` interface (Gemini, model id from env), Pydantic form schemas that keep `appears_on_form` separate from `mandatory_under_policy`, `form_schemas` (Alembic `0003`), versioning by file hash, and a Redis cache.
4. **Benchmark:** get the 20 labels independently reviewed, add queries (entity filters, form questions), and keep labels frozen before tuning.

Follow `CLAUDE.md`: ask before adding dependencies or changing the schema outside Alembic; keep the free tier and evidence-or-nothing rules.
