# M2 Handoff: Retrieval and Form Intelligence

> Status: **complete; approved and merged into `main` (merge commit `a288b09`, pushed to `origin/main`).** Final branch `feat/m2-g-gemini-forms`. This is an independent portfolio prototype, not an HDFC Bank or RBI system. Synthetic and public data only.

## 1. Roadmap requirements → implementation → evidence → status

Roadmap M2 (`docs/12-claude-code-playbook.md`):
- **(a)** BM25 + RRF hybrid, compared against dense and BM25, "keep whichever wins";
- **(b)** an optional cross-encoder reranker behind a flag;
- **(c)** Pipeline B with a Gemini `MultimodalProvider`, Pydantic validation, versioning and a Redis cache.

**Done when:** "hybrid ≥ dense baseline (or documented why not); form schema JSON validated + stored with page refs."

| # | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| a1 | BM25 via `rank_bm25`, built in-process from Postgres chunk rows, filtered before scoring, rebuilt after ingestion | `app/retrieval/keyword.py` (A1); `xmin`-based freshness (A2) | Unit and integration tests; filter-parity matrix | ✅ |
| a2 | RRF fusion; dense vs BM25 vs hybrid on the labelled set | `app/retrieval/{fusion,retrieve}.py`; `--mode dense\|bm25\|hybrid`; `make eval-compare` | `M2_A3_SELECTION.md`, `M2_HELDOUT_EVALUATION.md` | ✅ |
| a3 | Keep the winner (or document why not) | **Dense stays the default.** The dev-selected hybrid C6 did not generalise to the frozen held-out set | `M2_RETRIEVAL_OPERATIONAL_DECISION.md`; docs/10 "M2 outcome" | ✅ (the "documented why not" branch) |
| b | Optional cross-encoder rerank behind a flag | `app/retrieval/rerank.py`, MiniLM pinned, CPU, lazy; `RERANK_ENABLED=false`; `--rerank` | One diagnostic (B2, reused dev set; not a validation); failures labelled `degraded` | ✅, off by default |
| c1 | `MultimodalProvider` (Gemini) | `app/llm/{multimodal,gemini,fallback,fake}.py` | 5 approved forms extracted live (§3); 32 + 7 + 13 provider tests | ✅ |
| c2 | Pydantic form-schema validation | `app/schemas/forms.py` (`extra="forbid"`; `FormExtraction` output model has no `mandatory_under_policy`); one re-ask, never repaired | Validation tests; database guards 0004 | ✅ |
| c3 | Versioning | `form_schemas` identity (document, method, version); insert / unchanged / conflict, never overwrite; Gemini v1 and v2 rows coexist; `forms versions` | Migrations 0003–0005 up/down/up tests; v1/v2 immutability tests; all stored rows hash-identical | ✅ |
| c4 | Redis cache | `app/core/cache.py`, `load_form_schema_cached` (G4); Postgres decides the current row and validates every entry | 14 integration + 7 unit tests; live hit/miss/fallback (§4) | ✅ |
| ✓ | "Form schema JSON validated and stored with page refs" | Every field carries a physical page; Gemini fields carry a corroborating chunk id when found; per-page provenance (PNG SHA-256, model, outcome) | `forms show`; integration round-trips | ✅ |

**Removed from M2:** the C3 "evidence pack" from the reduced plan. It is not an M2 roadmap item; its role moves to M3's retrieval and form nodes. See `M2_REMAINING_PLAN.md`.

## 2. Retrieval results (unchanged since review; small single-annotator sets)

| Set | Configuration | Recall@5 | Hit@5 | MRR@5 | p50 / p95 ms |
|---|---|---|---|---|---|
| Dev (20 queries) | C0 dense | 0.825 | 0.900 | 0.775 | 34 / 44 |
| Dev | C6 hybrid (stopwords, k=20), selected | 0.900 | 0.950 | 0.735 | 49 / 58 |
| Held-out (20, frozen, run once) | C0 dense | **0.900** | 0.900 | 0.850 | |
| Held-out | C6 hybrid | **0.875** | 0.950 | 0.867 | |
| Dev (reused, diagnostic only) | dense + MiniLM rerank | 0.950 | 1.000 | 0.796 | 1131 / 1447 (≈26× slower) |

