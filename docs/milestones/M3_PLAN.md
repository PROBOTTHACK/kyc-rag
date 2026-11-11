# M3 Plan: LangGraph end-to-end KYC workflow

> Status: **approved by the project owner (2026-11-11), revision 2.** Committed in Stage S0. No implementation stage has started.
> Independent portfolio prototype. Not an HDFC Bank or RBI system. Synthetic and public data only.

**Revision 2 changes (from the owner's review of revision 1):**
- **PII:** the same screening now applies on both entry points, and clarification answers are structured (§6).
- **Statuses:** run lifecycle and report outcome are now two separate vocabularies (§5).
- **LLM cache:** opt-in, and limited to allowlisted synthetic inputs (§8).
- **Crash recovery and concurrency:** the run lease lives in Postgres, recovery is an explicit retry, and duplicates are handled by idempotency (§9).
- **Evidence boundaries:** association is never an obligation, and the MANDATORY gate is spelled out exactly (§7).
- **Retention:** two-phase purge (§10).

## 1. Context
M0–M2 built the evidence foundation: ingestion with provenance, dense/BM25/hybrid retrieval, and versioned form schemas (deterministic and Gemini). Nothing connects these pieces yet:
- `app/graph/` has only empty files;
- there is no text LLM layer;
- the only API endpoint is `/health`.

The roadmap M3 (docs/12) asks for KYCState, the security, intake, retrieval, form, synthesis and output nodes, conditional routing, a Postgres checkpointer, interrupt/resume for clarification, Redis pub/sub with SSE, and a Groq LLMProvider with fallback, a Redis rate limiter and a cache. **It is done when** an LLP question with missing information pauses, resumes and returns a cited checklist.

M3 builds a traceable, human-supervised workflow, not an autonomous agent. M4 keeps claim-level NLI, review cases, Prompt Guard and the rules table. M3 leaves a seam for each.

## 2. Verified starting point
- **Git:** `main` = `origin/main` = `a288b09` (checked with `git ls-remote`), so the M2 merge is pushed, and the tree is clean. `M2_HANDOFF.md` still says "not merged into main"; S0 fixes that line.
- **Reused:**
  - `Retriever.retrieve` / `RetrievalResult` (degraded, failures, provenance) and `SearchFilters` (`app/retrieval/{retrieve,search}.py`);
  - `evidence_context` / `get_chunks` / `get_document` (`app/db/repository.py`);
  - `load_form_schema_cached` (`app/db/forms.py`);
  - `FormSchema`/`FormField` (`mandatory_under_policy=None`, enforced by the database);
  - the taxonomy enums and `expand_entity_terms`;
  - the `Settings` Groq and limit fields;
  - the `main.py` lifespan (async engine, Redis, Qdrant) and `selector_loop_factory`;
  - the Gemini transport patterns: `RpmLimiter`, backoff, classification, redaction, and transient-only fallback;
  - the test fixtures `temp_db`, `migrated`, `corpus_db` and `corpus_store`, and the fakes.
- **Gaps:**
  - there is no text `LLMProvider`, Groq provider or fake text LLM;
  - the backoff helpers are private to `gemini.py`;
  - the retriever factory is private (`cli._retriever`);
  - retrieval, the repositories and the form cache are sync;
  - there are no workflow tables, checkpointer or API;
  - `rules/`, `security/` and `verification/` are empty;
  - langgraph, the checkpointer and groq are not installed.
- **Corpus fact that drives the demo:** LLP has no RBI CDD paragraph (docs/15), and only the scanned HDFC non-individual form names it. That form is `supporting` and OCR-uncertain. An honest LLP checklist is therefore mostly CONDITIONAL/UNCLEAR, and the acceptance test asserts exactly that.

## 3. Architecture in plain language
1. **The API is the front door.** It checks the session, the idempotency key and the run lease, then screens the input (§6) **before** anything is stored or sent anywhere. That covers both new questions and clarification answers.
2. **The graph is a fixed state machine:**
   - security checks;
   - intake: the LLM extracts what the user *stated*. If something is missing, the graph **pauses** and asks multiple-choice questions;
   - policy retrieval, real hits only;
   - stored form schemas, with **no Gemini call**;
   - deterministic links between form fields and evidence;
   - Groq drafts a checklist that may cite only the evidence IDs it was given;
   - a **deterministic validator** has the final say on citations and on MANDATORY;
   - finalize writes one immutable report with exactly one **outcome**.
3. **Postgres holds all state.** It saves a checkpoint after every node, and the run lease, idempotency records, reports and run logs live there too. **Redis is optional and best-effort:** it carries the rate limiter, the opt-in demo cache and progress pub/sub. Losing Redis never changes a result.
4. **No approve or reject state exists** in any enum, table or API.

```mermaid
flowchart TD
  M[POST /sessions/id/messages] --> GATE
  R[POST /sessions/id/resume<br/>structured answers + checkpoint_id] --> GATE
  T[POST /sessions/id/retry] --> GATE
  GATE[API gate: session active? idempotency key,<br/>Postgres run lease, PII screen] -- sensitive ID detected --> X[422 SENSITIVE_DATA_DETECTED<br/>nothing stored or sent]
  GATE -- ok --> SEC[input_security]
  SEC -- blocked / review --> FIN
  SEC -- safe --> INT[entity_intake]
  INT -- missing & rounds<2 --> CLR[clarify: interrupt()] -.pause.-> R
  INT -- missing & rounds=2 --> FIN
  INT -- complete --> RET[policy_retrieval]
  RET -- no eligible evidence --> FIN
  RET --> FN{forms relevant?}
  FN -- yes --> FORM[form_analysis<br/>stored schema only]
  FN -- no --> ASSOC
  FORM --> ASSOC[evidence_association<br/>relationships, not obligations]
  ASSOC --> SYN[compliance_synthesis<br/>Groq proposes]
  SYN -. M4 seam: claim_verification .-> VAL
  SYN --> VAL[output_validation<br/>deterministic, authoritative]
  VAL --> FIN[finalize: outcome by precedence,<br/>insert-only report]
```
Every node completion is written to `graph_runs` (Postgres), then published to Redis. SSE sends a Postgres snapshot first, then live events (§9).

## 4. State schema (`app/graph/state.py`)
`KYCState(TypedDict, total=False)` holds JSON-safe dicts produced by Pydantic models; nodes re-validate them when they read. The fields:
- **request:**
  - `session_id`, `run_id`;
  - `query` (the screened text);
  - `clarification_rounds` (≤ `MAX_CLARIFICATION_ROUNDS=2`);
  - `clarification_answers: list[ClarificationAnswer]` (structured, §6);
  - `screening: list[ScreenRecord]` (categories and counts only, never the matched text).
- **security:** `security_status` (`safe|blocked|review`), `security_reasons`.
- **intake:**
  - `entity_profile: EntityProfile`, which holds `entity_type`, `residency` and `account_type`, each recorded as `{value | null, source: user_stated | user_answered | unknown}`;
  - `missing_fields`, `ambiguities`, `intake_complete`.
- **retrieval:**
  - `retrieval_query`, `filters`;
  - `evidence: list[EvidenceItem]`, where an item holds the chunk_id, document_id/key, bank, page, page_end, paragraph, section, source_status, evidence_eligibility, text_reliability, can_support_mandatory, rank, score_kind, the public corpus `text` and `expanded_parent_id`;
  - `retrieval_meta`.
- **forms:**
  - `form_analysis_required`;
  - `forms: list[FormRef]`: form_key, method, version, row_id, schema_sha256 and the subset of fields used;
  - `form_flags`.
- **association:** `associations: list[FieldEvidenceLink]` (§7).
- **synthesis:**
  - `draft: ChecklistDraft | None`, `synthesis_attempts` (≤ 2: one call plus one repair);
  - `llm_calls: list[LLMCallRecord]`: provider, model, latency, tokens and cache_hit. No prompts and no keys.
- **outcome:**
  - `degradations: list[Degradation]`;
  - `validation_findings`;
  - `outcome` (§5), `report_id`.

**Bounds:** an explicit `recursion_limit` (`GRAPH_RECURSION_LIMIT=25`) and per-node timeouts. The state holds no secrets and no binaries.

## 5. Statuses: one lifecycle vocabulary, one outcome vocabulary
**Run lifecycle** (`graph_runs.status`), which describes the execution:

| Status | Meaning |
|---|---|
| `running` | holds a live lease (§9) |
| `awaiting_clarification` | interrupted at `clarify`; waiting for `/resume` |
| `completed` | `finalize` committed a report |
| `failed` | an unhandled error after bounded retries; no report. `/retry` is allowed while the session is active |
| `abandoned` | lease expired and taken over by `/retry` (a new run row continues the same thread) |

**Session status** (`kyc_sessions.status`): `active` | `expired`. Whether a session is busy comes from its runs, not from this field.

**Report outcome** (`reports.outcome`, exactly one per report). Every terminal path writes a report. Every report has `human_review_required = true` (a database CHECK) and the fixed notice *"Decision support only. Not a KYC decision. Not claim-verified (M4)."*

| Outcome | When | Checklist? | What the reviewer sees |
|---|---|---|---|
| `BLOCKED` | `security_status` is blocked or review (in M3 a security *review* is not processed; M4 turns it into a review case) | no | reason codes only, never the triggering text |
| `INSUFFICIENT_INFORMATION` | intake still incomplete after 2 clarification rounds | no | the missing fields |
| `INSUFFICIENT_EVIDENCE` | retrieval returned no eligible evidence, or every retriever failed (`RetrievalError`) | no | the query metadata and the failure kind |
| `DEGRADED` | a checklist could not be produced or fully trusted for **technical** reasons: LLM unavailable or invalid after the repair re-ask, a partially failed retriever (`RetrievalResult.degraded`), rerank failure, a form-schema load failure | only if synthesis succeeded, labelled `degraded` | the checklist (if any) plus `degradations` |
| `CHECKLIST_REVIEW_REQUIRED` | the normal path: a validated checklist with no degradation | yes | the checklist, citations, `unresolved_items` and validator findings |

- **Precedence**, deterministic and tested: BLOCKED > INSUFFICIENT_INFORMATION > INSUFFICIENT_EVIDENCE > DEGRADED > CHECKLIST_REVIEW_REQUIRED.
- **`failed`** is a run lifecycle state, not an outcome. It means no report was written.
- **No approve or reject anywhere:** a unit test asserts that no enum, CHECK or API schema contains approve/reject/accept/decline values.

## 6. PII screening (both entry points)
- **Where:** `app/security/screen.py` `screen_text(text) -> ScreenResult(action, categories, counts)`. It runs in the **API gate for `/messages` and `/resume`**, before the idempotency record, the graph state, the checkpoint, the logs or any LLM call. `input_security` runs it again inside the graph as defence in depth; a hit there means BLOCKED.
- **Policy:**
  - **Government and financial identifier patterns block the request:**
    - Aadhaar: 12 digits that pass the Verhoeff check;
    - PAN format;
    - passport-like patterns;
    - card numbers that pass Luhn;
    - long digit runs next to account or IFSC context.
    - The response is `422 SENSITIVE_DATA_DETECTED` with category names only. **Nothing is stored or sent.** The log records category and count only.
  - **Contact patterns** (email, phone) are replaced with `[REDACTED_EMAIL]` / `[REDACTED_PHONE]`, and the request proceeds. The screening record keeps the counts.
- **Clarification answers are structured:** `{checkpoint_id, answers: {entity_type?, residency?, account_type?}}`. The values must be taxonomy enum members, and free text in an answer is rejected. Answers merge deterministically, with **no LLM call on resume**. An optional free-text `note` is **not accepted in M3**.
- **Honesty statement** (in docs, the API description and the report notice): regex screening is a best-effort filter. **It does not guarantee that all personal data is removed.** The real control is the policy that M3 accepts **synthetic data only**. The API and README say so, and the live demo uses only the committed synthetic scripts.
- **Gemini:** never receives user content in M3. The form node reads stored schemas only.

## 7. Evidence association and the MANDATORY gate
- **Association links** (`same_chunk`: the field's chunk was retrieved; `same_section`: same document and section; `topic_match`: shared entity/residency tags plus section-term overlap) are **evidence relationships only**. They mean "this policy passage is related to this form field". **They are never proof of a legal obligation.** They do not count as citations, and they never set `mandatory_under_policy`. M3 writes no form field and leaves that value null.
- **Synthesis applicability is a proposal. `output_validation` is authoritative.** A requirement keeps `MANDATORY` only if **at least one cited evidence id** resolves (through Postgres, not state) to a chunk with all of the following:
  - `evidence_eligibility = primary`;
  - `can_support_mandatory = true`, so not OCR-uncertain;
  - `source_status = active`;
  - a document and page that exist in `document_pages`.
- **Downgrades:** an item supported only by form, supporting, dated, superseded or OCR-uncertain evidence becomes at most `CONDITIONAL` (`UNCLEAR` if it has no resolvable citation). Each downgrade is recorded as a finding.
- **Other validator rules:**
  - unknown evidence ids are stripped and flagged;
  - an applicability is never upgraded;
  - every item carries an uncertainty label.
- **Even a MANDATORY that passes the gate** is labelled "proposed; not claim-verified (M4)".

## 8. LLM layer and cache
- **Files:** `app/llm/provider.py` (an `LLMProvider` protocol with `generate_structured(request, schema) -> LLMResult[T]`), `transport.py`, `groq.py`, `FallbackLLMProvider` and `FakeLLMProvider`.
- **`transport.py`:** backoff and classification extracted from `gemini.py` **without changing Gemini behaviour**. The Gemini tests must pass unchanged.
- **Groq configuration:**
  - model IDs come from env;
  - the IDs, limits and **data-use terms are verified in S0** and recorded in docs/02;
  - live calls need `GROQ_LIVE_ENABLED=true` and `GROQ_DATA_TERMS_VERIFIED_ON`;
  - retries: ≤ 2 with jitter, plus a per-call timeout;
  - the fallback model is used only after transient exhaustion.
- **Rate limiter:** a Redis token bucket (`rl:groq:{window}`, TTL ≤ 120 s). If Redis is down it falls back to the in-process `RpmLimiter`, recorded as a degradation note. This is not an outcome change.
- **LLM response cache:**
  - **Off by default:** `LLM_CACHE_ENABLED=false`.
  - **When enabled, it caches only allowlisted synthetic inputs.** The SHA-256 of the screened, normalised query must appear in the committed `data/eval/m3_synthetic_inputs.jsonl` (demo and benchmark scripts, reviewed as synthetic), and the session must have been created with `data_class=synthetic_demo`. **Regex screening is never the cache decision.** Arbitrary user text is never cached, even when it looks clean.
  - **Key:** `kyc:llm:{sha256(prompt_id|prompt_version|model|input_digest)}`. It is a hash only, with no raw prompt or query. The value is the validated structured output; TTL 24 h.
  - **Every hit is re-validated** with Pydantic. `output_validation` runs on cached and live outputs alike.
  - **Redis error → miss → live (or fake) call.** Correctness does not depend on Redis.
- **LangSmith stays off.** Prompts are never written to state, `graph_runs` or logs.

## 9. Crash recovery and concurrency (no arq)
- **Execution:** the API starts the run as an in-process `asyncio` task and returns `202` with `run_id`. The graph uses `AsyncPostgresSaver`, thread_id = session_id, with a checkpoint after every node.
- **Run lease (Postgres, not Redis):**
  - **The lease:** `graph_runs` has `owner_id` (a per-process UUID), `heartbeat_at` and `lease_expires_at`.
  - **One active run per session:** a **partial unique index** `uq_graph_runs_active ON (session_id) WHERE status IN ('running','awaiting_clarification')` guarantees it.
  - **Heartbeat:** the running task renews every `RUN_HEARTBEAT_S=10` (lease `RUN_LEASE_S=60`).
  - **No Redis lock:** docs/08's `lock:run:{session}` is replaced by this lease (D6), so there is no stale Redis lock to recover.
- **Crash recovery is explicit, not automatic.**
  - **What survives a crash:** after an API crash the last checkpoint is intact in Postgres, and the run row stays `running` with an expiring lease.
  - **What resumes it:** `POST /sessions/{id}/retry` (with an Idempotency-Key). A compare-and-set on `lease_expires_at < now()` succeeds for one caller only. It marks the old row `abandoned`, inserts a new `running` row on the same thread, and resumes the graph from the last checkpoint.
  - **Before the lease expires:** a retry gets `409 RUN_IN_PROGRESS` with `retry_after`.
  - **On startup:** the app only logs stale leases. It never re-executes on its own, which avoids surprise LLM spend and duplicate runs.
  - **Re-running a node:** the node that was in flight re-runs.
    - Retrieval and the deterministic nodes are idempotent.
    - An LLM node may call again (bounded, counted in `llm_calls`).
    - `finalize` uses `report_id = uuid5(run thread, checkpoint id)` and `INSERT … ON CONFLICT DO NOTHING`, so a crash during finalize never creates two reports.
- **Idempotency:**
  - an `Idempotency-Key` header is **required** on `/messages`, `/resume` and `/retry`;
  - table `api_idempotency(session_id, key, endpoint, request_sha256, response_json, created_at)`, UNIQUE(session_id, key);
  - same key and same body → the stored response; same key and a different body → `409 IDEMPOTENCY_MISMATCH`;
  - the request hash is computed over the **screened** body. Blocked requests are not recorded.
- **Concurrent resumes:**
  - `/resume` requires the `checkpoint_id` from the interrupt payload;
  - the transition `awaiting_clarification → running` is a conditional UPDATE, so exactly one resume wins;
  - the other gets `409 RUN_IN_PROGRESS`, or `409 STALE_CHECKPOINT` if the checkpoint has moved on;
  - `/messages` while a run is active or awaiting clarification → `409`.
- **Crash after a checkpoint but before an event:**
  - **Events are notifications, never state.** Each node end is written in order: checkpoint (library) → append to `graph_runs.node_events` with a per-run `seq` → Redis publish.
  - **On connect or reconnect** (`Last-Event-ID`), SSE first sends a **snapshot from Postgres** (node_events after the seq, run status, report id), then live events, de-duplicated by seq.
  - **A lost publish** is therefore repaired on the next connect. An event lost before its `node_events` row was written is reconstructed from the checkpoint history.
- **Why not arq:** the acceptance criteria are pause, resume, a cited checklist and resume-after-crash. Postgres checkpoints, the lease and an explicit retry meet all four in one process. arq would add a worker process but no correctness property M3 needs. **Limitation:** M3 does not restart crashed runs automatically.

## 10. Retention and deletion
- **Settings:**
  - `SESSION_TTL_H=24`: the active window;
  - `REPORT_RETENTION_D=30`: after expiry;
  - `IDEMPOTENCY_TTL_H=24`.
- **Phase 1, at `expires_at`:**
  - the session becomes `expired`, and `/messages`, `/resume` and `/retry` return `410 SESSION_EXPIRED`. **Expired sessions cannot be resumed.**
  - its **LangGraph checkpoint data is deleted by thread_id** (the checkpointer's delete-thread API, verified in S0, otherwise a delete scoped to the `langgraph` schema);
  - its idempotency rows are deleted;
  - any non-terminal run is marked `abandoned` with no report.
  - Reports and `graph_runs` stay readable.
- **Phase 2, at `expires_at + REPORT_RETENTION_D`:** the session row is deleted, and `reports`, `graph_runs` and `api_idempotency` go with it through `ON DELETE CASCADE`. Until then, reports are insert-only; only this purge deletes them. M4 review cases will need their own retention rule.
- **Orphans:** checkpoint threads with no matching session (the library tables have no FK) are deleted by the same purge, and an integrity check reports them.
- **How it runs:** the CLI `workflow purge [--dry-run]` and `make purge`. It is idempotent and runs per session in one transaction. An optional in-process periodic sweep is off by default.
- **Redis:** every key has a TTL.
  - LLM cache: 24 h. Its keys are content hashes, not tied to a session, and hold only allowlisted synthetic-derived output.
  - Rate-limit keys: ≤ 120 s.
  - Form cache: unchanged, 7 d.
  - Pub/sub channels `chan:session:{id}` store nothing. Subscriptions close on the terminal event, on client disconnect and after `SSE_IDLE_TIMEOUT_S=900`, with keep-alive comments.
  - There are no Redis locks.

## 11. Persistence summary
- **Alembic `0006_m3_workflow`**, additive:
  - `kyc_sessions`: id, status, `data_class` (`synthetic_demo|unspecified`), created_at, expires_at, checkpoints_purged_at;
  - `graph_runs`: lifecycle, lease, node_events, llm_calls, latency, and the partial unique index;
  - `reports`: outcome CHECK, `human_review_required` CHECK true, nullable checklist JSON, degradations, findings; insert-only;
  - `api_idempotency`.
- **Alembic `0007_m3_checkpointer`:** calls the library `setup()` in a dedicated `langgraph` schema (D3). `check-schema` excludes that schema.
- **Unchanged:** Qdrant and `form_schemas` are read-only from the graph.

## 12. Stages and acceptance criteria
Each stage is one commit, with its tests, lint and docs; M1/M2 behaviour stays unchanged.

| Stage | Scope | Acceptance criteria and tests |
|---|---|---|
| **S0** Plan & dependency verification | Commit `M3_PLAN.md`; fix the stale M2_HANDOFF line; verify and pin langgraph, langgraph-checkpoint-postgres and groq (each ≥ 2 weeks old, licences in docs/02); verify Groq IDs, limits and data terms, and the checkpointer's delete-thread API; update docs/08 (lease replaces the Redis lock) and docs/07 (resume/retry/idempotency) | Unit 529 and integration 199 green; check-dense-golden, check-schema and lint clean; no behaviour change |
| **S1** LLM layer | provider protocol, transport extraction, Groq provider, fallback, fake, Redis limiter, opt-in allowlisted cache | Gemini tests unchanged and green; retries ≤ 2; timeout; 429 → fallback; refusal not retried; key redacted in errors and repr; invalid JSON → one re-ask; limiter with injected clock; Redis down → in-process limiter with identical results; **cache:** off by default; a non-allowlisted input is never written even when the regex finds nothing; the key contains no prompt or query substring; a corrupt entry is rejected; Redis down → live call, identical validated output |
| **S2** State, routing, skeleton | `state.py`, `routing.py`, `builder.py`, `GraphDeps`, stub nodes, MemorySaver; public `retriever_from_settings` (the CLI uses it) | every routing edge table-tested; **outcome precedence table-tested**; no approve/reject value in any enum; recursion limit enforced; state JSON round-trip; CLI `search` output byte-identical |
| **S3** Screening, security, intake, clarification | `app/security/screen.py`, `input_security`, `entity_intake`, `clarify`, structured answers | synthetic Aadhaar (Verhoeff-valid), PAN and card (Luhn) → 422 on **both** `/messages` and `/resume`, with nothing in state, checkpoint, logs or the fake LLM's recorded inputs; email/phone redacted; legitimate "what is a system prompt?" passes; an LLP query lacking residency/account → interrupt with enum questions; a non-enum answer → 422; a third round → `INSUFFICIENT_INFORMATION`; the LLM never sets a value the user didn't state |
| **S4** Retrieval, forms, association | `policy_retrieval`, `form_analysis`, `evidence_association` | partial retriever failure → a recorded degradation; all fail → `INSUFFICIENT_EVIDENCE`; integration: a TRUST query gives RBI ¶35 with doc and page; the form node makes **zero Gemini calls** (spy) and records method and version; OCR-uncertain fields keep their status; every link is typed as a relationship; `mandatory_under_policy is None` everywhere |
| **S5** Synthesis, validation, reports | versioned synthesis prompt, `ChecklistDraft`, `output_validation`, `finalize`, migration 0006 | MANDATORY with only form, supporting, dated or OCR-uncertain citations → downgraded with a finding; MANDATORY with an association link but no primary citation → downgraded; a fabricated id → stripped; an injection string inside an evidence chunk changes nothing (adversarial fixture); invalid JSON twice → `DEGRADED` without a checklist; a degraded retriever plus a valid synthesis → `DEGRADED` with a labelled checklist; report insert-only; `human_review_required` CHECK; migration up/down/up; check-schema clean |
| **S6** Checkpointer, API, recovery | migration 0007; `POST /sessions`, `GET /sessions/{id}`, `/messages`, `/resume`, `/retry`, `GET /reports/{id}`; lease, heartbeat, idempotency, error envelope | interrupt → **new app instance** → resume continues from the checkpoint; injected crash mid-run → retry before lease expiry 409, after expiry resumes from the last checkpoint, the old row `abandoned`, exactly one report; crash inside finalize → one report; duplicate key → same response, no second run; same key with a different body → 409; two concurrent resumes → exactly one 202; stale `checkpoint_id` → 409; secret and PII scan over checkpoint rows |
| **S7** Events & SSE | `app/core/events.py`; `GET /sessions/{id}/events` with a Postgres snapshot and `Last-Event-ID` | event order = node order with gap-free seq; publish dropped (Redis down) → the run is unaffected and a reconnect snapshot fills the gap; crash after checkpoint and before publish → snapshot shows the node; disconnect frees the subscription; events carry node, status, seq and timestamp only |
| **S8** Retention, E2E, handoff | `workflow purge`, `make purge`; the LLP demo via the API (fake LLM in CI, one opt-in live Groq run with a synthetic script); docs 01/02/03/07/08 and README; `M3_HANDOFF.md` | purge phase 1 deletes the checkpoint thread and idempotency rows and makes the session non-resumable (410); phase 2 cascades; orphan threads removed; dry-run writes nothing. **Done when:** the LLP question pauses, resumes and returns `CHECKLIST_REVIEW_REQUIRED` (or an honestly labelled outcome) in which every item cites doc, page and chunk, no MANDATORY lacks a primary citation, and unresolved items are listed. Full suites, lint, check-schema, check-dense-golden, ingest no-op and a secret scan are all green |

### S0 outcome (2026-11-11)
- **Packages pinned:** `langgraph==1.2.18`, `langgraph-checkpoint-postgres==3.2.0`, `groq==1.7.0`. Every transitive package is at least two weeks old, and the lock diff only adds lines. Details and licences: docs/02.
- **Groq verified:** both gpt-oss models are production models and support strict structured output, and API inputs are not used for training. Free-tier rate limits were **not** confirmed. The published Developer-plan figures conflict (TPM 8K vs 250K). docs/02, "Groq API verification", has the full record.
- **Consequences for later stages:**
  - **S1:** the limiter must respect a per-minute token budget as well as RPM, or prompts must be capped.
  - **S6:** must show that `setup()` and `adelete_thread()` work inside the `langgraph` schema through `search_path`, because the library uses unqualified table names.
- **Regression:** with docker services up and `TESSDATA_DIR` and `GEMINI_API_KEY` unset (as in M2 D), unit 529 passed, integration 199 passed, and `lint`, `check-schema` and `check-dense-golden` were green.
  - With the repo-root `.env` value `TESSDATA_DIR`, one unit test (`test_config.py::test_corpus_paths_resolve_against_repo_root`) fails. It was already environment-dependent before M3 and has nothing to do with the new packages.

### S1 outcome (2026-11-11)
- **Built:** `app/llm/{common,transport,provider,groq,cache}.py`, `app/core/ratelimit.py`, `FakeLLMProvider` / `scripted_llm`, plus the new settings listed in `infra/.env.example`.
  - Groq live calls are off unless `GROQ_LIVE_ENABLED` and `GROQ_DATA_TERMS_VERIFIED_ON` are set.
  - The LLM cache is off by default.
- **M2 unchanged:** the provider errors and `parse_structured` moved to `common.py`, and the retry helpers to `transport.py`. `multimodal.py` and `gemini.py` re-export the same objects. All Gemini, multimodal and forms tests pass unmodified.
- **Decisions taken while building:**
  - **Groq JSON schema:** best-effort mode (`strict: false`). Groq's strict mode needs every property required, so the S3/S5 output models may opt in later. Validation and the single re-ask apply either way.
  - **Schema rejections:** Groq's `400 json_validate_failed` is treated as invalid output, not as an error, so it goes through the re-ask.
  - **Token budget:** the limiter reserves estimated tokens (prompt characters / 3 plus the output cap) as well as requests. A request larger than the per-minute budget fails at once.
  - **`generate_text`:** not implemented. Every workflow call is structured.
- **Tests:** 69 new unit tests (Groq 32, contract/fallback/re-ask/transport 15, limiter 10, cache 12) and 3 Redis integration tests. Unit 598 passed and integration 202 passed (`TESSDATA_DIR`/`GEMINI_API_KEY` unset). `lint` (strict mypy, 79 files), `check-schema` and `check-dense-golden` are green.
- **No live Groq call was made.**

### S2 outcome (2026-11-11)
- **Built:**
  - `app/graph/state.py`: JSON-safe `KYCState`, nested Pydantic models, `validate_state`, and the `SecurityStatus`, `Outcome`, `RunStatus` and `LinkKind` vocabularies;
  - `app/graph/routing.py`: pure, fail-closed routing plus `decide_outcome` precedence;
  - `app/graph/builder.py`: `GraphDeps`, `build_graph`, `run_config` and `topology`;
  - nine stub nodes in `app/graph/nodes/`;
  - `app/retrieval/factory.py` (`retriever_from_settings`).
- **New settings:** `MAX_CLARIFICATION_ROUNDS=2` and `GRAPH_RECURSION_LIMIT=25`.
- **CLI unchanged:** search output is byte-identical to the pre-refactor baseline (dense, bm25, hybrid, entity-filtered mandatory-only, and dense + rerank). Each case was run twice before and twice after the change.
- **Decisions taken while building:**
  - **No refusal node:** the revision-2 diagram is authoritative, so a blocked or review security status routes to `finalize`, which records `BLOCKED`.
  - **Recursion limit:** LangGraph counts the input as a super-step, so a path of n nodes needs `recursion_limit` n + 1. The longest legal path is 8 + 2 x rounds nodes, so 2 rounds need 13. `GraphDeps` refuses any lower limit, and the default is 25.
  - **Stubs:** they make no decisions. Routing fails closed, so an empty state ends `BLOCKED`. The `clarify` stub only advances the bounded round counter, and S3 replaces it with `interrupt()`. The `finalize` stub only computes the outcome; it persists nothing.
  - **Synthesis flows straight to validation:** `compliance_synthesis` always continues to `output_validation`, which in S5 handles a missing draft (giving `DEGRADED`). The M4 claim-verification seam sits between them.
  - **Partial degradation:** a partially degraded retrieval still proceeds, and `decide_outcome` reports it as `DEGRADED`.
- **Tests:** 86 new unit tests:
  - routing: every conditional edge, cross-checked against the compiled topology;
  - outcome precedence, and no approve/reject vocabulary anywhere;
  - state JSON and checkpoint round trips;
  - the topology snapshot `tests/fixtures/m3_graph_topology.json`;
  - recursion-limit boundaries checked against LangGraph;
  - factory wiring and laziness.
- **Results:** unit 684 and integration 202 passed. `lint` (strict mypy, 92 files), `check-schema` and `check-dense-golden` are green.

## 13. Dependencies and configuration
- **New packages (D2):** `langgraph`, `langgraph-checkpoint-postgres` (pulls in `psycopg-pool`, `langchain-core` and `langsmith`) and `groq` (imported lazily).
- **Not added:** sse-starlette (`StreamingResponse` is enough) and arq (§9).
- **No new services:** Postgres gains a `langgraph` schema.
- **New env keys:** `GROQ_LIVE_ENABLED`, `GROQ_DATA_TERMS_VERIFIED_ON`, `GROQ_TIMEOUT_S`, `GROQ_MAX_RETRIES≤2`, `LLM_CACHE_ENABLED=false`, `LLM_CACHE_TTL_S`, `GRAPH_RECURSION_LIMIT`, `MAX_CLARIFICATION_ROUNDS=2`, `RUN_LEASE_S`, `RUN_HEARTBEAT_S`, `SESSION_TTL_H`, `REPORT_RETENTION_D`, `IDEMPOTENCY_TTL_H`, `SSE_IDLE_TIMEOUT_S`.
- **Before any live run:** align the M2 `.env` model drift.

## 14. Risks: security, privacy, reliability, compliance
- **Privacy:**
  - **Screening is not a guarantee.** The synthetic-only policy, blocking ID patterns, structured answers and the absence of free-text resume reduce exposure; they do not eliminate it.
  - **Groq** receives only the screened query and public corpus text. Its live gate requires recorded terms verification.
  - **Gemini** receives no user content.
  - **LangSmith** stays off.
  - Checkpoints are purged at session expiry.
- **Security:**
  - Evidence is delimited and treated as data, and the LLM has no tools. An adversarial fixture covers evidence injection.
  - Prompt Guard and the full attack suite are M4, and M3 documentation must not claim them.
  - API errors never echo the input.
- **Compliance:**
  - Only the deterministic gate yields MANDATORY, and it never upgrades.
  - Association links are never obligations.
  - Every outcome requires human review.
  - No approve or reject state exists.
  - Reports carry "not claim-verified (M4)".
- **Reliability:**
  - Postgres-only correctness: Redis loss means slower or no live events, never a different result.
  - Recovery is explicit, and a re-run node may make one more LLM call.
  - The partial unique index plus compare-and-set prevents double runs.
  - Finalize is idempotent.
  - All loops are bounded: clarification 2, synthesis 1 + 1, transport retries 2, recursion limit.
- **Technical:**
  - **Sync/async:** sync retrieval runs in `asyncio.to_thread`.
  - **Windows:** the selector loop is required for the checkpointer.
  - **LangGraph:** API churn is handled by exact pins and an isolated `builder.py`.
- **Evidence quality:**
  - **Thin LLP evidence:** an honest CONDITIONAL/UNCLEAR outcome is expected and documented, not tuned.
  - **Small retrieval benchmarks:** they stay visible as degradations and unresolved items.

## 15. Decisions (final table)
| # | Decision | Recommendation | Alternative |
|---|---|---|---|
| D1 | M3/M4 boundary | M3: deterministic security, PII screen, synthesis and a deterministic validator; **no** NLI, review_cases or Prompt Guard (M4) | Pull NLI into M3 (scope growth) |
| D2 | New packages | `langgraph`, `langgraph-checkpoint-postgres`, `groq`; no sse-starlette, no arq | httpx to Groq's OpenAI-compatible API instead of the `groq` SDK |
| D3 | Checkpoint tables | library `setup()` called from Alembic `0007`, in the `langgraph` schema; check-schema excludes it | a separate `make` target outside Alembic |
| D4 | Form source in the workflow | latest complete `gemini_multimodal` row, else `m1_chunks`, always recorded; never live Gemini | `m1_chunks` only |
| D5 | Branching | stacked sub-branches as in M2 (`feat/m3-s0-plan` → … → `feat/m3-s8-e2e`), one commit per stage, merge to `main` after the handoff | one `feat/m3-workflow` branch |
| D6 | Run lock | Postgres lease plus a partial unique index (replaces docs/08 `lock:run:{session}`) | Redis lock with TTL (needs stale-lock recovery; Redis becomes correctness-critical) |
| D7 | PII policy | block on ID/financial patterns (422, nothing stored); redact email/phone; structured enum-only clarification answers | block on every pattern |
| D8 | LLM cache | off by default; when on, only allowlisted synthetic inputs (`data_class=synthetic_demo` + committed hash list) | no LLM cache in M3 |

## 16. `docs/milestones/M3_PLAN.md` outline
1. Status and disclaimer
2. Context and roadmap
3. Verified starting point
4. Architecture and diagram
5. State schema
6. Statuses (lifecycle vs outcome)
7. Nodes and routing
8. PII screening
9. Evidence association and MANDATORY gate
10. LLM layer and cache
11. Crash recovery and concurrency
12. Retention and deletion
13. Persistence and migrations
14. Stages S0–S8 and acceptance criteria
15. Dependencies and configuration
16. Risks
17. Decisions
18. Out of scope / M4 seams
19. Verification and demo path

## Verification (end of M3)
1. `make up migrate check-schema ingest forms`
2. `make test test-integration lint check-dense-golden`
3. `make api`
4. Run the synthetic LLP script:
   - create a session (`data_class=synthetic_demo`);
   - POST the message (with an Idempotency-Key);
   - SSE shows `CLARIFICATION_REQUIRED`;
   - resume with enum answers;
   - `GET /reports/{id}` returns the outcome and citations; check them against `search` and `forms show`.
5. Crash drill: kill the API mid-run, restart it, `/retry` after the lease expires, and confirm exactly one report.
6. `make purge` with `--dry-run`, then for real.
7. Secret and PII scan over checkpoints, `graph_runs`, reports and Redis.
