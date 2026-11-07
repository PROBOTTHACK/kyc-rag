# 09 — Frontend Spec (React + Vite + Tailwind)

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
