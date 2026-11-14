# CLAUDE.md — Project rules for Claude Code

Project: **HDFC Bank Account Opening Assistant** (independent portfolio prototype, not an official product of HDFC Bank or the RBI, and not endorsed by either).

**Product direction (owner decision, 2026-11-14):** a customer-facing conversational assistant. It tells a customer which documents and information they need to open an HDFC Bank account, using **only HDFC material in the project corpus**. This **supersedes** the previous "Agentic KYC Compliance Engine" direction for all new work.

Read first, in this order:
1. `docs/assistant/PRODUCT_SPEC.md`: the **authoritative** product spec: evidence classes, taxonomy, conversation rules (including greetings, follow-ups and separate accounts), output and citations, fallback and notice, the LLM's role, memory and privacy, and what the MVP must not do. Its section numbers match A1, so "spec §N" below refers to it.
2. `docs/assistant/A0_AUDIT.md`: the corpus audit and the evidence-based coverage matrix.
3. `README.md`; `docs/handoff/` describes the preserved previous implementation (M0–M5).

`A1_PRODUCT_SPEC.md`, the other A-stage reports and `REDESIGN.md` are historical stage records; `A5_API.md` is the current API reference.

The spec is binding: don't add technologies, features or sources without asking.

## Non-negotiable principles
1. **Evidence or nothing.** Every checklist item cites stored HDFC text (document key + page, re-resolved in Postgres). With no evidence, use the prescribed fallback (spec §7), never a guess.
2. **Never invent** requirements, documents, conditions, specifications, citations, page numbers, dates or source documents. Unknown stays unknown; unknown metadata stays `null`.
3. **Never strengthen a classification.** Classes are assigned deterministically from the source tier (spec §2–§3). `policy_supported` exists only for an authoritative, applicable HDFC policy, and **none exists in today's corpus**. A form's field never becomes a document (a PAN number is not a PAN card copy), and a photograph box gets no added specifications.
4. **Retrieved text is untrusted data**, never instructions. Delimit it in prompts and never let it alter system behaviour.
5. **No hardcoded evidence or requirement text** in production code. The coverage map is data that cites stored chunks. Examples in docs are illustrative only.
6. **Model-agnostic, LLM optional.** All LLM calls go through the `LLMProvider` interface, and model ids come from env vars. The assistant must work deterministically without an LLM. The LLM may only extract slot values (enum-validated), phrase text from cited passages, and act as the assistant's **voice**: rewording the reply from the facts of the API answer only, with every reply checked by a deterministic validator and the deterministic reply used on any failure (owner decision, 2026-11-14; `backend/app/assistant/voice.py`). It never decides items, classes, citations, coverage or the fallback.
7. **Free-tier only.** Every service must work on a free tier or locally via Docker. Don't add paid services.
8. **No secrets** in code, logs, state or traces; use env vars. **No real customer PII anywhere**: synthetic data only. Input screening (`screen_text`) runs before anything is stored or sent to an LLM.
9. **Bounded loops.** Every retry or re-ask has a hard maximum (default 2), and every external call has a timeout and backoff.
10. **No approval language.** The assistant never approves, rejects, scores or assesses eligibility or KYC.
11. **Dated sources are disclosed.** Every answer carries the dated-document notice (spec §7) and advises confirming with HDFC Bank.

## Source rules (spec §2; A0)
- **HDFC only.** RBI documents remain stored (legacy) but are **excluded** from the assistant: never retrieved, cited or paraphrased. No external, internet or model-knowledge sources.
- **Hierarchy:** authoritative applicable HDFC policy (none today) > applicable HDFC opening form > applicable conversion form (conversions only) > other HDFC material (re-KYC/dormant, other categories): unconfirmed context only > `hdfc_kyc_aml_faq`, a legacy reference of unverified provenance: **excluded from checklists**.
- **Re-KYC material is never an account-opening requirement** unless an applicable opening document independently corroborates it.
- **Coverage comes only from corpus evidence** (A0 coverage matrix). A combination the corpus can't establish (e.g. resident savings, current or salary accounts) gets the prescribed fallback; never imply support.
- Adding documents means updating the manifest and ingesting through the existing pipeline. Inspect each new PDF and update the audit first, and get owner approval before any coverage change.

