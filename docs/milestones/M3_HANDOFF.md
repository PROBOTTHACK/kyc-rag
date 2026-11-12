# M3 Handoff: the end-to-end LangGraph KYC workflow

> Independent portfolio prototype. It is **not** an HDFC Bank or RBI system, it uses synthetic data only, it never approves or rejects KYC, and every result requires human review.

**Status:** M3 is complete (stacked branches S0 → S8) and was merged into `main` on owner approval (D5, merge commit `5c3250d`).
**Plan and per-stage outcomes:** [`M3_PLAN.md`](M3_PLAN.md). **Previous milestone:** [`M2_HANDOFF.md`](M2_HANDOFF.md).

## 1. What M3 delivers
A question about KYC documents goes through the API into a checkpointed LangGraph workflow. The workflow:
1. screens the question;
2. extracts the entity profile, asking enum-only clarification questions when needed (the run pauses and resumes);
3. retrieves cited policy evidence;
4. analyses the stored form schemas;
5. drafts a checklist with an LLM;
6. validates every citation and every applicability deterministically;
7. stores an insert-only report that always requires human review.

Progress streams over SSE. Crashed runs are recovered explicitly. Expired data is purged in two phases.

**The rule throughout: "No evidence, no answer".** An unsupported item is dropped or downgraded, never invented. When nothing survives, the report says so and refers the user to https://www.hdfcbank.com/ or HDFC Bank customer service. Missing evidence is never presented as "not required".

## 2. Stages and commits
| Stage | Commit | Delivered |
|---|---|---|
| S0 | `8f3c8c9`, `8216e6b` | approved plan; pinned `langgraph` 1.2.18, `langgraph-checkpoint-postgres` 3.2.0, `groq` 1.7.0; Groq terms verified |
| S1 | `4cf2856` | text-LLM layer (Groq, live-gated), shared Redis rate limiter (RPM + TPM), opt-in synthetic-only LLM cache |
| S2 | `3955702` | graph foundation: state, routing, builder, topology snapshot |
| S3 / S3.1 | `27d5a69`, `81aee8c` | input security and PII screening, entity intake with span grounding, clarification interrupt/resume, Indic text, node timeout |
| S4 | `0583b23`, `f876b4d` | policy retrieval (entity pass + entity-group pass), form analysis on stored schemas, evidence association (links are never obligations) |
| S5 | `6efa739` | compliance synthesis, deterministic output validation, outcomes, insert-only reports (migration 0006) |
| S6 | `17a1c7b` | Postgres checkpointer (0007), workflow API, leases, idempotency, explicit `/retry` recovery, resume security |
| S7 | `8720dc2` | progress events: Postgres `node_events` streamed over SSE, Redis as a wake-up only; recovered events; checkpoint-id screening fix |
| S8 | `402e453` + docs commit | retention purge, M3 acceptance checker, end-to-end test on the real corpus, `make demo` / `make demo-live`, the live Groq run, a logging fix |

## 3. How to run it (the demo path)
```
make up && make migrate && make ingest          # if not already ingested
make api                                         # terminal 1
make demo                                        # terminal 2: no live LLM → honest DEGRADED report
# live Groq (owner opt-in): GROQ_LIVE_ENABLED=true + GROQ_DATA_TERMS_VERIFIED_ON=<date> in .env,
# restart make api, then:
make demo-live                                   # afterwards set GROQ_LIVE_ENABLED=false
make purge-dry && make purge && make check-workflow
```
- **Endpoints** (docs/07):
  - `POST /api/v1/sessions`;
  - `POST …/messages` and `…/resume` (`checkpoint_id` plus enum answers) and `…/retry` (each needs an `Idempotency-Key`);
  - `GET /sessions/{id}`, `GET /sessions/{id}/events` (SSE), `GET /reports/{id}`.

