# M2 Remaining Plan: Reranker Flag → Form Schemas → Evidence Workflow → Handoff

> Status: **approved in principle by the project owner (2026-11-10), with the clarifications below incorporated.** Implementation waits for review of this document.

## Context
Retrieval research (M2 A1–A3 and the held-out review) is complete. **Dense is the operational default** and C6 is an optional experimental mode (`5855be4`, `docs/milestones/M2_RETRIEVAL_OPERATIONAL_DECISION.md`).

The owner has reduced scope. The goal is a **working, demonstrable, evidence-grounded KYC prototype**:
- No new datasets, model grids, research workstreams, dependencies or infrastructure unless a concrete function requires them.
- No reopening of finished stages for cosmetics.
- **M1 and M2 A1–A3 are frozen,** apart from genuine correctness or reliability fixes.

This document replaces the earlier, larger M2 B draft, which was never committed.

**Owner decisions:**
- **Form extraction:** deterministic, from the stored M1 chunks, now. A Gemini extractor may follow later, behind the same schema; it is deferred and not built in M2.
- **Reranker evaluation:** one usefulness-and-latency diagnostic on the existing M1 development set. It is explicitly *not* a validation.
- **Reranker model:** `cross-encoder/ms-marco-MiniLM-L6-v2` (Apache-2.0, 22.7M parameters, BERT tokenizer, no new package). This is the only new model.

**Verified environment (2026-11-10):**
- **Gemini:** `GEMINI_API_KEY` **is present** in `.env` (checked without printing it) and `MULTIMODAL_MODEL` is configured. `google-genai` is **not installed**. Gemini work stays deferred, and nothing in M2 depends on it.
- **torch:** CPU build only. `sentence-transformers` 6.2.0 provides `CrossEncoder`.

**What the stored M1 form data supports:**
- **Field structure for the 3 native-text forms:** `form_fields` chunks (`Fields: a | b | *c`) with sections, physical pages and chunk ids. NRI account opening has ~312 labels, resident-to-NRO conversion ~247 and re-KYC/dormant ~50.
- **OCR text only for the 2 scanned forms:** non-individual account opening and NRI re-KYC.
- **Some labels are noisy:** option groups are sometimes merged into one label. This is reported as a limitation and never tuned.

## Extractable documents (6 = 5 KYC forms + 1 reference)
| document_key | Role | Eligibility | Quality | Extraction result |
|---|---|---|---|---|
| `hdfc_nri_account_opening` | **KYC form** | supporting | native text | sections + fields + text blocks |
| `hdfc_resident_to_nro_conversion` | **KYC form** | supporting | native text | sections + fields + text blocks |
| `hdfc_resident_rekyc_dormant` | **KYC form** | supporting | native text | sections + fields + text blocks |
| `hdfc_non_individual_account_opening` | **KYC form** | supporting | scanned (fold-out) | sections + **OCR text blocks only** |
| `hdfc_nri_rekyc` | **KYC form** | supporting | scanned | sections + **OCR text blocks only** |
| `hdfc_netbanking_corporate_application` | **reference (not a KYC form)** | context_only | native text | extractable, but flagged `reference_document` / `context_only`. Never offered as a KYC form in the evidence workflow unless explicitly requested. |

## Keep / remove / defer
- **Remove** (from the earlier draft):
  - new datasets, owner annotation rounds and a held-out confirmation;
  - frozen thresholds and a selection rule;
  - a model comparison;
  - hybrid + rerank evaluation;
  - a timeout worker thread;
  - the GPU/CUDA option.
- **Defer** (beyond M2):
  - the Gemini `MultimodalProvider`, `google-genai`, page rendering, the approved-forms allowlist and the data-terms check;
  - the Redis form cache (there is no functional need: a Postgres read is cheap);
  - reranking as a default;
  - LLM synthesis and requirement checklists (M3);
  - human review (M4).
- **Keep:**
  - one feature-flagged reranker plus one diagnostic;
  - deterministic form schemas with provenance and versioning, persisted through Alembic;
  - one CLI evidence workflow;
  - regression and handoff.

## Milestones (one commit each)

