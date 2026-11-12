# 08 — Data Model (Postgres, Redis, Qdrant)

## Postgres (SQLAlchemy 2 + Alembic migrations)
M1 tables (Alembic `0001_m1_corpus` + `0002_m1_persistence`; ORM `app/db/models.py`; field meanings in `app/schemas/corpus.py` and docs/15). Enum-valued columns are TEXT with CHECK constraints. Postgres is the source of truth for documents, pages and chunks; Qdrant is rebuilt from it (docs/04, Stage E).
- `documents`: one row per **source document version**. `id` is a UUID derived from `sha256`. `document_key` is the logical identity from the manifest; `sha256` is UNIQUE, and in M1 `document_key` is UNIQUE too (one version per document, see docs/15). Other columns: `file`, `bank`, `document_type` (was `category`), `source_authority` NULL, `attribution_basis`, `title`, `version`, `issue_date`, `effective_from`/`effective_until` NULL, `source_status` (active|dated|superseded|unknown; was `status`), `status_reason`, `evidence_eligibility` (primary|supporting|context_only, derived), `supersedes_document_id` NULL, `version_review_status`, `entity_types text[]`, `residency_statuses text[]`, `ingestion_status`, `quality`, `page_count`, `metadata` jsonb (supersedes keys, coverage notes, notes), `created_at`, `updated_at`.
- `document_pages`: (`document_id`, `page_number`) PK; one row for **every** page, failed ones included. Columns: `extraction_method` (text|ocr), `status` (ok|low_quality|ocr_low_confidence|failed; a failed page must carry an `error`), `char_count`, `alpha_ratio`, `image_coverage`, `ocr_confidence` (0–1). `metadata` jsonb (since 0002): the Stage A/B page record (removed-line counts, page label, footnote count, OCR DPI/PSM/language and per-region printed page, word/line counts, mean and low confidence; per-run cache flags are left out so re-ingestion is a no-op). `char_count` counts the text layer.
- `chunks`: **every** chunk (single, parent, child). `id` (UUID derived from sha256 + chunk_index), `document_id` FK, `chunk_index` (UNIQUE per document), `document_key`, `bank`, `document_type`, `source_status`, `evidence_eligibility`, `page_number` (FK with `document_id` → `document_pages`, so every citation points at a recorded page), `page_end`, `section`, `entity_types text[]`, `residency_statuses text[]`, `extraction_method`, `content` (normalised), `content_sha256`, `raw_text` (pre-normalisation source lines), `parent_chunk_id`, `chunk_role` (single|parent|child), `text_reliability` (native|ocr_reliable|ocr_uncertain), `embedding_eligible`, `can_support_mandatory`, `metadata` jsonb, `created_at`, `updated_at`. These columns mirror the Qdrant payload for keyword search; the payload's other document-level fields live on `documents`. `chunks.metadata` holds the payload's provenance fields (`paragraph`, `region`, `printed_page`, `content_kind`, `page_status`, `ocr_confidence`, `footnotes`, `context`; `CHUNK_JSON_PAYLOAD_FIELDS`) plus Postgres-only `excluded_lines` (text, page, region, confidence). Since 0002 the role/reliability/eligibility flags and `raw_text` are columns (backfilled from `metadata` by the migration). Integrity: `(document_id, parent_chunk_id)` → `chunks(document_id, id)` DEFERRABLE INITIALLY DEFERRED with no delete action (a parent is in the same document and cannot be deleted while children reference it); CHECKs: exactly the children have a parent, parents are never embedding-eligible, a chunk is not its own parent, `text` extraction ⇔ `native` reliability, `can_support_mandatory = (primary AND NOT ocr_uncertain)`. Qdrant holds only the embedding-eligible single and child chunks.
- `document_exclusions` (0002): (`document_id`, `seq`) PK; `page_number` (FK → `document_pages`), `region`, `reason` (`section_heading`, `table_cell_line`, `duplicate_line`, `noise`, …), `text`: lines the chunkers left out of every chunk's content.
- `ingestion_runs` (0002): one row per ingestion attempt: `id`, `document_key`, `sha256`, `document_id` (no FK, so a rolled-back first ingestion is still recorded), `command`, `status` (processing|pending|completed|partial|failed; `pending` = persist-only run), `failed_stage` (extraction|ocr|chunking|persistence|embedding|indexing), `error`, `stages` jsonb (per-stage status, counts, error), `started_at`, `finished_at`. A failed run must name its stage and error.
- `documents.ingestion_status`: `pending` after `persist` (stored, not yet indexed; `retry` or `index --from-db` finishes it); `processing` while a run is between its Postgres write and its indexing check; `completed` only after every stage succeeded; `partial` when every stage ran but some pages failed (recorded with their error); `failed` when the latest run failed (earlier rows and vectors stay intact).
- A document's version relationship is never inferred from a hash change. A changed file fails manifest validation (SHA-256 mismatch) and is not ingested until a person records its identity and version relationship in the manifest. `supersedes` comes only from the manifest. `version_review_status` holds the review state of a recorded relationship (default `not_required`).
- `form_schemas` (implemented, M2 C2/G3, migrations `0003_m2_form_schemas` + `0004_m2_form_schema_guards` + `0005_m2_gemini_method`; ORM `FormSchemaRow`; repository `app/db/forms.py`): one row per **(document, extraction method, extractor version)**.
  - **Columns:**
    - `id`: UUID derived from the document SHA-256, method and version;
    - `document_id`: FK to `documents`, ON DELETE CASCADE;
    - `form_key`, `document_sha256`;
    - `document_role` (`kyc_form` | `reference_document`), `evidence_eligibility`;
    - `extraction_method` (`m1_chunks`), `extractor_version` (≥ 1);
    - `schema_sha256`: SHA-256 of the canonical JSON;
    - `schema_json`: JSONB holding the full `FormSchema`: sections, fields with printed label, `required_as_shown`, page, region, chunk ID and position, plus text blocks with OCR confidence and verification status, flags and issues;
    - the counts `section_count`, `field_count`, `required_as_shown_count`, `text_block_count`;
    - `quality_flags` (text[]), `created_at`.
  - **Identity and versioning:** UNIQUE(`document_id`, `extraction_method`, `extractor_version`). A new PDF (new SHA-256, so a new document ID) or a new extractor version is a **new row**; older rows are kept. An identical re-extraction writes nothing.
  - **No overwrites:** a *different* schema under an existing identity is refused (`FormSchemaConflict`), never overwritten.
  - **Database guards** (CHECK constraints):
    - no field in `schema_json` may carry `mandatory_under_policy`, and every field must have `appears_on_form = true`;
    - the JSON's role, method, version and SHA-256 must equal the columns;
    - roles, method, eligibility, SHA-256 format and counts are constrained.
    - **Extraction methods (migration `0005_m2_gemini_method`, M2 G3):** `ck_form_schemas_extraction_method` allows `m1_chunks` and `gemini_multimodal`. Every other guard applies to both. The downgrade refuses while any `gemini_multimodal` row exists rather than deleting data. `load_form_schema` reads a single method (default `m1_chunks`).
    - **Strict guards (migration `0004_m2_form_schema_guards`, M2 C2 follow-up):** a CHECK accepts a NULL result, so the 0003 expressions could be bypassed by JSON keys that were missing, null, wrongly typed or misplaced. 0004 adds two constraints whose expressions are always a definite TRUE or FALSE:
      - `ck_form_schemas_json_identity`: the JSON's `document_role`, `extraction_method` and `document_sha256` must equal their columns (`IS NOT DISTINCT FROM`, so a missing or null key is a mismatch), and `extractor_version` must be a JSON number equal to the column;
      - `ck_form_schemas_field_structure`: `sections` must be an array of objects, each with a `fields` array. Every field must be an object (not nested in an extra array) in which `appears_on_form` exists, is a boolean and is `true`, and `mandatory_under_policy` exists and is JSON `null`.
      - 0003 is unchanged. The downgrade drops only these two constraints. Adversarial raw-insert tests are in `tests/integration/test_form_schema_guards.py`.
  - **Read cache (M2 G4):** reads can go through the Redis cache keyed by (form_key, method, version). Postgres decides the current row and validates every cached entry (docs/02); the table is unchanged.
  - **Not stored:** review status and entity applicability. These belong to later human-review and rules stages, and are not inferred from forms.
