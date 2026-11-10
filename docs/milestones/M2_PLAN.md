# M2 Plan: Hybrid Retrieval, Optional Reranking, Pipeline B (Multimodal Forms)

> Revision 2 (incorporates review corrections 1–7). Once approved, it is saved verbatim as `docs/milestones/M2_PLAN.md`, the first and only file written before Stage 0. Educational portfolio prototype; not an HDFC Bank or RBI system.

## Context
M1 (merged into `main`, `d8d8b7b`) gives:
- Pipeline A ingestion into Postgres (456 chunks, the source of truth);
- a derived Qdrant index (429 eligible points);
- filtered dense search with citations and parent expansion;
- reconcile and retry tooling;
- a 20-query benchmark (dense: **Recall@5 0.825, Hit@5 0.90, MRR@5 0.775**).

M2 adds:
- **A:** real BM25 + RRF hybrid retrieval;
- **B:** an optional cross-encoder reranker;
- **C:** Pipeline B, multimodal form-schema extraction behind a `MultimodalProvider`.

Every change is measured, and M1 must keep working unchanged.

**Decisions already made:**
- **Evaluation discipline.** The M1 set (20 queries) is the **development set**. A new ~20-query **held-out set** is a frozen final evaluation set: I draft it, you review it, and it is frozen by hash before any hybrid run. It is never used to choose or tune anything (§6).
- **Gemini.** Interface + fake provider + an **opt-in live** `GeminiProvider`. Tests never touch the network.
- **Dependencies approved:** `rank_bm25` and `google-genai`. `google-genai` is imported only inside `app/llm/gemini.py`, so the app runs with no Gemini configuration.
- **Not approved yet: the reranker model download.** Workstream B is built and unit-tested with a fake. Its real evaluation (Stage B2) is gated on your approval of a specific model.

---

## 1. Current M1 architecture (verified in code)
| Concern | Implementation |
|---|---|
| Orchestration | `app/ingestion/pipeline.py` `DocumentIngestion`: A–C (`prepare.py`) → one Postgres txn (`db/persistence.py`) → index from committed rows (`retrieval/indexing.py`) → per-document point check → status. Modes `FULL / PERSIST / INDEX`. |
| CLI | `app/ingestion/cli.py` (argparse): `run/ingest, persist, index [--from-db], retry, reconcile [--repair [--delete-unknown]], runs, search, evaluate, validate-manifest, check-ocr`. Exit codes 0/1/2. The Makefile wraps each. |
| Schema | `0001_m1_corpus`, `0002_m1_persistence`. Tables: `documents` (one version per `document_key`), `document_pages`, `chunks` (content, `content_sha256`, eligibility flags, `metadata` JSONB, same-document parent FK), `document_exclusions`, `ingestion_runs`. |
| Row ↔ payload | `app/db/corpus.py` `chunk_metadata_from_rows(chunk, doc)` rebuilds the exact `ChunkMetadata`. `ChunkMetadata.to_qdrant_payload()` is the hit payload. |
| Dense retrieval | `app/retrieval/search.py` `search(query, embedder, store, top_k, filters)`. `SearchFilters` → `build_filter()` (Qdrant only) covers eligibility (primary+supporting by default), `mandatory_evidence_only`, banks, document keys, and entity terms via `expand_entity_terms`. Returns `SearchHit(rank, score, chunk_id, payload)` with `.citation`. |
| Embedding input | `embeddings.embedding_input(meta)` = `context + "\n" + content`. Queries get the BGE instruction. The model loads lazily. |
| Qdrant | `retrieval/qdrant_store.py` `ChunkStore`, with bounded backoff through `app/core/retry.call_with_backoff` (sync; 2 retries). |
| Parent expansion | `app/db/repository.py` `evidence_context(session, chunk_ids)`. |
| Reconcile | `app/retrieval/reconcile.py`: bidirectional Postgres↔Qdrant check. `--repair` re-indexes from Postgres. Exit 0 = consistent, 1 = not. |
| Evaluation | `app/retrieval/evaluation.py`: `Target.matches(payload)`, `score_case`, `evaluate()` (dense-only, hard-wired to `search`), `render_markdown`, `report_json`. Dataset `data/eval/m1_retrieval.jsonl` (20 cases, 28 targets). |
| Config / Redis | `app/config.py` `Settings`. `REDIS_URL` is required by settings but used only by `/health`. `GEMINI_API_KEY` and `MULTIMODAL_MODEL` exist; `MULTIMODAL_MODEL` is blank. |
| Not present | No BM25, fusion, reranker, `llm/` code, Redis cache, form schemas or forms tables. `rank_bm25` and `google-genai` are not installed. `sentence-transformers 6.2.0` (which includes `CrossEncoder`) is installed. |