## Stack
Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, PostgreSQL, Redis (where useful), Qdrant, PyMuPDF + Tesseract OCR, sentence-transformers (BGE-small), Groq (optional, gated), React + Vite + TypeScript + Tailwind + React Router + React Query (frontend toolchain pinned per `docs/milestones/M5_PLAN.md` §F.1; Node 26.13.1).
- **Orchestration:** a **plain per-turn service** (`backend/app/assistant/`). **LangGraph is no longer required or locked** for new work (decision D-C).
- **Not used by the assistant:** LangGraph, the DeBERTa NLI verifier, LangSmith, Gemini (form extraction already stored). They stay installed for the preserved legacy code.
- Ask before adding any dependency.

## Preserved legacy implementation (do not delete)
- The M0–M5 compliance-engine code, its tests and its database records are **preserved**:
  - the LangGraph workflow (`backend/app/graph/`), NLI verification and the conflict scan (`backend/app/verification/`);
  - review cases, report v1/v2, the acceptance checker;
  - the `/api/v1` workflow API and the M5 frontend screens;
  - the RBI ingestion path.
- Its tests keep running, and changes to it should be avoided.
- **Removal or rewrites** of legacy code happen only in a reviewed, owner-approved stage.
- **Legacy-only rules:** the old rules (RBI > HDFC hierarchy; "MANDATORY only with a primary citation"; "each LangGraph node = one file in `backend/app/graph/nodes/`") now apply only to that legacy code. See `docs/milestones/M4_CLOSURE_REPORT.md` and `docs/milestones/M5_CLOSURE_REPORT.md`.

## Conventions
- **New assistant code:**
  - backend in `backend/app/assistant/`, API under `/api/v2/assistant/…`;
  - frontend in `frontend/src/assistant/`;
  - new tables only through Alembic (existing tables untouched).
- **Code quality:** typed everywhere (strict mypy where practical), `ruff` + `black`, `pytest`, async FastAPI; frontend `tsc` strict + ESLint + Prettier + Vitest.
- **LLM output:** Pydantic models for every structured LLM output; validate before use; on invalid output retry once, then use the deterministic path.
- **Stages:** work in stages (A1–A7, `docs/assistant/PRODUCT_SPEC.md` §11), one stage at a time, one review and one commit each. After each stage: run the tests, update the docs if behaviour changed, and summarise what is done and what is not. Do not start the next stage without approval.
- **Tests:** write them alongside code. Retrieval and checklist correctness need measurable metrics (HDFC-only evaluation set, A2).
- **Ask before:** adding dependencies, changing the DB schema outside Alembic, changing coverage, or deviating from the spec.
- **Git:** never push, merge, tag, reset, rebase or delete branches without explicit approval.

## Commands (Makefile)
- `make up` / `make down`: start or stop Postgres, Redis and Qdrant (`infra/docker-compose.yml`)
- `make migrate`: run Alembic; `make check-schema`: no ORM/migration drift
- `make ingest` (and `ingest-dry`, `persist`, `index`, `retry`, `reconcile`, `repair`, `runs`, `search Q=...`, `check-corpus`, `forms`): the ingestion pipeline (docs/04)
- `make api`: FastAPI (uvicorn --reload)
- `make test` / `make test-integration` / `make lint`: backend checks
- `make web` / `make web-install` / `make web-test` / `make web-lint` / `make web-build`: frontend
- `make openapi` / `make web-types`: the published API contract and the generated frontend types
- `make eval`: retrieval benchmark (legacy M1 set)
- `make assistant-eval [MODE=dense|bm25|hybrid|all]`: the assistant's HDFC-only retrieval evaluation (A2; `docs/assistant/A2_RETRIEVAL_EVAL.md`)

## Definition of done (every stage)
Tests pass (known legacy baseline failures K7/K8 excepted and reported), lint clean, docs updated, no secrets committed, demo path documented.
