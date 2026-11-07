# 08 — Data Model (Postgres, Redis, Qdrant)

## Postgres (SQLAlchemy 2 + Alembic migrations)
- `documents`(id, filename, sha256 UNIQUE, bank, category, source_authority NULL, version, supersedes_document_id NULL, status (active|superseded|dated), ingestion_status, quality, created_at)
- `chunks`(id, document_id FK, bank, page_number, section, entity_types text[], content, parent_chunk_id NULL, metadata jsonb) — mirrors Qdrant payload for keyword search
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
Collection `policy_chunks` (BGE-small 384-dim, cosine). Payload = chunk metadata (document_id, bank, document_type, entity_types, page_number, section, version…). Create payload indexes on `bank`, `document_type`, `entity_types`, `document_id`. Qdrant holds retrieval data only — not conversation state.

## Files
`data/raw_pdfs` (private, gitignored), `data/processed` (page images, extracted JSON). Never under the public frontend dir.