## 2. Workstream → module map
| Workstream | Reuse | New |
|---|---|---|
| A: BM25 | `Chunk`/`Document` ORM, `chunk_metadata_from_rows`, `embedding_input`, `expand_entity_terms`, `SearchFilters`, `SearchHit`, `create_sync_sessionmaker` | `retrieval/keyword.py` (tokenizer, `KeywordIndex`, `KeywordIndexManager`) |
| A: fusion / modes | `search()` (dense, unchanged), `evidence_context` | `retrieval/fusion.py` (RRF), `retrieval/retrieve.py` (`RetrievalMode`, `retrieve()`), `SearchFilters.matches(payload)` |
| A: benchmark | `evaluation.py` matching and scoring (semantics unchanged) | a pluggable retriever in `evaluate()`; the dev comparison report; the held-out final report; latency timings; `data/eval/m2_heldout.jsonl` |
| B: rerank | `SearchHit`, the lazy-load pattern of `SentenceTransformerEmbedder` | `retrieval/rerank.py` (`Reranker` protocol, `NoopReranker`, `CrossEncoderReranker`) |
| C: Pipeline B | `ManifestEntry` + SHA-256 validation, PyMuPDF rendering (`pages.py` `get_pixmap` pattern), `documents` / `document_pages` / `chunks` rows (read-only), `Settings` | `app/llm/{multimodal,gemini,fake}.py`, `app/schemas/forms.py`, `app/forms/{pipeline,approval}.py`, `app/db/forms.py`, `app/core/cache.py`, migration `0003_m2_form_schemas`, `data/raw_pdfs/forms_approved.yaml`, CLI `forms …` |

## 3. Dependencies, missing abstractions, risks
**Dependencies:**
- `rank_bm25==0.2.2`: Apache-2.0, pure Python, depends on numpy (already locked); last release 2022. Mitigation: pin it, hide it behind `KeywordIndex`, and add a formula-check test so a ~40-line in-house BM25Okapi could replace it if needed.
- `google-genai`: pin it at implementation after checking its version and licence; import it only in `app/llm/gemini.py`.
- Nothing else. Timing uses `time.perf_counter`; memory uses `tracemalloc` plus model parameter counts.

**Missing abstractions:**
- a Python filter predicate equivalent to `build_filter`;
- a retriever interface for evaluation;
- bulk Postgres → payload loading;
- an async retry helper (`call_with_backoff` is sync);
- an optional Redis cache wrapper;
- the `llm/` package;
- a public-form approval check.

**Architectural risks and mitigations:**
1. **Filter drift** between Qdrant and Python filtering → one definition (`SearchFilters`) plus a live parity test over a filter matrix.
2. **Payload drift** between Postgres-built and Qdrant hits → a parity test on all 429 chunks; fusion keys on `chunk_id` only.
3. **Stale keyword index** in a long-lived process (the M3 API) → a fingerprint check on every query (§7).
4. **BM25 on noisy OCR** (the fold-out form) → measure it, and report hits by `text_reliability`.
5. **Overfitting the small benchmark** → the dev/held-out discipline in §6.
6. **Gemini churn:** model IDs, quota, data terms and the structured-output schema subset → env model ID, a pre-live checklist (§9), simple schemas and Pydantic validation.
7. **Extraction treated as fact** → separate extracted and verified statuses; confidence treated as a heuristic; form output never enters `chunks` or Qdrant in M2.
8. **Sync/async mix** → the provider is async (docs/02); the CLI uses `asyncio.run`; retrieval stays sync until M3.

## 4. Implementation order and rationale
**0 → A1 → A2 → A3 → B1 → (B2 after model approval) → C1 → C2 → C3 → D**
- **A first:** M3's retrieval node depends on it. It reuses existing assets, adds one small dependency and is fully measurable.
- **B after A:** a reranker can only reorder candidates, so its ceiling is the selected mode's dev Recall@20, known only after A3.
- **C last:** it is independent but riskiest (external API, new table, new dependency). Isolated modules and a new command keep it from destabilising M1 or retrieval.
- One stage per commit. Each stage ends with tests, lint and a doc update.

