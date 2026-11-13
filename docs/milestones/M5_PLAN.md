# M5 Plan: Frontend (KYC Assistant, Compliance Report, Review Cases)

> Independent educational prototype. Not an HDFC Bank or RBI system. Synthetic data only. The UI never approves or rejects anything, and every result requires human review.

**Status:** S0 locked the plan; decisions M5-D1 to M5-D3 were approved by the owner on 2026-11-13, and M5-D4 on 2026-11-14 (before S6). Stages run one at a time on the branch `feat/m5-s0-plan` and its successors, with one review and one commit per stage. **Previous milestone:** [`M4_CLOSURE_REPORT.md`](M4_CLOSURE_REPORT.md).

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
| M5-D4 | Where the browser keeps session ids for the Dashboard | **`sessionStorage`, never `localStorage`.** Session ids survive reloads and navigation in the same tab and do not persist once the tab or browser session closes. The Dashboard has a "Forget sessions" action that clears only the locally stored ids; it never deletes, abandons or modifies backend sessions or reports. Ids are not shown in the UI, logs or error messages, and malformed or expired stored entries are handled safely. A session id is the only access credential (L6). Options considered: `localStorage` (survives restarts; readable by any script on the origin until cleared) and memory only (no Dashboard history). | approved 2026-11-14 |

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
| S3 | Typed client and SSE hook | `frontend/src/api/` | uses the types generated in S2 from the S1 snapshot (not hand-written); one `Idempotency-Key` per user action, reused on a network retry of that action; error envelope → fixed messages per code; SSE hook follows the docs/07 semantics (current-status `run` events, durable `node` events, close on terminal status, reconnect with `Last-Event-ID`; read with `fetch`, not `EventSource`, see the S3 outcome); React Query for reads; unit tests for each | SSE close and reconnect edge cases |
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

## S3 outcome (2026-11-13): typed API client, idempotency, SSE progress
No backend change, no new endpoint, no new dependency, and no screen (the KYC Assistant is S4).

**Client (`frontend/src/api/client.ts`):**
- Functions exist for the existing routes only: `health`, `createSession`, `getSession`, `submitMessage`, `resume`, `retry`, `getReport`, `getReviewCases`, and the raw `fetchEvents` used by the stream.
- Request and response types come from the generated contract; request bodies are derived from `paths[...]` in `types.ts` and not redeclared.
- **Timeout:** every call has one (15 s).
- **Bounded retries:**
  - only for transient failures (no response, 502/503/504), at most 3 attempts with 500 ms then 1 s backoff;
  - only for GETs and for POSTs carrying an `Idempotency-Key`, which are retried with the **same** key;
  - `POST /sessions` has no key and is never retried.
- React Query adds no retries of its own (`retry: false`).
- **Health:** a 503 `degraded` response is returned as data, not thrown.

**Errors (`errors.ts`):**
- `ApiError` carries `status`, `code`, `categories` (code-shaped values only) and `retryAfter` (from the envelope or the `Retry-After` header).
- Its `userMessage` is a fixed text per code. The server's message, the request body and user input are never copied into an error.
- An unknown or malformed code becomes `UNKNOWN_ERROR`; a non-JSON error body becomes `BAD_RESPONSE`. Client-side codes: `NETWORK_ERROR`, `TIMEOUT`, `BAD_RESPONSE`, `UNKNOWN_ERROR`.

**Idempotency (`idempotency.ts`, used by the mutation hooks):**
- A new action gets a new key.
- A retry of the same action (same endpoint, session and body, with key order ignored) after an unknown outcome (no response or 5xx) reuses its key.
- The key is retired after success or a definitive 4xx.
- So two separate `/retry` actions never share a key. This matters because the backend's retry hash covers only the session, so a reused key would replay the old response.
- The fingerprint is held in memory only.

**React Query (`hooks.ts`):**
- `useHealth`, `useSession`, and `useReport` / `useReviewCases` (immutable reports: `staleTime: Infinity`).
- Mutations: `useCreateSession`, `useSubmitMessage`, `useResume`, `useRetryRun`. They invalidate the session view when they settle.
- An `ApiContext` lets tests inject a client.

