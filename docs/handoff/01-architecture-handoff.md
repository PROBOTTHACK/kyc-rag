# Architecture and Technical Handoff (M0–M5, as built)

> **Independent educational prototype.** Not affiliated with, endorsed by, or an official system of HDFC Bank or the RBI. It uses public documents and synthetic data only. It never approves or rejects KYC and gives no legal or compliance determination. Every result requires human review.

This document describes the system **as built at the close of M5**.
- **M4** was merged into `main` as `0b65df0` and pushed after closure.
- **M5** (frontend) is complete on stacked branches `feat/m5-s0-plan` … `feat/m5-s7-closure`. It is **not merged and not pushed**, pending the owner's decision ([`M5_CLOSURE_REPORT.md`](../milestones/M5_CLOSURE_REPORT.md)).

The specification documents (`docs/00`–`docs/15`) also describe planned work. Where they differ, this handoff and the milestone handoffs are authoritative about what exists.

## 1. Milestone history
| Milestone | Delivered | Status | Record |
|---|---|---|---|
| **M0** Scaffold | FastAPI app factory, settings, health checks, Docker Compose (Postgres :5433, Redis, Qdrant), async Alembic, Makefile | complete (tag `v0.1.0-m0`) | `docs/milestones/M0_HANDOFF.md` |
| **M1** Document ingestion | Manifest validation (SHA-256), PyMuPDF extraction, Tesseract OCR fallback, section-aware chunking (parent/child), evidence-eligibility and OCR-reliability policies, Postgres persistence, BGE-small embeddings, Qdrant index, reconciliation/repair, filtered dense search, Recall@5 benchmark | complete, merged (`d8d8b7b`) | `docs/milestones/M1_HANDOFF.md` |
| **M2** Retrieval and form intelligence | BM25 and hybrid (RRF) retrieval, registered dev selection and one-time held-out evaluation (dense kept as default), optional cross-encoder reranker (off), deterministic form schemas, Gemini multimodal extraction of 5 approved public blank forms (live-gated), versioned `form_schemas`, Redis read-through cache | complete, merged (`a288b09`) | `docs/milestones/M2_HANDOFF.md` |
| **M3** LangGraph workflow | Groq text-LLM layer (live-gated), rate limiter, LangGraph workflow (input screening, intake with clarification interrupt/resume, retrieval, form analysis, evidence association, synthesis, deterministic output validation, finalize), Postgres checkpointer, workflow API, leases, idempotency, SSE progress, insert-only reports, two-phase retention, acceptance checker, demo | complete, merged (`5c3250d`) | `docs/milestones/M3_HANDOFF.md` |
| **M4** Claim verification and review | Claim and report-v2 models, deterministic verification foundation, owner-reviewed claim datasets, local NLI scorer and registered selection (no configuration qualified), `claim_verification` node (NLI disabled), bounded source-conflict scan and supersession rule, open review cases, M4 acceptance rules, real-corpus end-to-end tests | **complete** (S0–S9), merged (`0b65df0`) | `docs/milestones/M4_PLAN.md`, `docs/milestones/M4_CLOSURE_REPORT.md` |
| **M5** Frontend | Published OpenAPI contract (documentation-only response models; `docs/api/openapi.json`); React + Vite + TypeScript + Tailwind app; typed client with bounded same-key retries and per-code errors; fetch-based SSE with `Last-Event-ID` replay and terminal close; Dashboard, KYC Assistant, Compliance Report (v1/v2) with read-only review cases; session ids in `sessionStorage` (M5-D4) | **complete** (S0–S7); not merged, not pushed | `docs/milestones/M5_PLAN.md`, `docs/milestones/M5_CLOSURE_REPORT.md` |

