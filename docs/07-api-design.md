# 07 — API Design (`/api/v1`)

> **Planned vs built.** The table below is the original plan. The **Status** column records what exists at the close of M4. Only the rows marked *built* exist. The others are not implemented, and M5 adds no endpoint (M5 decision M5-D2: response models on existing routes only). The authoritative contract is the "Workflow API as implemented" section below.

| Method | Endpoint | Purpose | Status |
|---|---|---|---|
| POST | `/sessions` | Create session | built (M3) |
| GET | `/sessions/{id}` | Get session | built (M3) |
| POST | `/sessions/{id}/messages` | Submit query (starts graph) | built (M3) |
| POST | `/sessions/{id}/resume` | Resume after clarification: structured enum answers + `checkpoint_id` only, no free text (M3 decision D7) | built (M3) |
| POST | `/sessions/{id}/retry` | Explicitly resume a run whose lease expired after a crash, from its last checkpoint (M3) | built (M3) |
| GET | `/sessions/{id}/events` | **SSE** progress stream | built (M3) |
| POST | `/documents/upload` | Upload PDF | not built |
| GET | `/documents` , `/documents/{id}` | List / details | not built |
| POST | `/ingestion/run` | Enqueue ingestion job | not built (ingestion is CLI-only) |
| GET | `/reports/{id}` | Compliance report | built (M3; v2 in M4) |
| GET | `/reports/{id}/review-cases` | Open review cases (read-only) | built (M4 S7) |
| GET | `/reports/{id}/export` | PDF export (enhancement) | not built |
| GET | `/reviews` , POST `/reviews/{id}/decision` | Human review queue | not built; a decision route needs authentication (M4 D5, future security milestone) |
| GET | `/observability/runs/{id}` | Node-level execution details | not built (M6) |
| GET | `/health` | Health (db, redis, qdrant). 200 when all ok, 503 `degraded` otherwise; per-check `ok`, `latency_ms`, `error` (exception class only, never URLs/credentials); each check times out after `HEALTH_CHECK_TIMEOUT_S` (default 2s) | built (M0) |

## Published contract (M5 S1)
- **The document:** `docs/api/openapi.json` (regenerate with `make openapi`). A unit test fails when it differs from the app. The frontend generates its types from it.
- **The models:** request and response shapes come from `app/api/v1/schemas.py` plus `ComplianceReport` (`app/graph/state.py`) and `ProgressEvent` (`app/core/events.py`, the SSE `data:` payload).
- **Documentation only:**
  - Routes keep `response_model=None` and declare no body parameter. FastAPI therefore neither parses a body before the service screens it nor re-serializes the output.
  - Responses are byte-identical to the service output, and contract tests check this.
  - Every model uses `extra="forbid"`, so an undocumented key fails the tests.
- **Optional keys:** keys documented with a default may be absent. `workflow.clarification` is present only while awaiting clarification; `workflow.report_id` and `workflow.outcome` only once completed.
- **Errors:** every 4xx/5xx is the `ErrorResponse` envelope. `categories` appears only on screening errors, `retry_after` only on some `RUN_IN_PROGRESS` errors.

## Workflow API as implemented (M3 S6; `app/api/v1/sessions.py`, `app/workflow/service.py`)
| Method | Endpoint | Body | Success |
|---|---|---|---|
| POST | `/sessions` | `{"data_class"?: "synthetic_demo" \| "unspecified"}` | `201 {session_id, status, data_class, expires_at}` |
| GET | `/sessions/{id}` | – | `200`: the session plus `workflow` (`run_id`, `thread_id`, `status`, `node_events`, and `clarification {checkpoint_id, round, max_rounds, questions}` while awaiting, or `report_id` / `outcome` once completed) |
| POST | `/sessions/{id}/messages` | `{"message": str}` | `202 {session_id, run_id, status: "running"}` |
| POST | `/sessions/{id}/resume` | `{"checkpoint_id": str, "answers": {field: enum value}}` | `202` |
| POST | `/sessions/{id}/retry` | `{}` | `202` (a new run on the same thread, from the last checkpoint) |
| GET | `/reports/{id}` | – | `200 ComplianceReport` |
| GET | `/reports/{id}/review-cases` | – | `200 {report_id, review_cases: [{id, requirement_id, claim_id (null in M4), reason, status ("open"), created_at}]}`, ordered by requirement id, then reason; `[]` for a report without cases; `404 REPORT_NOT_FOUND` for a malformed, unknown or purged report (M4 S7). Read-only: no decision, approval, rejection, resolution, dismissal or status route exists (D5). |

**Rules:**
- **Idempotency:** `Idempotency-Key` is required on `/messages`, `/resume` and `/retry`.
  - The same key with the same body returns the stored response, and no second run starts.
  - The same key with a different body is `409 IDEMPOTENCY_MISMATCH`.