## 5. Stages and acceptance criteria
**Stage 0: baseline freeze** (no feature code)
- Capture a dense golden snapshot: top-5 chunk ids and order per M1 query, in `backend/tests/fixtures/m1_dense_golden.json`.
- Draft `data/eval/m2_heldout.jsonl` (~20 queries): exact-term (Form 60, CKYCR, V-CIP), paraphrase, entity-filtered, form-specific and FAQ-vs-Directions. Labels come from the source text, and every target is checked against stored chunks. **Labels are drafted from the source documents only; no retrieval mode is run on these queries while drafting.**
- **You review the set.** Then freeze it: record its SHA-256 in `docs/10` and add a test that fails if the file changes.
- ✅ Done when:
  - every held-out target resolves to an eligible chunk (test);
  - the hash test passes;
  - dense on dev still gives 0.825 / 0.90 / 0.775.

**Stage A1: BM25 keyword leg (vertical slice, §12)**
- `keyword.py`; `SearchFilters.matches(payload)`; `search --mode bm25`; `evaluate --mode bm25` on **dev**.
- ✅ Done when:
  - filter parity holds on ≥ 12 filter combinations (live test);
  - payload parity holds on 429/429;
  - the BM25 corpus is 429 = Postgres eligible = Qdrant;
  - hits carry `document_key`, page, paragraph and citation, and `--expand` works;
  - unit tests cover the tokenizer, filter-before-scoring and empty results.
- **Measured target (not a correctness gate):** index build < 1 s on this laptop. It is reported; missing it is documented, not a failure.

**Stage A2: RRF and retrieval modes**
- `fusion.py` and `retrieve.py` with `RetrievalMode {dense, bm25, hybrid}`.
- Explicit degradation flags.
- `search --mode`; `RETRIEVAL_MODE` setting (default `dense` until A3 decides).
- ✅ Done when:
  - RRF unit tests pass (hand-computed fusion, ties, one-sided hits, deterministic order);
  - dense mode is identical to the golden snapshot;
  - a failed leg yields a flagged degraded result, never a silent one.

**Stage A3: selection on dev, then one final held-out run**
1. On **dev only**, compare the declared candidates (§6): dense; BM25 × 2 tokenizers; hybrid × 2 tokenizers × the RRF grid.
2. Apply the dev decision rule and **select one configuration**. Record the selection (config, dev metrics, reasons) in a commit **before** the held-out run.
3. Run the held-out set **once** for the selected configuration, with the M1 dense baseline scored in the same run as a fixed reference. The reference is reported for context only and never used to choose.
4. Report both, and set the `RETRIEVAL_MODE` default to the dev-selected mode.
- ✅ Done when:
  - the dev comparison report and the single held-out report are written;
  - the selection commit precedes the held-out report;
  - docs/10 and the handoff record the numbers and limitations.
- A disappointing held-out result is reported as is. It does **not** reopen tuning.

**Stage B1: reranker plumbing**
- `rerank.py` (protocol, noop, lazy `CrossEncoderReranker`).
- `RERANK_ENABLED=false`; `search/evaluate --rerank`.
- ✅ Done when:
  - with reranking disabled, the ranking is identical to the A3-selected mode (test);
  - a fake reranker reorders hits and keeps every payload field;
  - a load or inference failure → original ranking + a `rerank` degradation flag;
  - `CrossEncoder` is never imported when reranking is disabled (test).

**Stage B2: reranker evaluation** *(gated on your approval of the model download)*
- Same discipline as A3:
  - choose the model, candidate depth N and on/off on **dev only** (§8);
  - commit the selection;
  - run held-out **once** for the selected setup, with the A3-selected configuration without reranking as the fixed reference.
- ✅ Done when: the dev comparison and the single held-out report are written; latency, cold load time, size and parameters are reported; the default is set per the dev rule.

**Stage C1: contracts and storage**
- `app/llm/multimodal.py`; `app/llm/fake.py`; `app/schemas/forms.py`.
- Migration `0003_m2_form_schemas` (additive); `app/db/forms.py`.
- ✅ Done when:
  - upgrade → downgrade → upgrade is clean on a throwaway database, M1 rows are untouched, and `make check-schema` is clean;
  - validation tests cover malformed JSON, extra keys, out-of-range pages and duplicate fields → invalid;
  - **unknown, ambiguous or unextractable entity applicability → still valid**, with the correct status (§9);
  - low confidence → `needs_review`, not invalid;
  - `mandatory_under_policy` cannot be set by extraction (test).