## 2. Components and responsibilities
| Package (`backend/app/`) | Responsibility |
|---|---|
| `ingestion/` | Pipeline A (PDF → pages → OCR → chunks → Postgres → embeddings → Qdrant), reconciliation, the ingestion CLI (`python -m app.ingestion.cli`), form-schema extraction (deterministic and Gemini) |
| `forms/` | Form-schema building and Gemini extraction pipeline helpers |
| `retrieval/` | Dense (Qdrant), BM25 (Postgres rows), RRF hybrid, optional reranker, evaluation and the M2 held-out runner |
| `llm/` | Provider interfaces; Groq text provider; Gemini multimodal provider with fallback; fakes for tests; opt-in synthetic-only cache |
| `security/` | `screen.py`: deterministic input screening (hygiene, a narrow instruction-override list, ID/financial PII blocking, contact redaction). A pattern filter, **not** a prompt-injection defence |
| `graph/` | LangGraph state (`state.py`), routing, builder, nodes (`graph/nodes/`, one file per node), report building (`report.py`), review-case derivation (`review.py`), corpus reader |
| `verification/` | M4: claim extraction (`claims.py`), premise windows (`premises.py`), statement analysis (`statements.py`), the decision table (`decide.py`), the NLI scorer interface and `CrossEncoderNLI` (`nli.py`), the dev evaluation and registered selection (`evaluation.py`, `selection.py`, `cli.py`), the source-conflict scan (`conflicts.py`) |
| `workflow/` | Workflow service and runtime (API back end), retention purge and integrity check, acceptance checker (`acceptance.py`), demo |
| `db/` | SQLAlchemy models, repositories, workflow persistence (`PostgresReportStore`) |
| `api/v1/` | REST and SSE endpoints; `schemas.py` (M5): documentation-only contract models published in OpenAPI |
| `api/openapi.py` | Writes `docs/api/openapi.json` (`make openapi`); a unit test fails when it is stale |
| `rules/` | Empty package (a rules layer was planned for a later milestone and is not built) |

| Frontend (`frontend/src/`, M5) | Responsibility |
|---|---|
| `api/` | Generated types (`schema.d.ts` from `docs/api/openapi.json`), typed client (`client.ts`), errors with fixed per-code messages, Idempotency-Key tracker, SSE parser and stream controller (`sse.ts`, `events.ts`), React Query hooks |
| `storage/` | Session ids in `sessionStorage` (M5-D4), validated on read |
| `components/` | Layout with the persistent disclaimer, error boundaries, focus helper |
| `screens/` | `dashboard/`, `assistant/`, `report/` |

## 3. Pipelines
### 3.1 Ingestion (M1)
- `manifest.yaml` + PDFs are validated (schema, SHA-256), then:
  - **A** PyMuPDF extraction (physical pages, bookmarks → sections, ruled tables);
  - **B** page quality → Tesseract OCR fallback (cached);
  - **C** section-aware chunking with parent/child split and the OCR evidence policy;
  - **E** one Postgres transaction per document (source of truth);
  - **D** BGE-small embeddings of eligible chunks → Qdrant `policy_chunks` (a derived index);
  - **F** status, run log, integrity and bidirectional reconciliation.
- The corpus is 9 PDFs: RBI and HDFC regulatory, policy, reference and form documents, not redistributed (`docs/14`).
- M1 measured 456 chunks (332 single, 27 parent, 97 child) and 429 Qdrant points.
- Every chunk carries `bank`, `document_key`, page range, `source_status`, `evidence_eligibility`, `text_reliability` and `can_support_mandatory` (= primary AND NOT OCR-uncertain).

### 3.2 Retrieval (M1–M2)
- **Dense (default, `RETRIEVAL_MODE=dense`):** BGE-small, filtered by eligibility.
- **BM25 and hybrid (RRF):** selectable. Hybrid was dev-selected but did not generalise on the one-time held-out run, so dense remains the default (`docs/milestones/M2_RETRIEVAL_OPERATIONAL_DECISION.md`).
- **Cross-encoder reranker:** implemented, `RERANK_ENABLED=false`.

### 3.3 Form intelligence (M2)
- **Deterministic schemas** from stored chunks (`m1_chunks`, 6 documents).
- **Gemini multimodal extraction** of 5 approved public blank forms (`gemini_multimodal`, versioned, never overwritten). Live calls are gated by `GEMINI_LIVE_ENABLED` and a terms date.
- **The workflow uses only stored schemas** and never calls Gemini.
- Form fields mean "the form asks for it" and are **never** policy obligations (`mandatory_under_policy` is always empty and DB-enforced).