- **Errors:** every error is `{"error": {"code", "message", ...}}`. Validation errors never echo the input, unexpected errors return `INTERNAL_ERROR`, and request bodies are never logged.
- **Resume security:** every resume follows this order.
  1. Every string and number in the raw body, keys included, is screened (`422 SENSITIVE_DATA_DETECTED`, codes only).
  2. The body shape is checked.
  3. The run must be `awaiting_clarification`, and the `checkpoint_id` must equal the current one (`409 STALE_CHECKPOINT`, checked outside the graph).
  4. The answers go through `resume_command(asked=<the pending question fields>)` (`422 INVALID_ANSWER`).
  5. A compare-and-set claims the run (exactly one concurrent resume wins).
  6. Only then is a `Command` submitted.
  - Nothing from a rejected request reaches the checkpoint, the workflow tables, Redis or the logs.
- **Runs:**
  - **One active run per session** (`409 RUN_IN_PROGRESS`).
  - **Leases:** the Postgres lease is renewed every `RUN_HEARTBEAT_S`.
  - **Retry:** `/retry` is refused while the lease is valid (`409` with `Retry-After`), then takes over an expired or failed run.
  - **Startup:** expired leases are only logged, never re-executed.
- **Threads:** one LangGraph thread per question (S6-D1).
- **Cancellation:** there is no cancel endpoint. A client disconnect never cancels a run, and shutdown is treated like a crash (S6-D2).
- **Ownership:** possession of the session id (S6-D4). There are no user accounts in M3.
- **Retention (M3 S8):**
  - **Phase 1 (`make purge`, at `expires_at`):** the checkpoints and idempotency rows are deleted and any open run is `abandoned`. Writes return `410 SESSION_EXPIRED`. `GET /sessions/{id}`, `GET /reports/{id}` and the SSE replay still work.
  - **Phase 2 (after `REPORT_RETENTION_D` days):** the session, its runs and its reports are deleted, and every endpoint returns `404`.
- **Progress:** `node_events` are recorded in Postgres and streamed over SSE (`GET /sessions/{id}/events`, implemented in M3 S7; see below).

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
## SSE progress events (implemented in M3 S7)
`GET /api/v1/sessions/{id}/events` returns `text/event-stream` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`). Access is possession of the session id, as for every session endpoint. An unknown or malformed id gives `404 SESSION_NOT_FOUND`; an expired session can still read its events.

**Source (S7-D1):** everything sent is read from PostgreSQL: the durable `graph_runs.node_events` of every run in the session, plus each run's current `graph_runs.status`. Redis pub/sub (`chan:session:{id}`) only wakes the stream up. With Redis down or slow, the stream polls Postgres every `EVENTS_POLL_S`, and the workflow is unaffected because publishing is best-effort (`EVENTS_PUBLISH_TIMEOUT_S`, failures counted and never raised).

**Event kinds (S7-D2):** every `data` has exactly `kind`, `run_id`, `seq`, `node`, `status`, `at`. There is never question text, answers, evidence, report content or a report id; the report id comes from `GET /sessions/{id}` once the run is `completed`.
```
id: <run_id>.0.running
event: run
data: {"kind":"run","run_id":"<run_id>","seq":null,"node":null,"status":"running","at":"…"}

id: <run_id>.1
event: node
data: {"kind":"node","run_id":"<run_id>","seq":1,"node":"input_security","status":"completed","at":"…"}
```
- **`node`:** one entry of `node_events`. These are durable and replayable. `status` is `completed`, `failed` (the `error:<Class>` entry) or `recovered` (see below).
- **`run`:** the run's **current** status (`running`, `awaiting_clarification`, `completed`, `failed`, `abandoned`) (S7-D3). This is a current-status notification, not a durable history: a client that reconnects after several transitions receives only the latest status. There is no replay of past statuses.

**Ids and reconnect:** `<run_id>.<seq>` for a node event and `<run_id>.<seq>.<status>` for a run event. They are unambiguous across the runs of a session (one run per question). Send the last id back as `Last-Event-ID` to continue with no gaps and no duplicates. A malformed id, or one naming a run outside this session, restarts from the beginning of the session.

**When the stream closes (S7-D4):** when the latest run is `completed`, `failed` or `abandoned` and everything has been sent; when the client disconnects; or after `SSE_IDLE_TIMEOUT_S` with no new event. It stays open before the first message (open the stream first, then post) and while a run awaits clarification. A `: keep-alive` comment is sent every `SSE_KEEPALIVE_S`. The Redis subscription is taken before the first read and released on every exit path (end, disconnect, idle timeout, cancellation, error).

**Recovered completions (S7-D5):** if a process died after LangGraph checkpointed a node and before its `node_events` entry was written, `/retry` compares the thread's checkpoint history with the completions already recorded by the thread's earlier runs and appends only the missing ones, in execution order, to the new run as `status: "recovered"` before resuming.
Rules: versioned from day one, Pydantic request/response models, consistent error envelope `{error:{code,message}}`, `Idempotency-Key` header required on `/messages`, `/resume` and `/retry`. `/messages` and `/resume` are PII-screened before anything is stored or sent (`422 SENSITIVE_DATA_DETECTED`). Report outcomes and run statuses: `docs/milestones/M3_PLAN.md` §5; no endpoint ever returns an approve/reject decision.