**Stage C2: pipeline, approval check, cache and CLI with the fake provider**
- `app/forms/pipeline.py`: approval check → hash-verified render → provider → validate (one retry on invalid output) → corroborate → Postgres → optional cache.
- `app/forms/approval.py` and `data/raw_pdfs/forms_approved.yaml`.
- `app/core/cache.py`; CLI `forms extract | list | show`; `make forms`.
- ✅ Done when:
  - an end-to-end integration test passes with the fake provider;
  - a second run makes 0 provider calls (Postgres hit; and a Redis hit when Redis is up);
  - **with Redis stopped, the results, stored rows and exit codes are identical** (test);
  - `--force` or a version change re-extracts and keeps the old row;
  - a document not on the approved list is refused before rendering;
  - `make ingest` on unchanged data still writes 0 rows.

**Stage C3: opt-in Gemini provider**
- `app/llm/gemini.py`: lazy `google-genai`, timeout, ≤ 2 retries with backoff, in-process RPM limiter, structured output.
- **Pre-live checklist (§9)** completed and recorded first.
- One live run on **one** approved form (`hdfc_resident_rekyc_dormant`: 2 native-text pages, the cheapest).
- ✅ Done when:
  - the app imports and all tests pass with `GEMINI_API_KEY` unset;
  - the live run stores a validated schema with usage and latency;
  - the field corroboration rate against M1 page text is reported;
  - a secret scan of the logs, rows, cache values and reports finds no key.

**Stage D: documentation and handoff**
- Update `docs/01`, `02`, `04`, `08`, `10`, the README and the Makefile help.
- Write `docs/milestones/M2_HANDOFF.md`.
- ✅ Done when the CLAUDE.md definition of done holds.

## 6. Retrieval benchmark methodology
- **Roles:**
  - **Development set** = `m1_retrieval.jsonl` (20). *All* choices are made on it: mode, tokenizer, RRF `k` and depth, reranker model, depth and on/off.
  - **Held-out set** = `m2_heldout.jsonl` (~20, frozen by hash). It is used **only** to report the already-selected configuration, once per workstream:
    - at most **two exposures in M2**: A3 (retrieval), then B2 (reranker);
    - each run includes one fixed, named reference (the M1 dense baseline, or the A3 config without rerank) for context;
    - **held-out metrics never choose between configurations or tune parameters.**
- **Label integrity:** labels in either file are never edited after results are seen. A discovered label error is logged and fixed in a new versioned file, and the original numbers stay reported.
- **Declared candidate space (fixed before any run; small to limit dev overfitting):**
  - BM25Okapi `k1=1.5`, `b=0.75` (not tuned);
  - BM25 text = `embedding_input` (the same text as dense);
  - tokenizer ∈ {plain, plain + stopwords};
  - RRF `k` ∈ {60, 20}; candidate depth per leg ∈ {50}; top-k 5;
  - that is 1 dense + 2 BM25 + 4 hybrid = **7 dev configurations**.
- **Filters:** production defaults plus each case's `entity_terms`, the same for every mode.
- **Metrics:**
  - Recall@5, Hit@5 and MRR@5 (definitions unchanged);
  - Recall@20 (candidate ceiling, dev only);
  - per-query wins and losses against dense;
  - a paired bootstrap 95% CI of ΔRecall@5 (stdlib `random`, fixed seed, 10k resamples);
  - latency: 1 warm-up pass + 3 timed passes, p50/p95 per stage (embed, Qdrant, BM25, fusion, rerank); index build and model load reported separately.
- **Dev decision rule (pre-registered):**
  - a non-dense configuration is selected only if, on dev, mean Recall@5 improves on dense by ≥ 0.05 (one query-equivalent) **and** Hit@5 does not drop;
  - among qualifying configurations: highest Recall@5, then MRR@5, then the simpler or faster one;
  - if none qualifies, dense stays.
- **Held-out reporting:** report the selected configuration's metrics, the reference's metrics, the paired CI and per-query outcomes. If held-out is worse than the reference, say so plainly in docs/10 and the handoff; the dev-selected default stands unless you decide otherwise. **No re-tuning on held-out results.**
- **Limitations stated in every report:** one query = 0.05 Recall@5; a single annotator (the implementing assistant); wide CIs; "no measurable difference" is a valid outcome. The M1 misses (m1-01/05/06/19/20) are analysed diagnostically only, and no parameter is changed for a specific query.

