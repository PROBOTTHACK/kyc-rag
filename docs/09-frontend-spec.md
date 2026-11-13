# 09 — Frontend Spec (React + Vite + Tailwind)

> **M5 scope (owner decisions, 2026-11-13; [`milestones/M5_PLAN.md`](milestones/M5_PLAN.md)).** This spec predates M3/M4. M5 builds only what the existing API supports and the locked decisions allow:
> - **In M5:** KYC Assistant (without PDF upload), Compliance Report, read-only Review Cases, and a minimal Dashboard (API health plus sessions known to this browser, labelled demo data).
> - **Moved to M6:** Document Intelligence and Agent Observability (no document, chunk or run-detail endpoints exist; LangSmith is not built).
> - **Not built, by decision:**
>   - review approve/reject/resolve actions: open-only review cases, no decision route until authentication (M4 D5);
>   - PDF upload: no endpoint, and docs/06 upload controls do not exist;
>   - the "NLI class" column: NLI is disabled (0/140 qualified), so every claim shows *not verified*;
>   - LangSmith links;
>   - PDF export.
> - **Wording rules:** labels come from the report fields themselves. Nothing is shown as "Verified" while claim verification is not configured. When the conflict scan did not run, the UI shows "No automated check for conflicting sources was performed", never "no conflicts".

Goal: a professional fintech compliance dashboard, not a bare chatbot. Two experiences: clean **user-facing report** and developer-facing **observability**.

## Screens
1. **Dashboard** — sessions, completed reports, human-review cases, flagged claims, recent activity, document library status. Label all numbers *demo data* where synthetic.
2. **KYC Assistant** — chat, entity & account selectors, PDF upload, missing-info prompts (clarification cards), live workflow stage tracker (via SSE). Never expose chain-of-thought or system prompts.
3. **Document Intelligence** — library, category, extraction status, page preview, extracted text, tables, form schema viewer, versions.
4. **Compliance Report** — entity/account info; sections for Mandatory / Conditional / Unresolved; per-requirement evidence + page links; human-review flags; PDF export.
5. **Agent Observability** — graph stages, node success/fail, retrieval results, verification outcomes, retries, latency, LangSmith links.
+ **Review Queue** (part of Report/Dashboard) — claim, source page, NLI class, approve/reject/unresolved + reason.

## UX rules
- Always show uncertainty labels (Verified / Needs review / Unresolved) and source citations.
- Persistent disclaimer: "Independent prototype. Not an official bank system. Not a compliance approval."
- Accessible, responsive, loading/error/empty states for every view.
- Typed API client (generate from OpenAPI), SSE hook with reconnect, state via React Query.