### 3.4 LangGraph workflow (M3, extended in M4)
```
START → input_security ─┬─ safe ──→ entity_intake ─┬─ complete → policy_retrieval ─┬─ forms needed → form_analysis ─┐
                        └─ else → finalize          ├─ missing → clarify → input_security   ├─ no forms → evidence_association ←┘
                                                    └─ rounds exhausted → finalize           └─ no eligible evidence / failure → finalize
evidence_association → compliance_synthesis → output_validation ─┬─ non-empty checklist → claim_verification → finalize → END
                                                                 └─ missing/empty checklist → finalize → END
```
- **Outcomes** (by precedence): `BLOCKED` > `INSUFFICIENT_INFORMATION` > `INSUFFICIENT_EVIDENCE` > `DEGRADED` > `CHECKLIST_REVIEW_REQUIRED`. No outcome can express approval or rejection, and `human_review_required` is always true.
- **Limits:** every loop is bounded (clarification ≤ 2 rounds; the recursion limit is checked against the longest legal path, 9 nodes + 3 per round), and every external call has a timeout and backoff.
- **Checkpointing:** Postgres (`langgraph` schema, migration 0007); one thread per question; explicit `/retry` recovery.
- **Synthesis:** an LLM drafts requirements citing supplied evidence ids. **`output_validation`** is deterministic and authoritative:
  - it re-resolves every citation in Postgres and drops uncited items;
  - MANDATORY only with primary, entity-scope, active, non-OCR-uncertain evidence;
  - applicability is never upgraded.

### 3.5 Claim verification (M4)
In the node `claim_verification`, after `output_validation`:
- **Claims:** typed atomic claims per requirement (`policy_obligation`, `form_request`, `supporting_statement`, `explanatory_claim`) from versioned templates and the explanation sentences.
- **Premises:** windows built from the **stored** chunk text, never from model output.
- **Deterministic statement analysis:** modality, polarity and negation, item anchors, subject, entity scope.
- **Local NLI:** premise = evidence window, hypothesis = claim. Scores are uncalibrated logits and softmax values, never "confidence".
- **Decision table** per claim type. Effects **only lower** applicability; MANDATORY additionally needs `mandatory_established`.
- **Production state: NLI disabled.** The registered S4 selection found no qualifying configuration (0 of 140), so `GraphDeps.from_settings` never builds, loads or calls a model. As a result:
  - every claim is `not_verified` (`nli_not_configured`) and nothing is scored;
  - no rule-based contradiction is inferred;
  - items are capped at CONDITIONAL and flagged `not_verified`;
  - a `verification` degradation makes the run DEGRADED. M3 citations, gaps and form links are kept, and M3's applicability is kept as `applicability_before`.
- **The enabled path** exists and is tested only with the scripted `FakeNLI`.

### 3.6 Source conflicts and supersession (M4 S6)
- **The scan:** the bounded scan (`verification/conflicts.py`) compares at most `MAX_CONFLICT_PAIRS` (200) unique cross-document window pairs per run, each in both directions, drawn from evidence already retrieved. It runs **only with a scorer**.
- **In production it is `not_run`.** The report then says no automated conflict check was performed, never "no conflicts".
- **Supersession** (D10) resolves a conflict only with a recorded relation, an active superseding document and a confirmed version review. Dates are never used. The corpus records no relation, so nothing can be resolved.

### 3.7 Reports and human-review cases
- **Report v1** (M3 and any path where verification did not run) and **v2** (M4: `claim_verification`, `verification_meta`, `unverified_items`, `source_conflicts`, M4 notice). Stored v1 reports keep validating unchanged.
- **Reports are insert-only** (a trigger rejects UPDATE), with UUIDv5 ids per session/run.
- **Review cases** (M4 S7, migration 0008):
  - one **open** case per (report, requirement, review flag), written in the same transaction as the report;
  - insert-only and open-only;
  - no decision, approval, rejection or dismissal exists. Reviewer decisions need authentication, in a future security milestone.
  - With NLI off, every checklist item gets one `not_verified` case.
- **Acceptance checker** (`workflow/acceptance.check_report`, used by `make demo` and the end-to-end tests):
  - schema validation first;
  - M3 citation lookups in Postgres;
  - the M4 rules for every verification state (monotonic applicability, provenance, MANDATORY establishment, conflict visibility, scan wording, degradations).

## 4. Data stores
- **PostgreSQL** (source of truth), migrations `0001`–`0008`. Tables:
  - `documents`, `document_pages`, `chunks`, `document_exclusions`, `ingestion_runs` (M1);
  - `form_schemas` (M2, 0003–0005);
  - `kyc_sessions`, `graph_runs`, `reports`, `api_idempotency` (M3, 0006);
  - LangGraph checkpoint tables in the `langgraph` schema (0007);
  - `review_cases` (M4, 0008).