## 7. BM25 index lifecycle, consistency and recovery
- **Corpus:**
  - every `embedding_eligible` chunk (single and child, every eligibility value) joined to `documents`, the same 429 Qdrant holds;
  - parents are excluded, and children expand through `evidence_context` (unchanged);
  - payloads come from `chunk_metadata_from_rows(...).to_qdrant_payload()`.
- **Tokenizer** (`TOKENIZER_VERSION=1`):
  - lowercase over the already-NFKC content;
  - regex `[a-z0-9]+(?:[./-][a-z0-9]+)*` keeps "60", "v-cip" and section numbers;
  - an optional inline stopword list; no stemming;
  - the same function for queries and documents.
- **Filter before scoring:**
  - `SearchFilters.matches(payload)` gives the candidate ids first;
  - only those are scored, through `BM25Okapi.get_batch_scores(query, ids)`;
  - IDF and average length are global over the 429 chunks, so filters restrict candidates and don't shift statistics. This is documented;
  - zero-score documents are dropped.
  - Eligibility and authority are filters, never ranking signals.
- **Fingerprint:** SHA-256 over the sorted `(chunk_id, content_sha256, context, eligibility, can_support_mandatory, bank, document_key, entity_types, residency_statuses)` plus the tokenizer version; one lightweight SELECT.
- **Lifecycle:**
  - built lazily;
  - `KeywordIndexManager.get()` re-checks the fingerprint on every query, and rebuilds and swaps atomically under a lock when it changed;
  - `invalidate()` hook after a successful Postgres commit;
  - CLI processes always build fresh.
- **Recovery:**
  - if the fingerprint read or rebuild fails, the keyword leg is unavailable (**never stale**): hybrid → dense with `degraded=["keyword"]`; `--mode bm25` → exit 2;
  - Qdrant down in hybrid → keyword-only with `degraded=["dense"]`;
  - both down → error.
- **Reconciliation:**
  - `reconcile` may print one **informational** line: the keyword corpus count against the Postgres eligible and Qdrant counts;
  - it doesn't affect consistency, repair or exit codes;
  - if the count can't be computed, a note is printed;
  - `reconcile`, `reconcile --repair` and `--delete-unknown` keep their exact M1 semantics and exit codes (regression test).
- **RRF:**
  - `score = Σ 1/(k + rank_leg)` over the legs containing the chunk;
  - tie-break: fused score ↓, best single-leg rank ↑, `chunk_id`;
  - hits keep the payload and add `dense_rank`, `keyword_rank` and `fused_score`.

## 8. Reranker configuration, fallback and evaluation
- **Settings:**
  - `RERANK_ENABLED=false`;
  - `RERANK_MODEL` and `RERANK_MODEL_REVISION` (a pinned HF commit);
  - `RERANK_CANDIDATES`;
  - `RERANK_TIMEOUT_S`;
  - `RERANK_MAX_LENGTH`.
- **Candidates to verify at implementation** (licence, revision, size):
  - `cross-encoder/ms-marco-MiniLM-L-6-v2` (~22M params, ~90 MB): **recommended first, CPU-cheap**;
  - `BAAI/bge-reranker-base` (~278M, ~1.1 GB): the quality comparison.
  - `bge-reranker-v2-m3` (568M) is excluded as too slow for CPU.
  - **Download needs your approval.**
- **Behaviour:**
  - input = (query, `embedding_input`);
  - truncation affects scoring only and is counted;
  - top-N candidates of the A3-selected mode → rerank → top-k;
  - the payload is unchanged; hits add `rerank_score` and `pre_rerank_rank`.
- **Fallback:** a load error, an inference error or the timeout → original order + `degraded=["rerank"]`. Search never fails because of the reranker.
- **Dev-only selection:**
  - declared space: model ∈ {approved model(s)}, N ∈ {20};
  - reranking is enabled by default only if, **on dev**, ΔRecall@5 ≥ +0.05, Hit@5 doesn't drop, **and** added p95 latency ≤ 1.0 s/query on this laptop (a measured budget);
  - otherwise it stays opt-in.
- The held-out run follows (once), reported honestly, with no tuning against it.