### B1: Cross-encoder reranker behind a flag (default off)
- **`app/retrieval/rerank.py`:**
  - `CrossEncoderReranker`: lazily loads `sentence_transformers.CrossEncoder(RERANK_MODEL, revision=RERANK_MODEL_REVISION, max_length=512)` on CPU.
  - `rerank(query, hits, reranker, top_k)`: a pure function. It scores each retrieved chunk's `embedding_input` (child context + content; never the parent) and returns new `SearchHit`s. Ties go to the original rank, then `chunk_id`. Payloads and citations are untouched.
- **`Retriever.retrieve(..., rerank=False)`:** when enabled, fetches `RERANK_CANDIDATES` (20) from the selected mode, reranks them and keeps `top_k`.
- **Scores stay distinct:**
  - Provenance keeps the **original retrieval rank and score** (`pre_rerank_rank`, `pre_rerank_score`, plus the existing dense/BM25 fields) **separately** from `rerank_score`.
  - `score_kind = cross_encoder`.
  - Cross-encoder scores are **raw and uncalibrated**. They are never treated as confidence, never thresholded, and never used as an evidence-quality signal. This is stated in the code and in docs/04.
- **Latency:** reported separately as `timings_ms["rerank_ms"]`, plus `rerank_load_ms` on first use.
- **Failures:** a load or inference failure keeps the original order and returns a degraded result (`degraded=("rerank",)`) with the error recorded. The existing DEGRADED banner and exit-0 rule apply. Evaluations refuse degraded runs.
- **Settings:** `RERANK_ENABLED=false`, `RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2`, `RERANK_MODEL_REVISION=<pinned commit>`, `RERANK_CANDIDATES=20`.
- **CLI:** `search` / `evaluate --rerank|--no-rerank`.
- **Tests:**
  - with a fake reranker: ordering, ties, unchanged payloads and citations, distinct score fields, degraded on failure, never loaded while disabled;
  - one real-model test on synthetic queries;
  - dense golden unchanged.
- **Done when:** disabled is byte-identical to the current path; enabled reorders 20 → 5 with full provenance; lint and the suites are green.

### B2: Bounded diagnostic (one run, no tuning)
- **The run:** `evaluate --mode dense --rerank` **once** on `data/eval/m1_retrieval.jsonl`.
- **Reported:**
  - Recall@5, Hit@5 and MRR@5, against the known dense baseline;
  - dense Recall@20, which is the reranker's ceiling;
  - p50/p95 total latency, `rerank_ms` and model load time;
  - per-query wins and losses.
- **Framing:** documented in docs/10 as *a diagnostic on a previously used development set, not a validation*.
- **Flag default stays off whatever the result.** There is no held-out run and no second run.

### C1: Deterministic form schemas (extraction only)
- **`app/schemas/forms.py`:**
  - **`FormSchema`:** form key, document id and SHA-256, title, version, `document_role` (`kyc_form` | `reference_document`), eligibility, `extraction_method="m1_chunks"`, `extractor_version=1`, sections, quality flags and counts.
  - **`FormSection`:** name, page range, region, `fields[]`, `text_blocks[]`, `is_declaration`.
  - **`FormField`:**
    - `printed_label`: the label **exactly as extracted, including any `*` and original formatting**;
    - `label`: a normalised display form, with the marker removed;
    - `required_as_shown`: true only when the printed label carries `*`, stored separately from the label;
    - page, chunk id and `text_reliability`;
    - `appears_on_form=True` and `verification_status="extracted"`;
    - `mandatory_under_policy=None`, which extraction cannot set.
  - **`TextBlock`:** chunk id, page, region, text (as stored), `text_reliability`, `ocr_confidence`, and `verification_status` (`extracted` for native text, `ocr_uncertain` / `ocr_reliable` for OCR).
- **`app/forms/extract.py`, `extract_form(session, document_key) -> FormSchema`:**
  - reads stored chunks in `chunk_index` order (reusing `chunk_record_from_rows`) and groups them by section;
  - parses `Fields:` chunks by splitting on `|` **without discarding source formatting**;
  - declaration sections are those whose section name contains "DECLARATION".
  - **Scanned forms:** OCR text is kept verbatim as `TextBlock`s with page provenance, reliability and confidence. It is **never turned into structured fields and never marked as verified field extraction**. These sections are flagged `no_field_structure`.
  - The output is deterministic.
- **Tests:**
  - synthetic chunks: printed label kept with `*`, `required_as_shown` separate, declarations, OCR stays text-only with its flags, determinism;
  - one integration test over the real corpus: 6 schemas (5 `kyc_form` + 1 `reference_document`), with counts consistent with the chunk data.