- `rules`(rule_id PK, entity_type, topic, source_document FK, source_page, conditions jsonb, applicability, verification_status)
- **M3 workflow tables (implemented in M3 S5, Alembic `0006_m3_workflow`, ORM `KycSession`/`GraphRun`/`ReportRow`/`ApiIdempotency`, repository `app/db/workflow.py`; `docs/milestones/M3_PLAN.md` §11. `reports`: CHECKs on outcome (five values), `human_review_required IS TRUE` and JSON identity; a `BEFORE UPDATE` trigger makes it insert-only; UNIQUE(session_id, run_id). `graph_runs`/`api_idempotency` are used from S6; supersede the `sessions`/`reports`/`graph_runs` sketches below):** `kyc_sessions`, `graph_runs` (run lifecycle + Postgres run lease, one active run per session via a partial unique index), `reports` (insert-only, one `outcome` each, `human_review_required` always true), `api_idempotency`. The one-run-per-session lock is the Postgres lease, **not** a Redis key (M3 decision D6).
- `sessions`(id, entity_type, account_type, status, created_at, expires_at)
- `reports`(id, session_id FK, final_json jsonb, verification_status, created_at)
- **Report versions (M4 S1, no migration):** `reports.report_json` holds a `ComplianceReport`. Rows written by M3 have no `report_version` key and read back as **v1**, unchanged (tests: `tests/unit/test_m4_models.py`, `tests/integration/test_m4_report_compat.py`). **v2** (M4) adds `claim_verification`, `verification_meta`, `unverified_items` and the M4 notice; the existing CHECKs and the insert-only trigger accept it as is. A v1 report can carry no M4 field.
- **`review_cases` (M4 S7, Alembic `0008_m4_review_cases`, ORM `ReviewCaseRow`):**
  - **Columns:** `id` uuid (UUIDv5 of report id, requirement id and reason), `report_id` FK → reports ON DELETE CASCADE, `session_id` FK → kyc_sessions ON DELETE CASCADE, `run_id`, `requirement_id`, `claim_id` (NULL in M4), `reason`, `status`, `created_at`.
  - **Constraints:**
    - `reason` is restricted to `contradicted`, `conflicting_sources`, `unsupported`, `mandatory_not_established`, `explanation_overstates_source`, `not_verified`;
    - `status = 'open'` only;
    - UNIQUE(report_id, requirement_id, reason); index on `report_id`.
  - **Insert-only:** a `BEFORE UPDATE` trigger rejects every update. Rows go only by cascade.
  - **Writing:** cases are derived from the report's review flags (`app/graph/review.py`) and written in the same transaction as the report (`PostgresReportStore.save`). Re-saving is idempotent.