## 4. Results
- **Tests at the end of M3:** **unit 969, integration 267**. In the final run, integration was 267/267 and unit 968/969. The one failure, `test_groq_provider.py::test_app_imports_without_the_groq_sdk_or_a_key`, starts a fresh process that reads the repo-root `.env`, and the owner's live gate (`GROQ_LIVE_ENABLED=true`) was still open after the live run. It passes once the gate is set back to `false` (owner action 1). It is the same kind of `.env` dependence as the known `TESSDATA_DIR` case (S0), not a code change.
- **Checks:** lint (ruff, black, strict mypy on 112 files), `check-schema` (no drift; migrations 0006 and 0007), `check-dense-golden`, the topology snapshot, `make ingest-dry` (every document unchanged) and a secret scan of tracked files are all green.
- **CI end to end** (`tests/integration/test_m3_e2e.py`): the LLP question goes through the API on the real ingested corpus (BM25, stored form schemas, a scripted LLM citing the evidence retrieval actually returned, plus a bogus id). It pauses for `account_type`, resumes and passes `check_report`, the same checker the live demo uses: every chunk id, document key and page is looked up in Postgres, and no MANDATORY lacks a primary citation. The bogus id is stripped.
- **Demo without an LLM (dev stack):** the run paused (both questions), resumed and completed with 15 SSE events. The outcome was **DEGRADED** (`llm: not_configured`), evidence partial, 2 unresolved items, and the checker passed.
- **Live Groq demo (2026-11-12, owner-authorised, the only live session):**
  - **Calls:** two, intake and synthesis, both `openai/gpt-oss-120b`, both ok: 2.2 s and 3.2 s, 623/312 and 2886/1093 tokens. No fallback, no cache hit.
  - **Clarification:** intake grounded `LLP` and asked only for `account_type` (answered `CURRENT_ACCOUNT`).
  - **Outcome:** **`CHECKLIST_REVIEW_REQUIRED`**, evidence status `partial`, with 2 items:
    - "Form 60/61 (existing customers)", citing `hdfc_non_individual_account_opening` p.1 (supporting, entity scope);
    - "PAN Number", citing `hdfc_non_individual_account_opening` p.1 (supporting).
  - **Applicability:** the model proposed both as **MANDATORY**. The validator downgraded both to **CONDITIONAL** because only form or supporting evidence backs them (`mandatory_downgraded:only_form_or_supporting`).
  - **Unresolved items:** `no_entity_specific_primary_evidence`, `primary_evidence_only_via_entity_group`, `mandatory_downgraded:only_form_or_supporting`.
  - **Forms:** analysed from the stored Gemini schema of the same form (60 fields, `subset_only=true`).
  - **Acceptance:** passed, with 0 MANDATORY items, 2 citations and 3 unresolved items.
  - **Reading it:** this is the honest outcome on the corpus's thin LLP evidence (§14 of the plan), recorded as-is and not tuned.

## 5. Safety, privacy and security guarantees and their limits
- **Data:**
  - synthetic data only;
  - PII screening blocks ID and financial patterns before anything is stored (`422`, nothing written to checkpoints, tables, Redis or logs, verified by byte-level scans);
  - clarification answers are enum values merged without an LLM call;
  - Groq receives only the screened query, the enum context and public corpus text;
  - **Gemini receives nothing in M3** (forms come from stored schemas);
  - LangSmith is off.
- **Resume security:** raw-body screening → shape → awaiting run + current `checkpoint_id` (a lowercase UUID; the only value exempt from screening, S7 fix) → `resume_command(asked=…)` → idempotency → compare-and-set claim → the single submission point (`WorkflowService._execute`, AST-guarded).
- **Compliance:**
  - only the deterministic validator yields MANDATORY, and it never upgrades;
  - association links are never obligations;
  - there is no approve/reject state;
  - reports carry "Not claim-verified (M4)".
- **Reliability:**
  - Postgres is authoritative, and losing Redis only slows events;
  - leases plus a partial unique index prevent double runs;
  - finalize is idempotent;
  - every loop is bounded.
- **Retention:**
  - two phases, as detailed in docs/08;
  - checkpoint data is deleted and verified inside the same locked transaction as the session change;
  - a live writer is never purged under;
  - orphans are removed across every checkpoint table.