### C2: Persistence, versioning, CLI
- **Alembic `0003_m2_form_schemas`** (additive; downgrade drops it) and an ORM row:
  - columns: `id` (derived from the SHA-256 + method + extractor version), `document_id` FK (CASCADE), `form_key`, `document_sha256`, `document_role`, `extraction_method`, `extractor_version`, `schema_json` (JSONB), `section_count`, `field_count`, `quality_flags` (text[]), `created_at`;
  - **UNIQUE**(`document_id`, `extraction_method`, `extractor_version`).
- **Versioning:**
  - a new PDF or a new extractor version produces a new row, and older rows are kept;
  - an unchanged form writes nothing;
  - a future Gemini extractor would write rows under its own `extraction_method`.
- **Repository and CLI:**
  - `app/db/forms.py`;
  - CLI `forms extract [--document KEY]`, `forms list` and `forms show KEY [--json]`;
  - Make target `forms`.
- **Tests:** migration up/down/up on a throwaway database, idempotent re-extraction, and a version bump producing a new row. `make check-schema` stays clean.

### C3: Integrated evidence workflow (no LLM)
- **`app/evidence.py`, `build_evidence_pack(query, *, entity_terms, residency=None, mode, rerank, top_k=5)`:**
  - **Retrieval:** existing filtered retrieval (`Retriever`), then parent expansion (`evidence_context`).
  - **Mandatory-capable evidence:** comes **only** from chunks with `can_support_mandatory=True`, that is, primary, active RBI regulatory text that is not uncertain OCR. Supporting evidence (HDFC policy, forms, FAQs) is listed separately and labelled as such.
  - **Forms:** the KYC forms applicable to the entity and residency (`document_role=kyc_form`; the reference document is excluded unless explicitly requested), plus the matching form section for any retrieved hit that came from a form. Every field is presented as **"appears on form"**, never as mandatory under policy.
  - **Missing and uncertain evidence stays visible:**
    - no mandatory-capable passage found → reported as such;
    - OCR-uncertain items, degraded retrievers and scanned forms without field structure → flagged in the output.
  - **No automatic compliance approval or rejection, and no synthesised requirements.** The pack states it is an evidence pack for human review, not a compliance determination.
- **CLI:** `kyc QUERY --entity T [--residency R] [--json]`, and Make `kyc`. Every item cites document, page and chunk id.
- **Tests:**
  - unit tests on fixtures, covering the separation of mandatory-capable from supporting evidence, the field wording, visible gaps and flags, and the reference-document exclusion;
  - **one end-to-end acceptance test** on the real stores: a trust-account query → policy retrieval with citations (RBI para 35) → applicable forms associated → extracted fields with page and chunk citations → no field marked mandatory and no approve/reject output.

### D: Regression, documentation, handoff
- **Checks:** full unit and integration suites (`TESSDATA_DIR` unset), `make lint`, `make check-dense-golden`, a `make ingest` no-op, `make reconcile` and `make check-schema`.
- **Docs:**
  - docs/01, 04 and 08 and the README demo path;
  - `docs/milestones/M2_HANDOFF.md`: status, measurements, limitations, deferred items (Gemini, reranker default, Redis cache) and a scripted demo.

## Dependencies and order
- **B1** needs the one approved model download (MiniLM).
- **C2** needs an Alembic migration.
- **No new Python packages.**
- **Order:** B1 → B2 → C1 → C2 → C3 → D. C3 depends on C2 and may use B1's flag.
- **Gemini** is deferred. It is not a blocker: deterministic extraction works without it.

## Risks
- **Extraction quality:** noisy merged labels, and no field structure for the scanned forms. Both are flagged, not tuned.
- **Reranker:** costs CPU latency, and its diagnostic runs on a reused set, so no generalisation claim can be made.
- **Evidence pack:** only as good as retrieval. Dense misses stay visible as limitations.

## Verification at the end of D
1. `make up migrate check-schema ingest forms`
2. `make kyc Q="documents needed to open a trust account" ENTITY=TRUST`
3. `make search Q=… ` with `--rerank`
4. `make test`, `make test-integration`, `make lint`, `make check-dense-golden`, `make reconcile`

All must be green, and every output must carry page and chunk citations.
