# 07 — API Design (`/api/v1`)

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/sessions` | Create session |
| GET | `/sessions/{id}` | Get session |
| POST | `/sessions/{id}/messages` | Submit query (starts graph) |
| POST | `/sessions/{id}/resume` | Resume after clarification / review |
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
Rules: versioned from day one, Pydantic request/response models, consistent error envelope `{error:{code,message}}`, idempotency key on message POST.
