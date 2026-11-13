# M5 Plan: Frontend (KYC Assistant, Compliance Report, Review Cases)

> Independent educational prototype. Not an HDFC Bank or RBI system. Synthetic data only. The UI never approves or rejects anything, and every result requires human review.

**Status:** S0. The plan and decisions M5-D1 to M5-D3 were approved by the owner on 2026-11-13. M5-D4 is **open** and must be decided before S6. Stages run one at a time on the branch `feat/m5-s0-plan` and its successors, with one review and one commit per stage. **Previous milestone:** [`M4_CLOSURE_REPORT.md`](M4_CLOSURE_REPORT.md).

## A. Why M5 is the frontend
- docs/12 defines "M5 — Frontend": "Build the 5 screens + review queue per docs/09 with typed API client and SSE progress. Done: full demo flow in browser."
- The owner decision of 2026-11-12 (M4_PLAN.md, S0 outcome) keeps M5 reserved for the frontend. The deferred security work (Prompt Guard, rules layer, adversarial suite, reviewer decisions with authentication) is a "Future security milestone — number TBD".
- M6 is evaluation and observability. M7 is packaging and deployment.

## B. Current-state audit (M5 S0, read-only, `main` @ `0b65df0`)
- **Frontend:** `frontend/` holds only `.gitkeep`. `make web` refuses on purpose (Makefile).
- **Backend API that exists** (`backend/app/api/v1/sessions.py`, `health.py`):
  - `GET /health`;
  - `POST /sessions`;
  - `GET /sessions/{id}`;
  - `POST /sessions/{id}/messages`, `/resume`, `/retry` (each needs an `Idempotency-Key`);
  - `GET /sessions/{id}/events` (SSE);
  - `GET /reports/{id}`;
  - `GET /reports/{id}/review-cases`.
- **CORS:** `FRONTEND_ORIGIN` (default `http://localhost:5173`) is already configured in `app/main.py`.
- **Untyped contract:** every route returns `dict[str, Any]` and reads a raw request body, which is deliberate so input is screened before anything is parsed. As a result, OpenAPI has no request or response schemas. The real shapes are in `app/graph/state.py` (`ComplianceReport` and its parts) and in the dicts built in `app/workflow/service.py`.
- **Error envelope:** `{"error": {"code", "message", ...}}` with codes only:
  - `VALIDATION_ERROR`, `SENSITIVE_DATA_DETECTED`, `INPUT_REJECTED`, `INVALID_ANSWER`;
  - `SESSION_NOT_FOUND`, `REPORT_NOT_FOUND`, `SESSION_EXPIRED` (410);
  - `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_MISMATCH`;
  - `RUN_IN_PROGRESS`, `NOT_AWAITING_CLARIFICATION`, `STALE_CHECKPOINT`, `NOTHING_TO_RETRY`;
  - `WORKFLOW_UNAVAILABLE`, `INTERNAL_ERROR`.
- **Report content:** citations carry `document_key`, page range, eligibility, reliability and status. They carry **no evidence text**, so the UI cannot preview source text.
- **Missing endpoints:** no listing of sessions or reports, and no document, chunk or run-detail endpoints.
- **Environment (checked 2026-11-13):** Node v20.19.4 and npm 11.5.2 are installed. Node is not yet listed as a prerequisite in the README or setup guide; S2 adds it.

## C. Owner decisions
| ID | Decision | Choice | Status |
|---|---|---|---|
| M5-D1 | Scope | **Core on the existing API:** KYC Assistant (no upload), Compliance Report, read-only Review Cases, minimal Dashboard (API health plus sessions known to this browser, labelled demo data). Document Intelligence and Observability move to M6. PDF upload and review decisions are out. | approved 2026-11-13 |
| M5-D2 | Backend changes | **Pydantic response models on existing routes only.** The JSON returned stays identical, enforced by contract tests. No new endpoint. The raw-body, screen-before-parse request handling is unchanged. | approved 2026-11-13 |
| M5-D3 | Toolchain | TypeScript, React, Vite, Tailwind CSS, React Router, TanStack React Query, Vitest, Testing Library, ESLint, Prettier, openapi-typescript. **No Playwright:** the browser demo is documented and run manually. Exact versions are pinned (each release at least two weeks old) and listed in S2 for owner confirmation before install. | approved 2026-11-13 |
| M5-D4 | Where the browser keeps session ids for the Dashboard | **Open.** A session id is the only access credential (L6: possession of the id gives access). Options: (a) `localStorage`, which survives restarts but leaves the ids readable by any script on the origin until cleared; (b) `sessionStorage`, which is cleared when the tab closes; (c) memory only, with no Dashboard history. Recommended: (b), with a "forget sessions" control. | **to decide before S6** |