- **Limits:**
  - screening reduces exposure but is not a guarantee;
  - explanations are model prose and not claim-verified (NLI is M4);
  - Prompt Guard and the attack suite are M4;
  - there is no user authentication (possession of the session id is the access model);
  - crashed runs are not restarted automatically;
  - run-status SSE events are current status only;
  - a re-run node may make one more LLM call;
  - Groq retains logs for up to 30 days by default (docs/02).

## 6. Issues found and fixed during M3 (most recent first)
- **S8, prompt in dev logs:** with the dev DEBUG root level, `groq._base_client` logged the full request (the prompt) during the live run. No key was logged. `app/core/logging.py` now holds payload-logging libraries (groq, httpx, httpcore, google-genai, huggingface_hub, …) at WARNING in every environment; there is a regression test; the local log was deleted.
- **S8, AST guard collision:** the S6 guard on graph submission flagged the demo's `httpx` `client.stream(...)`. The guard was kept strict, and the demo uses `send(stream=True)`.
- **S7, checkpoint-id false positive:** about 2% of UUIDs pass the card Luhn check, so valid resumes could be refused. `checkpoint_id` must now be a lowercase UUID and is the only value exempt from screening.
- **S6, Alembic `fileConfig`:** it disabled the app loggers in test fixtures, so the fixtures re-enable them (log-leak assertions are real).

## 7. Decisions (owner-approved)
- **Plan level:**
  - **D1:** M3/M4 boundary (no NLI, Prompt Guard or review cases in M3).
  - **D2:** the packages listed above.
  - **D3:** checkpoint tables in the `langgraph` schema via the library `setup()` in Alembic.
  - **D4:** stored form schemas, never live Gemini.
  - **D5:** stacked branches, merged after the handoff.
  - **D6:** a Postgres lease, not a Redis lock.
  - **D7:** block ID/financial PII, enum-only answers.
  - **D8:** the LLM cache is off, and synthetic-allowlist only.
- **Stage decisions:**
  - **S4-D1:** an entity-group pass only when the entity pass has no primary chunk. `evidence_status=found` does not mean relevance was verified.
  - **S5-D1..D4:** uncited items dropped; nothing surviving gives INSUFFICIENT_EVIDENCE with the fallback text; uuid5 report ids; four tables.
  - **S6-D1..D5:** one thread per question; no cancel endpoint; `node_events` in Postgres; possession of the id; the live-gated LLM.
  - **S7-D1..D5:** Postgres-sourced SSE; `kind` + `run_id`; current-status run events; close rules; recovered completions on retry.
  - **S8-D1..D5:** CLI-only purge; skip live leases; idempotency TTL; demo over real HTTP; live record summarised here, with the full JSON kept locally in the gitignored `data/processed/m3_demo/`.

## 8. Carry-forward to M4 and later
- Claim verification (DeBERTa NLI) for every requirement explanation, and `review_cases` for human review.
- Prompt Guard and the adversarial suite (evidence injection is covered by a fixture in M3 only).
- LLP and other non-individual evidence is thin. NRI evidence is partial. The LLP group scope is never MANDATORY. The 60-field form cap stays visible (`subset_only`).
- A missing form schema stays distinct from a retrieval failure.
- Groq rate limits per organisation were not confirmed (docs/02). The limiter stays at RPM 20 / TPM 6000.
- Before any further live Gemini work, make the `.env` fallback `MULTIMODAL_FALLBACK_MODEL=gemini-3.5-flash-lite` (the verified M2 value).
- Optional: an in-process periodic purge (not built, by decision S8-D1). Run-status history in SSE would need a migration.

## 9. Owner actions after M3
1. Set `GROQ_LIVE_ENABLED=false` in `.env` (the live run is done).
2. Review this handoff, then approve or decline merging `feat/m3-s8-e2e` into `main`.
3. Optional: `make purge` after the demo sessions expire (24 h), or leave them for review.