## 9. Pipeline B design
**Interface** (`app/llm/multimodal.py`; refines docs/02):
```python
class PageImage(BaseModel): document_key: str; page_number: int; region: str | None; png_sha256: str; dpi: int
class MultimodalRequest(BaseModel): prompt_id: str; prompt_version: str; pages: list[PageImage]
class MultimodalResult(BaseModel, Generic[T]): parsed: T | None; raw_text_sha256: str; validation_errors: list[str]
    provider: str; model_id: str; usage: TokenUsage | None; latency_ms: int; finish_reason: str | None
class MultimodalProvider(Protocol):
    name: str; model_id: str
    async def analyze_pages(self, images: list[bytes], request: MultimodalRequest, schema: type[T]) -> MultimodalResult[T]: ...
```
- Errors: `ProviderUnavailable`, `ProviderRateLimited`, `ProviderRefused`, `ProviderTimeout`.
- `FakeMultimodalProvider` scripts responses: valid, malformed, partial, low confidence, unknown entity, refusal.

**Gemini boundaries** (`app/llm/gemini.py`):
- `google-genai` is imported here only;
- the model comes from `MULTIMODAL_MODEL` and the key from `GEMINI_API_KEY` (`SecretStr`);
- JSON structured output with a simplified schema, then Pydantic validation;
- 60 s timeout, ≤ 2 retries with backoff on 429/5xx/timeouts (async helper), and an in-process limiter at `RATE_LIMIT_GEMINI_RPM`;
- the system instruction says that image text is data, the task is extraction only, and no legal, mandatory or applicability judgment is wanted.

**Data handling and the pre-live checklist** (enforced in code where possible):
1. **Terms first.** Before any live call, the current Google Gemini API terms and data-use policy (free tier: whether inputs may be used to improve products) are read and recorded in `docs/02` with the date. The live provider refuses to run unless `GEMINI_DATA_TERMS_VERIFIED_ON` (a date) is set, and the CLI prints that date before calling.
2. **Approved public blank forms only.** `forms extract --provider gemini` sends a document only if it is:
   - in the manifest;
   - `document_type=form`;
   - SHA-256-verified against both the manifest and `documents.sha256`;
   - **listed in `data/raw_pdfs/forms_approved.yaml`**, a tracked file with key, sha256, "public blank form" attestation, source and reviewer date, which you approve.
   
   Anything else, including any future upload path, is refused before rendering.
3. **No real customer KYC data or PII**, ever. Only blank public forms. Filled forms, ID documents and customer images are out of scope, and the approval file attests this.
4. **Secrets:** the API key exists only in `.env` and the process env. It never appears in Git, logs, exceptions, LangSmith, database rows (`usage` and errors are scrubbed) or cache values. A test scans the stored rows, cache values and captured logs for the key.

**Pydantic schemas** (`app/schemas/forms.py`, `extra="forbid"`):
- **Information status:** a shared enum `InfoStatus = stated | not_stated | ambiguous | unextractable`:
  - `not_stated`: the form shows no such information (missing);
  - `ambiguous`: present, but readable in more than one way; requires `ambiguity_note`;
  - `unextractable`: the region exists but couldn't be read (illegible, cropped, low-quality scan); requires a reason.