## D. Scope
**In M5:**
- **KYC Assistant:**
  - creates a session with `data_class: "synthetic_demo"`;
  - submits a question;
  - shows a live node tracker over SSE;
  - shows enum-only clarification cards (fields and allowed values from `GET /sessions/{id}`, answered with the current `checkpoint_id`);
  - offers an explicit Retry for a failed or abandoned run;
  - maps every error code to a fixed message. It never echoes rejected input.
- **Compliance Report:** renders report v1 and v2:
  - the outcome, notice, disclaimer and deterministic `user_message`;
  - the profile;
  - the checklist grouped by applicability, each item showing its uncertainty label, verification status and citations (document key and page range);
  - unverified items, unresolved items, gaps, degradations and forms;
  - for v2, the verification and conflict-scan statements.
- **Review Cases (read-only):** the open cases for a report (requirement, reason code, status, created time), with no action controls.
- **Dashboard (minimal):** API health, plus sessions known to this browser (per M5-D4), labelled *demo data*.
- **Always visible:** the disclaimer "Independent prototype. Not an official bank system. Not a compliance approval."

**Out of M5:**
- Document Intelligence and Agent Observability (M6).
- PDF upload and PDF export.
- Review decisions, authentication and any new endpoint.
- Live LLM calls (the demo runs without a live LLM and is honestly DEGRADED).
- Deployment (M7).
- Any change to M0–M4 behaviour, outcomes, gates or datasets.

