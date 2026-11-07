# 08 — Data Model (Postgres, Redis, Qdrant)

## Postgres (SQLAlchemy 2 + Alembic migrations)
M1 tables (Alembic `0001_m1_corpus`; ORM `app/db/models.py`; field meanings in `app/schemas/corpus.py` and docs/15). Enum-valued columns are TEXT with CHECK constraints.
- `documents`: one row per **source document version**. `id` is a UUID derived from `sha256`. `document_key` is the logical identity from the manifest; `sha256` is UNIQUE, and in M1 `document_key` is UNIQUE too (one version per document, see docs/15). Other columns: `file`, `bank`, `document_type` (was `category`), `source_authority` NULL, `attribution_basis`, `title`, `version`, `issue_date`, `effective_from`/`effective_until` NULL, `source_status` (active|dated|superseded|unknown; was `status`), `status_reason`, `evidence_eligibility` (primary|supporting|context_only, derived), `supersedes_document_id` NULL, `version_review_status`, `entity_types text[]`, `residency_statuses text[]`, `ingestion_status`, `quality`, `page_count`, `metadata` jsonb (supersedes keys, coverage notes, notes), `created_at`, `updated_at`.
- `document_pages`: (`document_id`, `page_number`) PK; one row for **every** page, failed ones included. Columns: `extraction_method` (text|ocr), `status` (ok|low_quality|ocr_low_confidence|failed; a failed page must carry an `error`), `char_count`, `alpha_ratio`, `image_coverage`, `ocr_confidence` (0–1).
- `chunks`: `id` (UUID derived from sha256 + chunk_index), `document_id` FK, `chunk_index` (UNIQUE per document), `document_key`, `bank`, `document_type`, `source_status`, `evidence_eligibility`, `page_number` (FK with `document_id` → `document_pages`, so every citation points at a recorded page), `page_end`, `section`, `entity_types text[]`, `residency_statuses text[]`, `extraction_method`, `content`, `content_sha256`, `parent_chunk_id` NULL, `metadata` jsonb. These columns mirror the Qdrant payload for keyword search; the payload's other document-level fields live on `documents`.
- A document's version relationship is never inferred from a hash change. A changed file fails manifest validation (SHA-256 mismatch) and is not ingested until a person records its identity and version relationship in the manifest. `supersedes` comes only from the manifest. `version_review_status` holds the review state of a recorded relationship (default `not_required`).
- `form_schemas`(id, form_id, version, file_hash, entity_applicability text[], extracted_json jsonb, validation_status, review_status, extracted_at)
- `rules`(rule_id PK, entity_type, topic, source_document FK, source_page, conditions jsonb, applicability, verification_status)
- `sessions`(id, entity_type, account_type, status, created_at, expires_at)
- `reports`(id, session_id FK, final_json jsonb, verification_status, created_at)
- `claims`(id, report_id FK, claim_text, evidence_refs jsonb, nli_label, nli_scores jsonb, confidence, review_status)
- `review_cases`(id, report_id, claim_id, reason, status, reviewer_decision, reviewer_note, created_at, decided_at)
- `graph_runs`(id, session_id, langsmith_run_id, status, latency_ms, retries, token_usage jsonb, node_events jsonb)
- `eval_runs`(id, config_name, created_at) and `eval_results`(id, run_id, case_id, predicted jsonb, expected jsonb, metrics jsonb)
- LangGraph checkpoint tables are created by `langgraph-checkpoint-postgres` (`.setup()`).

Use a separate schema or prefix for checkpoint tables. Apply retention (delete expired sessions/uploads).

## Redis key conventions (all with TTL)
| Key | Purpose | TTL |
|---|---|---|
| `rl:{provider}:{window}` | rate-limit token bucket | seconds–minutes |
| `cache:llm:{sha256(prompt+model+version)}` | LLM response cache | 24h |
| `cache:emb:{sha256(text)}` | embedding cache | 7d |
| `cache:form:{file_hash}:{version}` | form schema cache | 7d |
| `chan:session:{id}` | pub/sub progress channel | n/a |
| `lock:ingest:{sha256}` / `lock:run:{session}` | idempotency / one-run lock | minutes |
| `arq:*` | job queue | managed |

## Qdrant
Collection `policy_chunks` (BGE-small 384-dim, cosine). Payload = `ChunkMetadata.to_qdrant_payload()` (app/schemas/corpus.py): chunk_id, document_id, document_key, document_name, file, bank, document_type, source_status, evidence_eligibility, source_authority, version, effective_from/until, entity_types, residency_statuses, section, page_number, page_end, chunk_index, parent_chunk_id, extraction_method, content. Keyword payload indexes (`QDRANT_KEYWORD_INDEXES`): `document_id`, `document_key`, `bank`, `document_type`, `source_status`, `evidence_eligibility`, `entity_types`, `residency_statuses`. Compliance retrieval filters on `evidence_eligibility` (it excludes `context_only`), so eligibility is a filter, not a ranking signal. Qdrant holds retrieval data only — not conversation state.

## Files
`data/raw_pdfs` (private, gitignored), `data/processed` (page images, extracted JSON). Never under the public frontend dir.
