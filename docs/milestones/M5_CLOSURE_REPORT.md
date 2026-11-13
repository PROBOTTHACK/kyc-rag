# M5 Closure Report: the web frontend

> Independent educational prototype. It is not an HDFC Bank or RBI system and not endorsed by either. It uses synthetic data only, makes no KYC approval or rejection, and every result requires human review.

## Statement
**M5 is complete.** Stages S0–S7 are finished. S7 (browser demo, accessibility, documentation and closure) is the last M5 stage. M5 delivered a local web frontend on the existing API:
- a Dashboard;
- the KYC Assistant;
- the Compliance Report with read-only review cases.

It also delivered the published API contract it is typed from. No backend behaviour changed and no endpoint was added. NLI stays disabled, review cases stay open-only, and nothing approves or rejects a KYC case.

## 1. Branch state
M5 was developed on stacked branches, one per stage. **Nothing was merged, pushed or tagged**; that is the owner's decision.

| Stage | Branch | Commits |
|---|---|---|
| S0 plan and docs | `feat/m5-s0-plan` | `07d7804` |
| S1 typed API contract | `feat/m5-s1-api-contract` | `0338fb7` |
| S2 scaffold | `feat/m5-s2-frontend-scaffold` | `22f2920`, `8ea2d07` (line endings), `009f2a7` (docs) |
| S3 client and SSE | `feat/m5-s3-api-client` | `4c7dd95` |
| S4 KYC Assistant | `feat/m5-s4-assistant` | `33e4d58`, `76befd6` (recorded stream test), `9f1bdaf` (docs) |
| S5 Compliance Report | `feat/m5-s5-report` | `d29f24c` |
| S6 M5-D4 and Dashboard | `feat/m5-s6-dashboard` | `bb89a8b` |
| S7 demo, accessibility, docs, closure | `feat/m5-s7-closure` | `cefb4b7`, plus a docs commit recording the fresh-clone result |

`feat/m5-s7-closure` contains every M5 commit on top of `main` (`0b65df0`), so merging it alone merges M5.

## 2. Owner decisions
- **M5-D1 scope:** KYC Assistant, Compliance Report, read-only Review Cases and a minimal Dashboard. Document Intelligence and Observability moved to M6; no upload and no review decisions.
- **M5-D2 backend:** response models on existing routes only, as documentation. No new endpoint, and the JSON is byte-identical.
- **M5-D3 toolchain:** TypeScript, React, Vite, Tailwind, React Router, React Query, Vitest, Testing Library, ESLint, Prettier, openapi-typescript. Exact pins, each at least two weeks old; Node 26.13.1 LTS.
- **M5-D4 session storage:** `sessionStorage` only, with a local "Forget sessions" action.
- **Accepted deviations:**
  - SSE is read with `fetch`, not `EventSource` (S3);
  - type generation moved into S2;
  - the minimatch dev-tooling advisory is accepted (S2), and was rechecked at S7.

## 3. Delivered
| Area | Delivered |
|---|---|
| API contract | `app/api/v1/schemas.py`, `docs/api/openapi.json` (`make openapi`), contract tests (unit and real-flow integration) |
| Client | Generated types; existing routes only; 15 s timeouts; bounded same-key retries; `ApiError` with fixed per-code messages; Idempotency-Key tracker |
| Live progress | Fetch-based SSE: `Last-Event-ID` replay, deduplication, current-status run events, stays closed after a terminal run, bounded backoff, silence watchdog |
| KYC Assistant | `synthetic_demo` sessions; question form (cleared after submit and after sensitive-data rejection); live steps; clarification only from the backend's options; stale-checkpoint and expiry handling; explicit retry of a failed run |
| Compliance Report | v1 and v2 from their own fields; the backend message verbatim; the exact conflict-scan wording; applicability, claim-check, validator note and citations (with the K5 caveat); set-aside items, conflicts, open points, forms; read-only review cases |
| Dashboard | API health (ok, degraded, unreachable); this tab's sessions with real status; Open in the Assistant; Forget sessions (local only) |
| Accessibility (S7) | Focus moves to the new section when the Assistant replaces the control just used; "Check again" never disables itself while focused |
| Disclaimer (S7) | The banner now also states: "Not affiliated with or endorsed by HDFC Bank or the RBI." |

## 4. Final checks (S7)
See [`docs/handoff/03-evaluation-report.md`](../handoff/03-evaluation-report.md) §9 for the full record.

| Check | Result |
|---|---|
| Frontend `make web-test` | **120 passed / 120**, 3 consecutive runs |
| Frontend `make web-lint` (tsc, ESLint, Prettier, generated types) | clean |
| Frontend `make web-build` | pass |
| Fresh clone of `feat/m5-s7-closure` at `cefb4b7` (`npm ci` + all of the above) | pass (120/120, lint, types, build) |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `npm audit` (full tree) | 10 moderate, all GHSA-g3hv-75wg-m249 (dev tooling; accepted, rechecked) |
| Backend unit (`make test`) | 1,505 passed, 1 failed (K7, environment-dependent), 512 deselected |
| Backend integration (`make test-integration`) | 507 passed, 1 failed (K8 `test_dense_golden`, tied pair at m1-03), 1,510 deselected |
| Backend `make lint` / `check-schema` / `check-workflow` | clean / no drift / `"ok": true` |
| Browser smoke test (headless Chrome 157, real backend) | **14/14** steps |
| Accessibility audit (headless Chrome) | no unnamed controls or unlabelled inputs; one `h1` and no skipped heading levels per page; landmarks present; every Tab stop has a visible focus indicator; WCAG AA contrast passes (minimum 4.55); full flow by keyboard |

## 5. Outstanding issues
All are recorded in [`docs/handoff/04-limitations-and-known-issues.md`](../handoff/04-limitations-and-known-issues.md):
- **Limitations:** L11 (local only, no authentication), L12 (browser-local session list), L13 (no source text in the report).
- **Known issues:**
  - K13: a stuck run is not offered a retry in the UI;
  - K14: the question length limit is the backend default;
  - K15: the validator wording on checked v2 items;
  - K16: the accepted minimatch advisory.
- **From M4, unchanged:** K1–K12, including the K7 and K8 environment-dependent test failures.
- **Not reproduced live in S7:** an unreachable API, a failed run, an expired session and a lease conflict. These are covered by component tests on recorded or derived API responses.

## 6. Owner actions
1. Review M5, then decide whether to merge `feat/m5-s7-closure` into `main` and push. Neither has been done.
2. **Local environment:** during the S7 browser test, an API server started before S7 (pid 31460) had its reloader process stopped by my clean-up. Its worker (pid 9252, `spawn_main(parent_pid=31460)`) is still serving port 8000 but no longer reloads. Stop it with `Stop-Process -Id 9252`, then start `make api` again.
3. Optionally clear the synthetic demo sessions created during M5 testing: `make purge` once they expire (24 h).
