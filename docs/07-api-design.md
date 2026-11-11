# 07 — API Design (`/api/v1`)

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/sessions` | Create session |
| GET | `/sessions/{id}` | Get session |
| POST | `/sessions/{id}/messages` | Submit query (starts graph) |
| POST | `/sessions/{id}/resume` | Resume after clarification: structured enum answers + `checkpoint_id` only, no free text (M3 decision D7) |
| POST | `/sessions/{id}/retry` | Explicitly resume a run whose lease expired after a crash, from its last checkpoint (M3) |
| GET | `/sessions/{id}/events` | **SSE** progress stream |
| POST | `/documents/upload` | Upload PDF |
| GET | `/documents` , `/documents/{id}` | List / details |
| POST | `/ingestion/run` | Enqueue ingestion job |
| GET | `/reports/{id}` | Compliance report |
| GET | `/reports/{id}/export` | PDF export (enhancement) |
| GET | `/reviews` , POST `/reviews/{id}/decision` | Human review queue |
| GET | `/observability/runs/{id}` | Node-level execution details |
| GET | `/health` | Health (db, redis, qdrant). 200 when all ok, 503 `degraded` otherwise; per-check `ok`, `latency_ms`, `error` (exception class only, never URLs/credentials); each check times out after `HEALTH_CHECK_TIMEOUT_S` (default 2s) |

## Request
```json
{"session_id":"session_001","message":"What KYC documents are required for an LLP current account?",
 "entity_context":{"entity_type":"LLP","account_type":"CURRENT_ACCOUNT"}}
```
## Report response (illustrative)
> **As implemented (M3 S5): `ComplianceReport` in `app/graph/state.py`. It supersedes the sketch below.**
> - **Fields:**
>   - `report_id`, `outcome` (one of five; no approve or reject value exists) and `human_review_required: true`;
>   - the fixed `notice` and the independence `disclaimer`;
>   - a deterministic `user_message`;
>   - `profile` (values and sources only, never the user's words);
>   - `evidence_status` / `gaps`, `checklist` (validated requirements with citations copied from stored chunks), `dropped_requirements`, `unresolved_items`, `findings`, `degradations`;
>   - `forms`, where `subset_only` marks a capped field analysis;
>   - `security_reasons` (BLOCKED only: codes, never text).
> - **Requirement fields:** `applicability` (the validator's) beside `proposed_applicability` (the model's), `uncertainty_label`, `verification_status: not_claim_verified`.

```json
{"report_id":"report_001","entity_type":"LLP","account_type":"CURRENT_ACCOUNT",
 "status":"REVIEW_REQUIRED",
 "requirements":[{"requirement_id":"REQ_001","name":"...","applicability":"MANDATORY",
   "verification_status":"SUPPORTED",
   "evidence":[{"document_id":"doc_001","page":12,"section":"..."}]}],
 "unresolved_items":[],"review_required":true}
```
## Clarification (interrupt) response
```json
{"status":"CLARIFICATION_REQUIRED","questions":[{"field":"partners","text":"..."}],"checkpoint_id":"..."}
```
## SSE events (backed by Redis pub/sub)
```
event: status
data: {"node":"retrieval","status":"running"}

event: report
data: {"report_id":"report_001"}
```
Rules: versioned from day one, Pydantic request/response models, consistent error envelope `{error:{code,message}}`, `Idempotency-Key` header required on `/messages`, `/resume` and `/retry`. `/messages` and `/resume` are PII-screened before anything is stored or sent (`422 SENSITIVE_DATA_DETECTED`). Report outcomes and run statuses: `docs/milestones/M3_PLAN.md` §5; no endpoint ever returns an approve/reject decision.