- **Held-out CI:** the 95% CI for ΔRecall@5 is [−0.125, +0.050]. No configuration is statistically distinguishable from dense.
- **Regression gate:** `make check-dense-golden` is green throughout M2.

## 3. Form extraction results

- **Deterministic (`m1_chunks` v1, 6 documents):** unchanged and byte-identical since C2. `make forms` reports `6 unchanged`.
- **Gemini (`gemini_multimodal`, five approved public blank forms):**
  - **Models actually used (from each row's per-page provenance):**
    - **Primary `gemini-3.6-flash`:** the repo-root `.env` sets this `MULTIMODAL_MODEL`, not the `gemini-3.8-flash` verified in G2; both are listed as stable, image-input, structured-output, free-tier models (docs/02). It answered only the dormant re-KYC form (2 pages) and was exhausted by 503/429 on every other form.
    - **Fallback `gemini-3.5-flash-lite`:** set per process for every live run, overriding the `.env` value `gemini-3.5-flash`, which was never used or verified. It answered **25 of the 27** stored pages (17 of the 19 pages in the latest records).
  - **Latest record per form:**

| Form | Latest stored | Pages | Fields (corroborated / uncorroborated) | `*` shown | Older rows kept |
|---|---|---|---|---|---|
| hdfc_non_individual_account_opening (scanned) | **v2** | 2/2 | 120 (76 / 44) | 19 | v1: 35 fields, partial |
| hdfc_nri_account_opening | **v2** | 6/6 | 362 (332 / 30) | 50 | v1: 296 fields, partial |
| hdfc_resident_to_nro_conversion | **v2** | 6/6 | 298 (267 / 31) | 45 | – |
| hdfc_nri_rekyc (scanned) | **v1** (complete, 3/3) | 3/3 | 61 (47 / 14) | 3 | v2 not extracted |
| hdfc_resident_rekyc_dormant | **v1** (complete, 2/2) | 2/2 | 55 (37 / 18) | 0 | v2 not extracted |

- **Completeness:** every latest record is complete (no `partial_extraction`, no refused or invalid page).
- **Versions are not interchangeable:**
  - **v1** invalidated pages with repeated labels; **v2** keeps them as separate fields, flagged `repeated_labels`.
  - Two forms have only v1 records. They had no repeated-label failures under v1, but they were not re-extracted under v2.
  - `forms versions` never claims a version without a stored row.
- **Live-run history** (full detail in docs/04):
  - **Run 1:** 503/429, nothing stored.
  - **Rerun:** 3 of 5 forms stored. One form was refused with `RECITATION` and one got 503.
  - **RECITATION diagnostic:** it did not recur.
  - **v2 targeted runs:** Resident-to-NRO, then the two partial forms.
  - Secret scans were clean after every run.

## 4. Validation (M2 D, 2026-11-11, `TESSDATA_DIR` unset, `GEMINI_API_KEY` unset for the suites)

| Check | Result |
|---|---|
| Unit suite | **529 passed** |
| Integration suite | **199 passed** |
| `make lint` (ruff, black, strict mypy) | clean, 73 source files |
| `make check-schema` | no drift (head `0005_m2_gemini_method`) |
| `make check-dense-golden` | passed |
| `make forms` | 6 `m1_chunks` schemas unchanged; all 13 stored `form_schemas` rows re-hash identically |
| `make ingest` (run twice) | content no-op: document content (excluding `updated_at`), pages, chunks, exclusions and form schemas identical. Only the M1 run log (`ingestion_runs`) and `documents.updated_at` change, by design. Reconcile is consistent (429 eligible chunks in Postgres, Qdrant and BM25) |
| Redis cache, live on the dev stack | miss then hit with identical output; v1 and v2 under separate keys; Redis unreachable → `bypassed`, identical output from Postgres |
| Secret scan | GEMINI key not found in run logs, `form_schemas`, `ingestion_runs` or Redis values; cached values hold only `{format, row_id, schema_sha256, schema_json}`; the docs contain no key |

## 5. Data-use and safety constraints (enforced in code)

- **What is sent:** only the five public **blank** forms in `data/raw_pdfs/forms_approved.yaml`. Each needs the attestation, an approver and date, and a SHA-256 matching the registry, manifest and database. Refusals happen before rendering; the NetBanking reference is never sent.
- **Live gates:**
  - live calls stay off unless `GEMINI_LIVE_ENABLED=true` and `GEMINI_DATA_TERMS_VERIFIED_ON` are set. Every live run here set them for that process only, and `.env` stayed off.
  - **Free-tier terms (verified 2026-11-10):** inputs may be used to improve Google's products and may be reviewed by humans. Hence no customer documents, filled forms or PII (docs/02).
- **Key handling:** the key is a `SecretStr`, redacted from SDK errors and never chained into exceptions.
- **Retry bounds:** 60 s timeout, 5 attempts per model at 2→4→8→16 s (an owner-approved override of the default of 2, Gemini transport only), plus an RPM limiter. The fallback model is used only after transient exhaustion.
- **RECITATION:** never retried or sent to another model. The diagnostics record finish reason, message, citations, and the partial output's length and SHA-256 only. The page is OCR'd locally (M1 Tesseract and cache) into a separate `requires_human_review` record that never becomes fields.
- **No policy obligations from forms:** `mandatory_under_policy` is always null. The database guards (0004) reject any other value, including malformed or misplaced JSON.

## 6. Known limitations

- **Small benchmarks:** retrieval sets are 20 queries each, single-annotator. Dense and hybrid are not statistically distinguishable. BM25 ignores negation.
- **Reranker:** about 26× slower and measured only on the reused dev set, so it stays off.
- **Form accuracy is not measured:** there is no labelled ground truth. Corroboration only shows a label appears in the stored page text; uncorroborated labels are often fuller printed wording, not necessarily errors.
- **Model configuration drift:** `.env` (`gemini-3.6-flash` / `gemini-3.5-flash`) differs from the verified `infra/.env.example` (`gemini-3.8-flash` / `gemini-3.5-flash-lite`). Rows record the model that actually answered. Align `.env` before further live runs.
- **Gemini runs on the free tier:**
  - the real per-project limits were not visible; 10 RPM is conservative;
  - the primary model was overloaded in every run, so the fallback produced most pages;
  - output is non-deterministic, so re-extraction needs a version bump.
- **Partial v2 coverage:** two forms have only v1 Gemini records.
- **OCR fallback untested live:** the RECITATION→OCR path is covered by tests with real local Tesseract, but RECITATION has not recurred live.
- **No page reuse across extractor versions:** each version is re-extracted whole.
- **No layout validation:** checkbox, signature and option extraction depend on the model.
- **The cache adds little:** each read still asks Postgres for the current row's id and hash (a small query). The cache saves the JSON payload transfer and validation only.
- **Windows console:** unencodable characters print as `?`; `--json` is lossless.

## 7. Demo path

See README "Getting started", steps 2–5:
- `make up migrate check-schema check-corpus ingest`;
- `make forms`;
- optional live Gemini extraction (opt-in gates);
- `forms versions`;
- `forms show hdfc_nri_account_opening --method gemini_multimodal`.

## 8. Branch ancestry (linear; no merges since `main`)

```
main  d8d8b7b  Merge M1: document ingestion foundation
 └─ feat/m2-kyc-intelligence      37edf48 … 29ced62  (plan, Stage 0, A1–A3, held-out, decision, B1, B2, C1)
     └─ feat/m2-c2-form-persistence  41db683 … 9db36e7  (C2, 0004 guards)
         └─ feat/m2-g-gemini-forms   92d81e5 … (D commit = this branch tip)  (G1–G4, follow-ups, D)
```

`feat/m2-g-gemini-forms` contains every M2 commit. It was merged into `main` as `a288b09` ("Merge M2: KYC intelligence and Gemini forms") and pushed to `origin/main`.

## 9. Next: M3

M3 is the LangGraph end-to-end workflow, per the roadmap. Its approved plan is `docs/milestones/M3_PLAN.md`.