- **`EntityApplicability`:** `status: InfoStatus`, `entity_types: list[EntityType]` (non-empty only when `stated`), `unmapped_terms: list[str]` (wording that doesn't map to the taxonomy, kept verbatim) and `evidence_text`, plus `page_number`. **Unknown or unclear applicability is a valid result**: it is never invalidated, defaulted or guessed. An unmapped term goes to `unmapped_terms` with status `ambiguous`, never forced into a taxonomy value.
- **`FormField`:** `label`, `field_type` (text|date|number|checkbox|signature|photo|table|other), `page_number`, `region`, `required_as_shown` (`InfoStatus` + bool | None; true only when the form itself marks it), `required_marker_text`, `options[]`, `evidence_text`, `model_confidence` (0–1 | None) and `ambiguity_note`.
- **`FormExtraction`:** `form_title` (with status), `sections[]`, `declarations[]`, `annexure_references[]`, `entity_applicability`, `extraction_notes`.
- **The LLM schema has no `mandatory_under_policy`.** The stored model adds `mandatory_under_policy: None`, settable only by the M4 rules layer.
- **Invalid only for structural defects:** malformed JSON, extra keys, a page outside the sent pages, empty labels, confidence out of range, duplicate (page, section, label), or a status that contradicts its payload (for example `stated` with no value).

**Model confidence:** `model_confidence` is the provider's **self-reported, uncalibrated heuristic**. It is stored and used only to route items to `needs_review` below `FORM_MIN_CONFIDENCE`. It **cannot** establish factual correctness, source authority, regulatory applicability or evidence eligibility, and high confidence never upgrades any status. Documented in the schema docstring and docs/04.

**Extracted vs verified:** each field and the applicability block carry `verification_status`:
- `extracted`: a model claim only;
- `text_corroborated`: the label or wording was found deterministically in that page's M1 text or OCR from Postgres (normalised match; threshold fixed before C3);
- `not_found_in_text`;
- `needs_review`: low model confidence, `ambiguous`/`unextractable`, or an OCR-uncertain page;
- `human_verified` is reserved for M4.

Corroboration shows that the text **appears on the form**, nothing more. Schema-level statuses are `validation_status` (valid|partial|invalid) and `review_status` (unreviewed|approved|rejected; default unreviewed). **Nothing from `form_schemas` feeds `chunks`, Qdrant, evidence eligibility or MANDATORY in M2.** Source authority and eligibility come only from the M1 manifest and document rows.

**Failure handling:**
- invalid output → one retry with the validation errors, then a stored row with `validation_status=invalid`, the errors and a raw-response hash (raw text capped at 64 KB);
- missing pages → `partial`;
- refusal or safety block → `invalid` with the reason;
- a field on a page that wasn't sent → dropped and recorded.

**Versioning** (`0003_m2_form_schemas`, additive):
- columns: `id`, `document_id` FK → documents (CASCADE), `form_id` (= `document_key`), `document_sha256`, `form_version` (= documents.version, nullable), `schema_version`, `prompt_version`, `provider`, `model_id`, `render_dpi`, `page_numbers int[]`, `page_image_sha256 jsonb`, `entity_applicability_status` (CHECK InfoStatus), `entity_applicability text[]` (CHECK ⊆ taxonomy; may be empty), `extracted_json jsonb`, `validation_status` / `review_status` (CHECK), `validation_errors jsonb`, `usage jsonb`, `extracted_at`, `created_at`;
- **UNIQUE**(`document_id`, `schema_version`, `prompt_version`, `provider`, `model_id`, `render_dpi`).
- A new PDF means a new sha256 and a new document id: never merged. A new prompt, schema or model version means a new row, and old rows are kept. M1's one-version-per-key rule on `documents` is unchanged.

**Redis: optional cache, never a correctness dependency** (`app/core/cache.py`):
- **Postgres is the only system of record.** Every decision (skip, re-extract, the `--retry-invalid` quota guard, review status) reads Postgres. Redis only saves a Postgres read.
- key `cache:form:{document_sha256}:{extraction_version_hash}` (docs/08), where the hash = sha256(schema|prompt|provider|model|dpi); TTL 7 d; the value = validated JSON + `cache_format_version` + the Postgres row id.
- read order: Redis → Postgres → provider. A cached value is used only if it parses, validates and its row id still exists in Postgres with the same status; otherwise miss + delete. Write-through happens only after the Postgres commit.
- **Invalidation:** any version input changes the key; `--force` and review-status changes delete the key.
- Redis unreachable, timing out (short timeout) or erroring → bypass with a warning. Behaviour, rows and exit codes are identical to the Redis-up path (C2 test). No provider call is ever made just because Redis is down.

**M1 integration:**
- `forms` reads `documents` (`document_type=form`, status completed/partial), re-verifies the PDF hash and renders pages with PyMuPDF (150 dpi default; fold-out halves separately);
- it never writes `chunks`, `document_pages`, `ingestion_runs` or Qdrant;
- `run`/`ingest`/`reconcile` are untouched.

## 10. Security, privacy, cost, latency, reliability
- **Security:** retrieved text and form images are untrusted data. Prompts delimit them; schema validation enforces output shape; returned free text is stored, never executed or used as instructions. Secret scanning per §9 item 4.
- **Privacy:** only approved public blank forms leave the machine (§9 checklist). No customer data or PII anywhere. TTL on every Redis key.
- **Cost:** free tier only. Live calls are opt-in, one per form version, with Postgres (and Redis when available) preventing repeat calls. `--dry-run` shows the call count first. BM25 is in-process; the reranker is local CPU.
- **Latency:**
  - the BM25 build target is < 1 s (measured target, not a gate);
  - the per-query fingerprint check is a few ms;
  - RRF is negligible;
  - the reranker is bounded by N and its timeout.
  - All are measured and reported.
- **Reliability:** every external call has a timeout + ≤ 2 retries; degraded legs are flagged; no stale BM25 is ever served; Redis loss is harmless; per-form failure isolation.

## 11. M1 regression tests
1. All existing tests pass unchanged (M1: 266 unit / 70 integration).
2. **Dense golden snapshot:** dense mode and the legacy `search()` give identical top-5 ids and order on all 20 dev queries; default `evaluate` still gives 0.825 / 0.90 / 0.775 until the A3 default change, which is documented with its dev evidence.
3. Existing CLI invocations parse and behave the same. New flags are additive. `search` without `--mode` uses `RETRIEVAL_MODE`.
4. `make ingest` on unchanged data: 0 rows, 0 embeddings, CONSISTENT 429=429.
5. **Reconcile semantics:** `reconcile` / `--repair` / `--delete-unknown` produce the same consistency verdicts, repairs and exit codes as M1, with the keyword line informational only (tests with an injected inconsistency and with the keyword count unavailable).
6. Migration 0003 upgrade/downgrade round trip preserves the M1 tables and row counts; `alembic check` is clean.
7. Payload parity (Postgres vs Qdrant) and filter parity (Python vs Qdrant).
8. Isolation: `app.*` imports and runs with no Gemini, rerank or Redis configuration; `google.genai` and `CrossEncoder` are not imported unless used.
9. `make lint` (ruff, black, mypy strict) is clean.

## 12. Smallest useful vertical slice
**Stage A1: BM25-only retrieval end to end.** The slice is:
1. load the 429 eligible rows from Postgres;
2. tokenize, then build `BM25Okapi`;
3. filter, then score;
4. return `SearchHit`s with identical payloads and citations;
5. run `search --mode bm25 "Form 60 instead of PAN"`, with `--expand` working;
6. `evaluate --mode bm25` on **dev** prints Recall@5 / Hit@5 / MRR@5 / latency next to dense.

This exercises corpus loading, tokenization, filtering semantics, provenance, parent expansion and the evaluation harness, with no fusion, model or migration risk.

## Critical files
- Modify: `app/retrieval/search.py` (`SearchFilters.matches`), `app/retrieval/evaluation.py` (pluggable retriever, dev comparison and held-out reports), `app/ingestion/cli.py` (`--mode`, `--compare`, `--rerank`, `forms`), `app/config.py`, `app/retrieval/reconcile.py` (informational line only), `backend/pyproject.toml` + `requirements.lock`, `Makefile`, `infra/.env.example`.
- New: `app/retrieval/{keyword,fusion,retrieve,rerank}.py`, `app/llm/{multimodal,gemini,fake}.py`, `app/schemas/forms.py`, `app/forms/{pipeline,approval}.py`, `app/db/forms.py`, `app/core/cache.py`, `alembic/versions/0003_m2_form_schemas.py`, `data/eval/m2_heldout.jsonl`, `data/raw_pdfs/forms_approved.yaml`, tests per module.

## Verification (end to end)
1. `make up && make migrate && make check-schema`
2. `make test && make test-integration && make lint`
3. `make ingest` (0 writes, consistent) and `make reconcile` (unchanged verdict and exit code)
4. `make search MODE=hybrid Q="…"`
5. dev comparison (`evaluate --compare`) → selection commit → held-out run once
6. (B2) dev rerank comparison → selection commit → held-out once
7. `make forms DOC=hdfc_resident_rekyc_dormant PROVIDER=fake` twice (0 calls on the second), then again with Redis stopped (identical results)
8. after the checklist, the live Gemini run on that one form

## Open items (gates)
- Your review of the held-out queries (Stage 0).
- Approval of a specific reranker model download (Stage B2).
- Your approval of `forms_approved.yaml`, verification of the current Gemini data-use terms, a verified `MULTIMODAL_MODEL` ID, and your `GEMINI_API_KEY` in `.env` (Stage C3).

## Consistency check (revision 2)
- Every selection rule (§5 A3/B2, §6, §8) uses **dev only**; held-out is reported once per workstream with a fixed reference and never used to choose. The "dev + held-out combined" rule from revision 1 is removed.
- RRF `k` is now a declared dev-selected choice ({60, 20}), not fixed. This matches correction 1 ("select … RRF configuration using development data").
- BM25 build time appears only as a measured target (§5 A1, §10).
- Redis appears only as an optional cache. All gating decisions read Postgres (§9, C2 test).
- Reconcile changes are informational only, and M1 exit codes are preserved (§7, §11.5).
- Entity uncertainty is valid (§9 schemas, C1 criteria). Confidence is a heuristic (§9).