## E. UI safety rules (acceptance criteria for every screen)
1. **No approval or rejection wording.** No control, label or colour implies approval, rejection or compliance. Outcomes are shown with their own names. `human_review_required` is always shown.
2. **Applicability as the report gives it.** The UI shows exactly the applicability the report contains, never a stronger one. MANDATORY is shown only when the report says MANDATORY.
3. **"Verified" only for a supported claim.** "Verified" (or similar) appears only for a requirement verdict of `supported`. With `verification_meta.status == "not_configured"` every item reads *not verified*, and the UI explains that checking is not enabled, so this is not a finding that the item is wrong.
4. **Conflict-scan wording.** When `contradiction_scan == "not_run"`, the UI shows "No automated check for conflicting sources was performed". It never shows "no conflicts".
5. **Missing evidence is never "not required".** The UI renders `user_message`, `gaps` and `unresolved_items` as given. Retrieval failure (`evidence_status: "retrieval_failed"`) is shown as a technical problem, distinct from "none".
6. **Form fields are not obligations.** Form fields and related fields are labelled "the form asks for this (relationship only)" and never as obligations.
7. **Citations as stored.** Citations show the stored document key and page range. K5 (a child chunk shows its parent's page range) is noted in the citation help text, not hidden or "fixed" in the UI.
8. **Nothing sensitive leaves the browser or is stored.**
   - Raw input is never logged, stored or echoed. The question box is cleared after a successful submit.
   - Error messages are fixed text per code.
   - There is no analytics, no third-party script and no external font or CDN at runtime.
9. **No hidden reasoning.** No chain-of-thought or system prompt is displayed. None exists in the API.

## F. Stages (one stage, one review, one commit)
| # | Objective | Main files | Acceptance | Risks |
|---|---|---|---|---|
| S0 | Lock the plan and decisions; fix the docs discrepancies found in the readiness audit | this file, docs/00, 07, 09, 12, M4 closure report, handoff/01 | decisions recorded; docs state what is built | — |
| S1 | Typed API contract: Pydantic response models on the existing routes, published in OpenAPI; a committed OpenAPI snapshot | `app/api/v1/schemas.py` (new), `sessions.py`, `health.py`, tests | every existing API test unchanged and green; each route's JSON is identical before and after (contract tests on real service output, including report v1 fixtures and v2); OpenAPI snapshot test; screening order unchanged; ruff, black, strict mypy clean | a response model that drops, renames or coerces a field; mitigated with `extra="forbid"` checks and byte-equality tests |
| S2 | Frontend scaffold | `frontend/` (Vite + React + TS + Tailwind), Makefile `web`, `web-install`, `web-test`, `web-lint`, `web-types`, .gitignore, README prerequisites | pinned versions approved by the owner; `npm ci` reproducible from the lockfile; type check, lint and a smoke test pass; dev proxy `/api` → `:8000`; app shell with routing, error boundary and the persistent disclaimer | dependency footprint; Node version drift |
| S3 | Typed client and SSE hook | `frontend/src/api/` | uses the types generated in S2 from the S1 snapshot (not hand-written); one `Idempotency-Key` per user action, reused on a network retry of that action; error envelope → fixed messages per code; SSE hook follows the docs/07 semantics (current-status `run` events, durable `node` events, close on terminal status, browser reconnect with `Last-Event-ID`); React Query for reads; unit tests for each | SSE close and reconnect edge cases |
| S4 | KYC Assistant screen | `frontend/src/screens/assistant/` | create, submit, track, clarify and resume; Retry; every error code handled; the §E rules; loading, empty and error states; component tests with recorded fixtures | clarification UX; stale checkpoints |
| S5 | Compliance Report and Review Cases screens | `frontend/src/screens/report/` | renders all 8 committed v1 fixtures (`backend/tests/fixtures/m3_reports_v1/`) and v2 fixtures (NLI off, and a scripted-enabled shape); §E rule tests (no "approved"/"rejected"/"no conflicts"; no "verified" with NLI off; applicability as stored); read-only review-cases list with no action controls | wording that overstates certainty |
| S6 | Minimal Dashboard | `frontend/src/screens/dashboard/` | health panel (an outage shows as an error state); session list per M5-D4; demo-data labels; empty, loading and error states | M5-D4 risk |
| S7 | Browser demo, accessibility pass, docs and closure | README, docs/09, docs/handoff/*, `M5_CLOSURE_REPORT.md` | manual demo recorded: `make up && make api && make web`, an LLP question, clarification, a DEGRADED report and its review cases; keyboard navigation and labels checked; backend suites at the M4 baseline (known K7/K8 only); all lint clean | — |

## F.1 Frontend toolchain (owner-approved 2026-11-13, S2)
- **Runtime:** Node.js **26.13.1 LTS** via NVM for Windows (npm 11.20.0). `package.json` sets `"engines": {"node": "^26.13.1"}` and `.npmrc` sets `engine-strict`.
- **Pins:** exact (`save-exact`). Every pin was released at least 14 days before 2026-11-13.
  - Production: `react` 19.3.1, `react-dom` 19.3.1, `react-router` 7.18.5, `@tanstack/react-query` 5.105.0.
  - Dev: `vite` 8.3.5, `@vitejs/plugin-react` 6.1.2, `typescript` 5.9.3, `tailwindcss` 4.3.3, `@tailwindcss/vite` 4.3.3, `vitest` 5.0.5, `jsdom` 30.1.3, `@testing-library/react` 16.3.3, `@testing-library/dom` 10.4.2, `@testing-library/user-event` 14.6.8, `eslint` 10.13.0, `@eslint/js` 10.0.1, `typescript-eslint` 8.72.1, `eslint-plugin-react-hooks` 7.1.1, `globals` 17.13.0, `prettier` 3.9.10, `openapi-typescript` 7.13.0, `@types/react` 19.3.0, `@types/react-dom` 19.3.0, `@types/node` 26.6.6.
- **Version decisions:**
  - TypeScript stays on 5.9.3: openapi-typescript requires `^5.x` and typescript-eslint requires `<6.1.0`.
  - React Router stays on 7.18.5, the maintained 7.18 line with a known API.
  - jest-dom is not used.
- **Transitive dependencies:** resolved with `npm install --before=2026-10-30`, so every package in the tree meets the two-week rule. The newest is 2026-10-29. `package-lock.json` (v3) freezes the tree, and installs use `npm ci`.
- **Install scripts:** disabled (`ignore-scripts`). The only package that has one is `fsevents`, which is optional and macOS-only.

### Known dependency advisory (accepted temporarily; recheck at S7)
| Item | Detail |
|---|---|
| Advisory | **GHSA-g3hv-75wg-m249**, `minimatch` "potential denial of service via globparts amplification", moderate, published 2026-10-30 |
| Where | `minimatch@10.2.6`, pulled in by `eslint` 10 and every `typescript-eslint` up to 8.72.2 (the latest). `npm audit` counts it 10 times along that chain. **Dev tooling only.** |
| Production | `npm audit --omit=dev`: **0 vulnerabilities** |
| Fix available | Only minimatch 11.0.1+, a new major outside ESLint's declared range. `npm audit fix --force` would downgrade ESLint to 9.39.5 (breaking) and would still leave typescript-eslint flagged. |
| Exposure | minimatch evaluates glob patterns from our own ESLint configuration against repository paths, only when the linter runs. Exploiting it needs attacker-controlled patterns. Nothing reaches the browser bundle. |
| Decision (owner, 2026-11-13) | Accepted temporarily. No forced upgrade, no `overrides`. **Recheck at S7:** if ESLint or typescript-eslint have released versions that depend on a patched minimatch (and meet the two-week rule), propose upgrading them; otherwise record the status again. |

## G. Testing policy
- **Backend:** `make test`, `make test-integration`, `make lint` and `make check-schema` after every stage that touches `backend/`. Results are compared with the M4 baseline: unit 1 known failure (K7), integration 2 known failures (K8). No existing test is weakened or edited to pass.
- **Frontend:** `make web-test` (Vitest) and `make web-lint` (tsc, ESLint, Prettier check). Fixtures are copies of committed backend fixtures or recorded synthetic responses. No test calls a live LLM or the network.

## H. Known issues carried into M5 (unchanged)
K1–K12 and L1–L10 from [`docs/handoff/04-limitations-and-known-issues.md`](../handoff/04-limitations-and-known-issues.md). The ones the UI exposes:
- L1: NLI is disabled, so every claim is *not verified* and reports are DEGRADED;
- L2: the conflict scan is not run;
- L5: review cases are open-only;
- L6: there is no authentication;
- K5: citation page ranges.

M5 shows these as they are and fixes none of them.

## S0 outcome (2026-11-13)
- **Docs only:** no application code, test, dependency, migration or data change.
- **Discrepancies fixed:**
  - M4 merge status in `M4_CLOSURE_REPORT.md` and `docs/handoff/01-architecture-handoff.md`: merged as `0b65df0` and pushed after closure; commit count 14;
  - docs/07: the planned endpoint table now has a Status column (built / not built);
  - docs/09: M5 scope note (what is in, moved to M6, or not built by decision, and the wording rules);
  - docs/00: pointer to the as-built handoff (no regeneration loop, no "confidence", NLI disabled, no LangSmith);
  - docs/12: the M5 section points to this plan.
- **Environment:** Node v20.19.4 and npm 11.5.2 present.
- **Open:** M5-D4, to decide before S6.

## S1 outcome (2026-11-13): published, typed API contract (no behaviour change)
- **`app/api/v1/schemas.py` (new):** documentation models for every workflow response and request:
  - `SessionCreated`, `SessionView` / `WorkflowView` / `NodeEvent` / `PendingClarification` (reusing `intake.Question`), `RunAccepted`, `ReviewCasesResponse` / `ReviewCase`, and the `ErrorResponse` envelope;
  - the request models `CreateSessionRequest`, `MessageRequest`, `ResumeRequest` and `RetryRequest`, whose fields match the service's own body models;
  - `extra="forbid"` throughout.
- **Routes (`sessions.py`):**
  - `responses=` documents the success and error shapes; `openapi_extra` documents the raw request bodies (inline, flat schemas);
  - `response_model=None` keeps FastAPI from re-serializing, and no body parameter is declared, so the screen-before-parse order is unchanged;
  - every 422, including on GET routes, is documented as the envelope. FastAPI's default `HTTPValidationError` schema was wrong for this API and is gone.
- **SSE:** `/events` is documented as `text/event-stream` with `ProgressEvent` (registered in components by `main._publish_extra_schemas`).
- **Health:** the 503 response (`HealthResponse`) is now documented. The route itself is unchanged.
- **Published document:** `docs/api/openapi.json`, written by `make openapi` (`python -m app.api.openapi`). The output is deterministic, and a unit test fails when the file is stale.
- **Tests:**
  - `tests/unit/test_m5_api_contract.py` (13):
    - the snapshot;
    - documented shapes per route; flat request bodies matching the service models; no body field and no response model on any workflow route;
    - byte-identical pass-through for every session state, the 8 v1 fixtures, the v2 NLI-off and scripted reports and their review cases;
    - error envelopes;
    - undocumented keys are rejected; a review case cannot carry a decision.
  - `tests/integration/test_m5_api_contract_db.py` (2): the real LLP flow (create → pause → resume → DEGRADED v2 report → `not_verified` review cases → SSE events) and the main error codes validate against the contract.
- **Unchanged:** request handling, screening, idempotency, the graph, reports, migrations, dependencies, data.
- **Checks:**
  - **Unit (`make test`):** 1,505 passed, 1 failed (K7, the environment-dependent Groq test).
  - **Integration (`make test-integration`):** 507 passed, 1 failed (K8: `test_dense_golden`). This run, the known tied pair flipped at query m1-03 ranks 5/6 (both 0.694, identical text in two HDFC forms) rather than at m1-14, and `test_hybrid_dev` passed. No retrieval code was touched. Recorded in the K8 register entry; the test was not modified.
  - `make lint` (ruff, black, strict mypy on 126 files): clean.
  - `make check-schema`: clean.
  - `git diff --check`: clean.

## S2 outcome (2026-11-13): frontend scaffold, app shell, generated API types
- **Toolchain:** installed exactly as approved (§F.1). `npm ls --depth=0` matches every pin. The lockfile has 313 entries (10 production, 303 dev, 50 platform binaries for other operating systems).
- **Generated API types:** `src/api/schema.d.ts` is generated from the committed `docs/api/openapi.json` (`npm run gen:api` / `make web-types`). `npm run check:api` (part of `make web-lint`) fails when it is stale; this was checked both ways.
  - Generated with `--default-non-nullable false`. With the tool's default, keys that may be absent from a response (`workflow.clarification`, `report_id`, `outcome`) would have been typed as always present. Report fields with defaults therefore appear optional in TypeScript, which is the safe direction.
  - `src/api/types.ts` gives named aliases.
- **Shell:**
  - `Layout`: the sticky disclaimer "Independent prototype. Not an official bank system. Not a compliance approval." plus "Synthetic data only. Every result requires human review.", and navigation (Dashboard, KYC Assistant).
  - A page-level `ErrorBoundary` that resets on navigation. A top-level boundary renders the disclaimer itself.
  - Error details are never rendered, logged or reported.
- **Routes:** `/`, `/assistant`, `/reports/:reportId`, `*`. The pages are placeholders that say "Not built yet" and show no data. There is no upload route and no Document Intelligence or Observability route (M5-D1).
- **Dev server:** `127.0.0.1:5173` (strict port, matching `FRONTEND_ORIGIN`), with `/api` proxied to `127.0.0.1:8000`. There is no runtime CDN, external font or analytics, and the referrer policy is `no-referrer`.
- **Makefile:** `web-install` (`npm ci`), `web` (replaces the M0 refusal), `web-test`, `web-lint` (tsc + ESLint + Prettier + `check:api`), `web-build`, `web-types`.
- **Tests (Vitest, 15):**
  - the disclaimer and the correct page on every route, including not-found;
  - navigation keeps the disclaimer;
  - the navigation offers only the M5 screens;
  - no "approved/rejected/compliant" text appears;
  - a failing page keeps the shell and disclaimer and hides the error text; navigating away clears the error; the top-level fallback shows the disclaimer; "Try again" recovers;
  - type-level checks (enforced by `tsc`): optional keys stay optional, review cases are `"open"` only, outcomes contain no approval or rejection, and the error envelope types hold.
- **Smoke test:** `make api` + `make web`. The page is served; `/assistant` is served by the SPA fallback; `/api/v1/health` returns 200 through the proxy; `POST /api/v1/sessions` (`synthetic_demo`) returns 201 through the proxy. That left one synthetic session in the dev database, which `make purge` removes after it expires.
- **Backend:** unchanged in S2.
- **Line endings (found during S2 verification):** with `core.autocrlf=true`, a fresh Windows clone checked frontend files out as CRLF, and `make web-lint` failed there (Prettier and `check:api`) although it passed in the original working copy. `frontend/.gitattributes` (`* text=auto eol=lf`) fixes it. Verified on a fresh clone: `npm ci`, type-check, ESLint and Prettier, `check:api`, 15 tests and the build all pass.
