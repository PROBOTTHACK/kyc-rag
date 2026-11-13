# 00 — Project Overview

> This is the original project vision. Some planned parts were changed or not built: there is no regeneration loop (M4 decision D3), scores are uncalibrated signals and never "confidence" (M4 D4), NLI is disabled (no model qualified), and LangSmith is not implemented. For what exists, see [`docs/handoff/01-architecture-handoff.md`](handoff/01-architecture-handoff.md) and [`docs/handoff/04-limitations-and-known-issues.md`](handoff/04-limitations-and-known-issues.md).

**Name:** Agentic KYC Compliance Engine (portfolio prototype; configured for HDFC Bank documents + RBI regulation)
**Category:** Agentic AI · FinTech · Multimodal RAG · Document Intelligence

## Problem
KYC requirements differ by customer category (individual, LLP, private limited company, others). A plain document chatbot returns a generic paragraph. The system must relate: (1) entity type, (2) applicable regulation, (3) bank-documented requirements, (4) the account-opening form, (5) evidence for each checklist item.

## Solution
A stateful LangGraph workflow that: validates and guards input -> identifies entity/account type (asks for missing info, never guesses) -> retrieves policy evidence (hybrid search with metadata filters) -> optionally analyzes forms with a multimodal model -> synthesizes an evidence-backed checklist -> verifies every claim (atomic claims + NLI + rules) -> routes unsupported/contradicted claims to regeneration or human review -> outputs a report with citations and uncertainty labels.

**Differentiator:** it knows when it cannot answer. A polished-but-wrong checklist is worse than an incomplete honest one.

## Eight capabilities
1. Intelligent KYC intake (structured extraction, targeted clarification, interrupt/resume)
2. Policy intelligence & document understanding (two ingestion pipelines)
3. Multimodal form intelligence (fields, checkboxes, signatures, declarations, annexures -> validated JSON)
4. Agentic policy retrieval (metadata filter + dense + keyword + RRF [+ rerank])
5. Compliance synthesis (MANDATORY / CONDITIONAL / OPTIONAL / UNCLEAR)
6. Hallucination & evidence verification (claim extraction -> evidence match -> NLI -> confidence -> routing)
7. Security & guardrails (input, document, output)
8. Observability & tracing (LangSmith + in-app observability screen)

## Scope
| In scope (Core) | Enhancement | Out of scope |
|---|---|---|
| Ingestion of ~8-10 public/authorized PDFs (RBI + HDFC), entity classification, policy RAG, Qdrant metadata filtering, multimodal form extraction, LangGraph orchestration, claim-level NLI, prompt-injection protection, human review workflow, LangSmith, React dashboard, evaluation benchmark | Hybrid search + reranking, PDF export, RBAC, audit logs, advanced OCR | Real banking integrations, actual KYC approval, identity verification (PAN/Aadhaar), legal decisions |

## Document-first rule
Before coding: inspect the real source PDFs, classify them, decide which entity types they genuinely support. Do NOT promise entity coverage the sources don't back.

## Disclaimers (must appear in README and UI)
Independent prototype; not an official bank product; no compliance approvals; doesn't replace compliance teams; synthetic data only.