- **Qdrant:** collection `policy_chunks` (derived; rebuilt from Postgres).
- **Redis:** form-schema read-through cache, the shared LLM rate limiter, and a wake-up signal for SSE. Losing Redis only slows events; Postgres stays authoritative.
- **Files:** source PDFs under `data/raw_pdfs/` (gitignored); evaluation datasets and records under `data/eval/` (tracked).

## 5. API (`/api/v1`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | DB, Redis and Qdrant checks |
| POST | `/sessions` | create a session (`synthetic_demo` or `unspecified`) |
| GET | `/sessions/{id}` | session, run status, clarification questions, report id |
| POST | `/sessions/{id}/messages` | start a run (Idempotency-Key) |
| POST | `/sessions/{id}/resume` | answer clarification (enum values, `checkpoint_id`) |
| POST | `/sessions/{id}/retry` | explicit recovery of a crashed run |
| GET | `/sessions/{id}/events` | SSE progress (Last-Event-ID replay) |
| GET | `/reports/{id}` | the stored report (v1 or v2) |
| GET | `/reports/{id}/review-cases` | open review cases (read-only) |

There is no user authentication: possession of the session id is the access model. Details: `docs/07-api-design.md`. **M5:** the request and response shapes are published in `docs/api/openapi.json` (documentation only: routes do not parse bodies or re-serialize, and responses are byte-identical to the service output). The frontend reaches the API through the Vite dev proxy (`/api` → `127.0.0.1:8000`).

## 6. Important configuration (names only; values in `infra/.env.example`, secrets only in a local `.env`)
- **Services:** `DATABASE_URL`, `REDIS_URL`, `QDRANT_URL`, `QDRANT_COLLECTION`.
- **LLM gates (off by default):**
  - `GROQ_LIVE_ENABLED` + `GROQ_DATA_TERMS_VERIFIED_ON`;
  - `GEMINI_LIVE_ENABLED` + `GEMINI_DATA_TERMS_VERIFIED_ON`;
  - model ids from env (`PRIMARY_LLM_MODEL`, `FALLBACK_LLM_MODEL`, `MULTIMODAL_MODEL`, `MULTIMODAL_FALLBACK_MODEL`).
- **Retrieval:** `RETRIEVAL_MODE=dense`, `RERANK_ENABLED=false`.
- **M4:** `NLI_MODEL` (empty), `NLI_MODEL_REVISION`, `NLI_BATCH_SIZE`, `NLI_MAX_PAIRS`, `MAX_CONFLICT_PAIRS`, `NLI_SELECTION_RECORD`. NLI is never enabled from settings in M4.
- **Workflow limits:** `MAX_CLARIFICATION_ROUNDS`, `GRAPH_RECURSION_LIMIT`, `NODE_TIMEOUT_S`, `RUN_LEASE_S`, `SESSION_TTL_H`, `REPORT_RETENTION_D`, `IDEMPOTENCY_TTL_H`.
- **Reserved, not implemented:** `PROMPT_GUARD_MODEL`, `MAX_REGENERATIONS` (there is no regeneration loop, decision D3) and `LANGSMITH_TRACING`/`LANGSMITH_*` (no tracing code).

## 7. Feature status at M5 close
| Status | Features |
|---|---|
| **Operational** | Ingestion and reconciliation; dense retrieval (BM25/hybrid selectable); deterministic form schemas and stored Gemini schemas; LangGraph workflow with clarification, checkpointing, recovery and SSE; input screening (pattern filter); deterministic citation and MANDATORY validation; report v1/v2; claim verification in its **NLI-disabled** mode (honest `not_verified`, DEGRADED); open review cases and their read endpoint; two-phase retention and `check-workflow`; acceptance checker; demo; **the web frontend (M5, local dev server): Dashboard, KYC Assistant, Compliance Report with read-only review cases**; the published OpenAPI contract |
| **Built, infrastructure only (tested, inactive by default)** | `CrossEncoderNLI` scorer, dev evaluation and registered selection; the enabled claim-verification path (scripted scorer in tests); the bounded conflict scan and supersession rule; cross-encoder reranker; live Groq and live Gemini (gated); opt-in synthetic LLM cache |
| **Disabled by evidence** | NLI semantic verification (no configuration qualified in S4); the automated conflict scan in production |
| **Not built** | Prompt Guard and the adversarial suite; a rules layer; reviewer decisions and authentication; LangSmith tracing; a regeneration loop; Document Intelligence and Agent Observability screens (moved to M6); PDF upload and export; the 50-case benchmark (M6); deployment (M7) |