**SSE (`sse.ts`, `events.ts`, `useSessionEvents`).** The stream is read with `fetch` rather than `EventSource` (a deviation from the S3 row's original wording). A browser `EventSource` reconnects after every server close, so after a completed run it would reconnect every few seconds indefinitely. It also cannot send `Last-Event-ID` on a new connection and cannot be bounded. Reading with `fetch` gives:
- **Applying events:** `node` events are durable and applied once (by event id, so replays never duplicate). `run` events are current-status only, so the latest wins (S7-D3).
- **`Last-Event-ID`:** sent on every reconnect.
- **Terminal close:** when the server closes with the latest run `completed`, `failed` or `abandoned`, the stream stays closed. `follow()` reopens it after a new message or retry and continues from the last id into the next run.
- **Other ends:** an idle timeout, a dropped connection, a 5xx or a silent connection (no bytes for 45 s, three keep-alive intervals) reconnect with backoff of 1, 2, 4 and 8 s. After 5 consecutive failures the stream stops with an error, and `follow()` starts over.
- **4xx:** e.g. `SESSION_NOT_FOUND`; the stream stops without retrying.
- **Other details:**
  - A connection that delivered anything resets the failure count.
  - `stop()` and the watchdog cancel the reader explicitly.
  - Events outside the contract (bad JSON, a kind mismatch, no id) are ignored.
  - The parser reports only a message's own id, a deliberate difference from the EventSource spec, documented in `sse.ts`.
- **The hook:** `useSessionEvents` refetches the session view on every run-status change (to get clarification questions or the report id) and closes the connection on unmount.
- **Storage (M5-D4 still open):** nothing is written to browser storage; session ids and progress live in memory only.

**Tests (Vitest; 52 in total, 37 new):**
- `client.test.ts` (13): routes, methods and headers; the key only where the API defines it; the error envelope, fixed messages and no request input in errors; `retry_after`; malformed codes; non-JSON 502; same-key bounded retries; no retry of a 4xx, 500 or `POST /sessions`; timeout; caller cancellation.
- `idempotency.test.tsx` (7): tracker rules and fingerprints; through the mutation hooks: a user's retry reuses the key, a new message gets a new one, a corrected answer is new, two run retries never share a key, and pending keys are per action.
- `sse.test.ts` (4): the parser at every split point, with CRLF and CR, and multi-line data.
- `events.test.ts` (10): replay to a terminal close with no reconnect; reconnect with `Last-Event-ID` after a non-terminal end, with duplicates ignored and the latest status winning; resume after a dropped connection; bounded failures with backoff and a manual restart; 5xx retried and 4xx not; a non-SSE response; the silence watchdog vs keep-alives; continuing into the next run; open with no run and `stop()`; malformed events.
- `hooks.test.tsx` (3): session refetch on run-status changes only, cleanup on unmount, the query key.
- Timing-dependent tests were run 6 times in a row, all green.

**Live check (`make api` + `make web`, synthetic LLP question, no live LLM):**
- **Streaming:** the stream opened through the Vite proxy before posting; events arrived 0.3 to 1.6 s after the post while the connection stayed open (the run paused for clarification), so the proxy streams and does not buffer.
- **`Last-Event-ID`:** it passes through the proxy. `<run>.1` returned only seq 2 and the current status; the latest id returned nothing new.
- **Unknown session:** returns the 404 envelope.
- This left one more synthetic session in the dev database, removed by `make purge` after it expires.

**Checks:**
- **Frontend:** `make web-lint` (tsc, ESLint, Prettier, `check:api`), `make web-test` (52/52) and `make web-build` all pass.
- **Backend (unchanged):** `make lint` is clean; `make test` gives 1,505 passed and 1 failed (K7, as before).

## S4 outcome (2026-11-14): KYC Assistant screen
Built on the S3 client and hooks only. No backend change, no new endpoint, no new dependency. Report details (S5) and the Dashboard (S6) are not built here.

**Screen (`frontend/src/screens/assistant/`):**
- **Start:** creates a `synthetic_demo` session (the only kind the UI offers).
- **Session:**
  - The session id is held in memory by `AssistantSessionProvider` (`session.tsx`) above the routes, so opening the report and coming back keeps it.
  - It is never written to browser storage, the URL or the page, and a reload forgets it (M5-D4 remains open).
  - The event stream opens with the session, before the first question (docs/07).
- **Question (`QuestionForm`):**
  - The text box is cleared after a successful submit, and also after `SENSITIVE_DATA_DETECTED` so personal data does not stay on screen.
  - Rejected text is never echoed or logged.
  - "Try again" appears only after an unknown outcome and only while the text is unchanged, so it resends the same action with the same key.
  - The client-side limit is 2,000 characters (the backend's default `QUERY_MAX_CHARS`).
- **Progress (`ProgressTracker`):**
  - shows the run status (an `aria-live` region) and the completed steps from the live stream, falling back to the session view's `node_events`;
  - steps have readable labels; an `error:<Class>` step shows as "Stopped by an error", without the class name.
- **Clarification (`ClarificationForm`):**
  - Questions and options come only from the backend's `clarification`, rendered as radio groups with nothing pre-selected and no guessed answer. Option text is the enum value with underscores shown as spaces; the submitted value is unchanged.
  - Every asked question must be answered before sending (an unanswered field would be asked again and use up a round). It shows "Round n of m".
  - The form is keyed by `checkpoint_id`, so a new checkpoint always starts with empty answers.
  - `STALE_CHECKPOINT`, `NOT_AWAITING_CLARIFICATION`, `RUN_IN_PROGRESS` and `SESSION_EXPIRED` reload the session view.
  - A `null` clarification shows "could not be loaded" with a reload button.
- **Result (`RunResult`):**
  - Completed: the outcome label (neutral wording for all five outcomes; nothing approves, rejects or certifies), the human-review note, and a link to `/reports/{id}` (or "No report was stored"). Report details are S5.
  - Failed: an explicit "Retry the run" (`POST /retry` with its own key). Lease-conflict errors show "try again in about N seconds"; `NOTHING_TO_RETRY` reloads the session. A new question is also possible.
  - Abandoned: explained, and a new question is possible.
- **Expired session (status `expired`, or a 410 on a write):** inputs are hidden, with an offer to start a new session.
- **Unknown session (404):** the same offer.
- **Connection notices:** "reconnecting (attempt n)" while the stream retries. "Live progress is unavailable" after it gives up, with Reconnect and Refresh buttons; only the error code is shown.
- **Messages:** all errors use the fixed per-code messages from S3 (`ErrorNotice`).

**Fixtures (`frontend/src/test/fixtures/`):**
- **Recorded:** `live/*.json` and `live/events_completed.txt` were recorded from the real backend (dev corpus, no live LLM, synthetic LLP question) by a one-off script.
  - Every server UUID was replaced by a stable fake one.
  - The question text and the rejected synthetic PAN appear nowhere in them; the backend never echoes input.
  - Recorded states and errors: session created / no run / awaiting clarification (`entity_type` and `account_type`, with the real option lists) / completed (DEGRADED, 11 steps), the report and its review cases, and the errors `RUN_IN_PROGRESS`, `STALE_CHECKPOINT`, `INVALID_ANSWER`, `SENSITIVE_DATA_DETECTED` (message and resume), `NOTHING_TO_RETRY` and `SESSION_NOT_FOUND`.
- **Derived:** shapes that cannot be produced on demand are derived from the recorded ones in `live.ts` and labelled: a failed run, an expired session, a 410, a lease-valid `RUN_IN_PROGRESS` with `retry_after`, a new checkpoint, an unreadable clarification.
- **Error parsing:** test errors go through the real `errorFromResponse`.

**Tests:**
- `AssistantPage.test.tsx` (14) runs the real `App` (disclaimer, routes, in-memory session), hooks, idempotency tracker and SSE stream; only the network is replaced. It covers:
  - a `synthetic_demo` session, with the disclaimer always visible and the id never shown;
  - the whole flow (question → live steps → clarification with exactly the backend's options → resume body → result and report link), with the box cleared after submit, the question text not echoed, and no approval or rejection wording;
  - a stale checkpoint (reload, answers cleared, the next resume uses the new checkpoint);
  - an invalid answer; `RUN_IN_PROGRESS`; `SENSITIVE_DATA_DETECTED` (cleared, never in the page or in any console call, no "Try again");
  - a 410 expiry;
  - "Try again" reusing the key;
  - a failed-run retry (the error class is hidden) and the lease-valid retry message;
  - an unreadable clarification; an unknown session;
  - reconnecting and unavailable live-progress states with a manual reconnect;
  - session kept across navigation to the report and back.
- `App.test.tsx`: the S2 shell tests now render inside a React Query provider (the assistant needs one). Their assertions are unchanged.
- `events.test.ts` (+1): the recorded `events_completed.txt` is replayed through `SessionEventStream`. It closes on `completed` with no reconnect, and its node events equal the session view's. `frontend/.gitattributes` exempts only that recorded stream from the whitespace check: its final blank line terminates the last SSE event.

**Checks:**
- **Frontend:** `make web-lint`, `make web-test` (67/67; 66 before the replay test, 3 consecutive runs) and `make web-build` pass, and a fresh clone of the branch passes them too.
- **Backend (unchanged; no backend file modified):** `make lint` is clean; `make test` gives 1,505 passed and 1 failed (K7).

**Known issues and limits (S4):**
- **A crashed run can't be recovered from the UI yet.** A run left `running` by a server crash, with its lease expired, can be recovered by `POST /retry`, but the UI offers Retry only for `failed` runs; until the lease expires the backend refuses anyway (409 with `Retry-After`). The screen shows the run as in progress, and the live-progress notice offers Reconnect and Refresh. This is left to the owner (S7 or later).
- **Session is lost on reload** (memory only), until M5-D4 is decided. *Resolved in S6: kept in `sessionStorage` per M5-D4.*
- **Character limit:** the 2,000-character limit is the backend default; the server enforces its own configured value.
- **No browser run in S4:** the screen has not been driven in a real browser yet. The full flow is covered by component tests on recorded responses, the live SSE path was verified through the proxy in S3, and the manual browser demo is S7.
- **Dev database:** recording the fixtures left synthetic sessions there (removed by `make purge` after they expire).

## S5 outcome (2026-11-14): Compliance Report and read-only review cases
Built on the S3 hooks (`useReport`, `useReviewCases`). No backend change, no new endpoint, no new dependency. The Dashboard (S6) is not built here.

**Screen (`frontend/src/screens/report/`):**
- **Rendering:** `ReportView` renders one stored report, v1 (M3) or v2 (M4), from its own fields only. Absent keys stay absent; stored v1 rows lack the defaults the API adds, and are handled as they are.
- **Outcome:** the outcome label, "Human review required" with the report's own `notice`, and the report format version.
- **Summary:** the backend's `user_message`, verbatim, including its disclaimer, its verification note and its conflict-scan sentence.
- **Profile:** the customer profile used (value and source: stated, answered, not provided) and any missing details.
- **Checks:**
  - **Evidence status:** `retrieval_failed` is shown as a technical problem, distinct from `none`, which says that it "does not mean that nothing is required".
  - **Claim checking:** v1 gives "not checked"; v2 `not_configured` gives "not enabled… not a finding that any item is wrong"; ran / partial / failed each have their own text. For ran or partial, the model id and thresholds version are shown, with "scores are uncalibrated signals, not confidence".
  - **Conflicting sources:** word for word as the backend writes it. `not_run`, and every v1 report, gives **"No automated check for conflicting sources was performed."**; `failed` and `bounded` give the backend's own sentences, with the counts for a bounded scan.
- **Checklist:**
  - Grouped Mandatory → Conditional → Optional → Unclear. A `null` checklist and an empty one each get their own message, and the empty one says it "does not mean that nothing is required".
  - Each item shows:
    - its applicability, and the proposed one when it was lowered;
    - its claim-check status (a label and an explanation per verdict). "Supported" wording appears only for supported verdicts, always qualified "(automated check)". `not_verified` reads "Not verified … not a finding that the item is wrong"; `not_claim_verified` reads "Not claim-verified";
    - the validator's `uncertainty_label`, verbatim;
    - the model-drafted explanation with its status;
    - its citations, as `document_key, p./pp.` plus eligibility, text reliability, source status and a broader-group scope;
    - related form fields, labelled "the form asks for this; not an obligation";
    - review flags.
- **K5 note:** wherever citations are listed, "Page references are copied from the stored source records and have not been independently re-checked … (known issue K5)".
- **Set aside for review:** v2 `unverified_items` with their prior applicability, reason codes and citations, and "This does not mean they are not required."
- **Possible conflicts between sources:** both sides (document, pages, eligibility, status, cited or not), resolved or unresolved, and the reason, with "a newer date never settles a conflict". The uncalibrated contradiction score is not shown.
- **Open points:** unresolved items and gaps with readable labels plus the raw code, degradations (component and code), validation findings, and screening codes for BLOCKED.
- **Forms analysed:** the extraction method, version, fields analysed, subset-only and flags, with "Form fields are never policy obligations."
- **Review cases (`ReviewCasesPanel`):**
  - read-only, listed as requirement (name and id), reason, "Open", and the time it was opened;
  - states that recording a review decision is not available (it needs authentication);
  - **no buttons or inputs**; an empty list still states that every result requires human review;
  - if loading them fails, the report stays visible with its own retry.
- **States:** loading; `REPORT_NOT_FOUND` (deleted after retention, or a wrong address; no retry); unreachable or other errors (fixed message and "Try again").
- **Session:** the page never touches the assistant session and links "Back to the KYC Assistant".
- **Labels (`labels.ts`):** typed by the generated enums, so a new backend value fails the type-check until it is labelled. Unknown free-form codes are shown raw, never guessed.

**Fixtures (no hand-written reports):**
- **v1:** the 8 committed fixtures, read directly from `backend/tests/fixtures/m3_reports_v1/` with `import.meta.glob` (no copies).
- **v2:** `frontend/src/test/fixtures/reports/v2_*.json`, 8 genuine reports from the backend's M4 acceptance helper (`test_m4_acceptance_s8.py::reports`: real graph runs, synthetic corpus, NLI disabled or scripted, no model), serialized with `ComplianceReport.model_dump(mode="json")`.
  - Each has a `*.review_cases.json` derived with the backend's `review_cases()`.
  - Together they cover verification `ran` / `partial` / `failed` / `not_configured`, scans `not_run` / `bounded` / `failed`, an unsupported item set aside, a source conflict, and a contradicted item.
- **Live:** the DEGRADED v1 report and its empty review cases recorded from the real API in S4.

**Tests (`ReportPage.test.tsx`, 33):**
- **Every one of the 17 reports renders its own data:**
  - the human-review line and the notice;
  - the backend message verbatim;
  - the conflict-scan sentence, also asserted to occur in the backend's own `user_message` for v2;
  - each item's applicability, claim-check label, validator note and every citation's `document_key, pages`;
  - no "Supported" wording for not-verified items, and the K5 note;
  - no approve/approval/reject/rejection/compliant anywhere;
  - no unnegated "verified" in the UI's own text. The backend's prescribed fallback ("I don't have sufficient verified information …") is shown verbatim and excluded from this check only;
  - **no button, checkbox or radio on the page.**
- **Specific states:** not-configured review cases; check ran (qualified, uncalibrated); unsupported set aside; conflict; contradicted; v1 notice and form links; retrieval failure vs no evidence; no evidence ≠ not required; blocked; missing information; degradations and empty review cases.
- **Loading, missing and unavailable:** loading, not found, unreachable, and a review-cases failure that keeps the report visible.
- **Guard check:** relabelling `not_verified` as "Verified" made 4 tests fail; the label was then restored.
- **Assistant navigation (`AssistantPage.test.tsx`):** the test now opens the real report the run produced, returns through "Back to the KYC Assistant" and through the main navigation, and confirms the same in-memory session and a single `createSession`.

**Checks:**
- **Frontend:** `make web-lint`, `make web-test` (100/100, 3 consecutive runs) and `make web-build` pass.
- **Backend (no backend file modified):** `make lint` is clean; `make test` gives 1,505 passed and 1 failed (K7).

**Limitations (S5, documented rather than worked around):**
- **No source text:** the API returns no evidence text, so the report shows document keys and page ranges but no passage preview. Page references are not re-checked (K5).
- **Stale validator wording on checked items:** on a v2 report whose claim check ran, an item's `uncertainty_label` still carries the M3 validator wording ("…proposed, not claim-verified (M4)"), because the backend sets it before verification. It is shown verbatim, labelled "Validator note", next to the item's actual claim-check status.
- **Fallback message wording:** the backend's fallback ("I don't have sufficient verified information …") is shown as written; it is the prescribed M3 message.
- **Access to reports:** anyone with a report id can open the report (L6, no authentication). Review cases have no decision workflow (L5).

**Known issues carried forward from S4 (unchanged):**
- the session is lost on a page reload (memory only; M5-D4 open). *Resolved in S6 (M5-D4: `sessionStorage`).*
- a run stuck after a server crash is not offered a retry in the UI (only `failed` runs are);
- the question's 2,000-character limit is the backend default and must be confirmed against the configured `QUERY_MAX_CHARS`.

## S6 outcome (2026-11-14): M5-D4 locked; minimal Dashboard
No backend change, no new endpoint, no new dependency. No Document Intelligence, observability, upload, review decision or authentication.

**M5-D4 implementation (`frontend/src/storage/sessions.ts`):**
- **Key:** one `sessionStorage` key, `kyc-assistant.sessions.v1`, holding `{sessions: [{id, addedAt}], current}`.
  - `addedAt` is when this tab first saw the session; it is local metadata, not from the API.
  - `localStorage` is never used.
- **Validation on every read:**
  - Invalid JSON or a non-object discards the value and clears the key.
  - Entries must be lowercase UUIDs with a parseable `addedAt`; anything else is dropped, and duplicates are removed.
  - `current` must be in the list, otherwise it becomes `null`.
  - The list is capped at 20 (the most recent). A cleaned value is written back, so malformed data never lingers.
- **Storage failures:** storage that is unavailable or throws degrades to memory only and never throws to the UI.
- **Expired or purged sessions:** the API decides. A stored id whose session is expired shows as expired; one the server no longer knows (404) shows as "No longer available on the server". Neither is ever shown as active or complete.
- **Forget sessions:** removes only that key (other keys untouched). No API call is made; backend sessions and reports expire on their own schedule.
- **Session provider:** `AssistantSessionProvider` loads from the store and persists after each change, from an effect, so state updaters stay pure. The Assistant's session therefore survives reloads and navigation in the same tab, which resolves the S4 "lost on reload" issue.
- **Ids:** never shown in the page, the URL, logs or errors. Sessions are labelled "Session 1, 2, …".

**Dashboard (`frontend/src/screens/dashboard/DashboardPage.tsx`):**
- **API health** (`GET /health`):
  - ok: all services reachable, with latency per check;
  - degraded (the 503 body is data, not an error): which service is unavailable, without exception names;
  - unreachable: the fixed error message;
  - loading; and "Check again".
- **Sessions in this browser tab:**
  - Stated as browser-local and "not a complete history".
  - Each stored session is read with the existing `GET /sessions/{id}` (at most 20).
  - What is shown, all of it from the API's own fields, nothing inferred:
    - active until a time, or expired;
    - "No question asked yet";
    - the latest question's run status;
    - for a completed run only: the outcome label and "Open the report", or "No report was stored";
    - 404 shown as no longer available, with no "Open" action;
    - other errors with "Try again".
  - "Open in the Assistant" makes the session current and navigates; the Assistant then marks it "(open in the Assistant)".
  - Empty state links to the Assistant.
- **Other:** a "Demo data" label, and the disclaimer stays in the layout.

**Tests (17 new; 117 in total):**
- `storage/sessions.test.ts` (8): survives a reload; never uses `localStorage`; malformed values discarded and cleared (6 shapes); only valid entries kept (bad ids, bad dates, duplicates, an unknown current); the cap; remember/switch/ignore-invalid; forget removes only its key; no throw when storage is unavailable.
- `DashboardPage.test.tsx` (9):
  - health ok / degraded / loading → unreachable → check again;
  - empty and browser-local wording;
  - real per-session status across 7 states (no run, running, awaiting, completed with report, expired, 404, 503), with only the completed run showing a result or report link, no id in the page, and no approval wording;
  - "Open in the Assistant";
  - "Forget sessions" clears the list and the key, makes no state-changing call, and leaves the Assistant on its start screen;
  - malformed stored data at load;
  - a session started in the Assistant survives a reload (unmount plus a fresh render), is resumed without a second `createSession`, and is listed on the Dashboard.
- **Shared test setup:** clears `sessionStorage` and `localStorage` after each test (isolation).
- **`App.test.tsx`:** the shell tests now use a fake client, because the Dashboard checks health on load. Their assertions are unchanged.
- **Mutation checks:**
  - storing in `localStorage` made 6 tests fail;
  - a forget that kept the key was caught by the storage test (the provider also removes the key when it saves an empty state, so behaviour stays correct through either path);
  - both were restored.

**Checks:**
- **Frontend:** `make web-lint`, `make web-test` (117/117, 3 consecutive runs) and `make web-build` pass, also on a fresh clone.
- **Backend (no backend file modified):** `make lint` is clean; `make test` gives 1,505 passed and 1 failed (K7, environment-dependent, as before). The integration suite was not re-run in S6 because no backend code changed; the last full run (S1) was 507 passed and 1 failed (K8).

**Known issues carried forward:**
- a run stuck after a server crash is not offered a retry in the UI (only `failed` runs are);
- the question's 2,000-character limit is the backend default (`QUERY_MAX_CHARS`); confirm it against the configured value;
- the S5 report limitations (no source text, the stale validator wording on checked v2 items, the prescribed fallback wording, access by possession of an id).