- `claims`: **not built** (M4 decision): claims live inside the immutable report JSON, not in a table. The sketch was `claims`(id, report_id FK, claim_text, evidence_refs jsonb, nli_label, nli_scores jsonb, confidence, review_status).
- `review_cases`(id, report_id, claim_id, reason, status, reviewer_decision, reviewer_note, created_at, decided_at)
- `graph_runs`(id, session_id, langsmith_run_id, status, latency_ms, retries, token_usage jsonb, node_events jsonb)
- `eval_runs`(id, config_name, created_at) and `eval_results`(id, run_id, case_id, predicted jsonb, expected jsonb, metrics jsonb)
- **LangGraph checkpoint tables (implemented in M3 S6, migration `0007_m3_checkpointer`):**
  - **Where they live:** the library's own `setup()` creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations` in a dedicated `langgraph` schema. The runtime pool sets `search_path=langgraph`.
  - **Not in the ORM:** `check-schema` compares the default schema only.
  - **Downgrade:** it refuses while any checkpoint exists.
  - **Threads:** one per question (`graph_runs.thread_id`).

Use a separate schema or prefix for checkpoint tables.

**Retention (implemented in M3 S8, `app/workflow/retention.py`; `make purge`, `make purge-dry`, `make check-workflow`):**

| Phase | When | What happens | What stays |
|---|---|---|---|
| 1 | `expires_at` | checkpoint data for every thread of the session (all `langgraph` tables with a `thread_id` column), the idempotency rows; open runs → `abandoned`; `status=expired`, `checkpoints_purged_at` set | session, runs, reports (readable) |
| 2 | `expires_at + REPORT_RETENTION_D` | checkpoint data deleted and verified first, then the session row; the FKs cascade to `graph_runs`, `reports`, `review_cases` (M4), `api_idempotency` | nothing |
| orphans | any time | checkpoint data whose thread has no `graph_runs` row | — |
| idempotency TTL | `created_at + IDEMPOTENCY_TTL_H` | old idempotency rows | — |

- **One transaction per session:**
  1. lock `kyc_sessions` and `graph_runs` (`FOR UPDATE`, `lock_timeout` 5 s);
  2. recheck the state;
  3. delete the checkpoint data and verify it is gone;
  4. change or delete the session.
- **Skipped, with a reason, and retried by the next purge:** a valid run lease (`live_lease`), a held lock (`locked`), or a state that changed since planning (`not_expired`, `gone`). Any error rolls back.
- **Stale writers:** checkpoint data written after phase 1 by a writer whose lease had already expired is reported by `check` and removed by the next purge.
- **Dry run:** `--dry-run` is one read-only transaction.

## Redis key conventions (all with TTL)
| Key | Purpose | TTL |
|---|---|---|
| `rl:{provider}:{window}` | shared rate limit (implemented M3 S1): a hash with `req` and `tok` counters for one fixed minute; window = epoch minute | 120 s |
| `kyc:llm:{sha256(prompt id+version, provider, model, schema, sha256(system), sha256(user), max tokens)}` | LLM response cache (implemented M3 S1, decision D8): **off by default**; only allowlisted synthetic inputs; value = validated structured output, never prompt or raw text | ≤ 24h |
| `cache:emb:{sha256(text)}` | embedding cache | 7d |
| `cache:form:{file_hash}:{version}` | form schema cache | 7d |
| `chan:session:{id}` | pub/sub progress channel (implemented M3 S7): a wake-up signal only; the SSE stream reads what it sends from Postgres, so a dropped publish loses nothing; stores nothing | n/a |
| `lock:ingest:{sha256}` | ingestion idempotency lock | minutes |
| `arq:*` | job queue | managed |

## Qdrant
Collection `policy_chunks` (implemented in M1 Stage D): BGE-small-en-v1.5, 384-dimensional, cosine, one point per embedding-eligible chunk, with point id = `chunk_id`. Payload = `ChunkMetadata.to_qdrant_payload()` (app/schemas/corpus.py) plus an `index` object (`embedding_model`, `embedding_revision`, `input_version`, `input_sha256`, `payload_sha256`) used for idempotent re-indexing. Keyword payload indexes (`QDRANT_KEYWORD_INDEXES`): `document_id`, `document_key`, `bank`, `document_type`, `source_status`, `evidence_eligibility`, `entity_types`, `residency_statuses`, `text_reliability`, `content_kind`, `chunk_role`; boolean: `can_support_mandatory`. Compliance retrieval filters on `evidence_eligibility` (excluding `context_only` by default) and, for MANDATORY evidence, on `can_support_mandatory`. Eligibility is a filter, not a ranking signal. An existing collection with a different vector size or distance is rejected, never recreated. Qdrant holds retrieval data only, not conversation state.

## Files
`data/raw_pdfs` (private, gitignored), `data/processed` (page images, extracted JSON). Never under the public frontend dir.
