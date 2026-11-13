# 12 — Claude Code Playbook

Work in order. Don't start a milestone until the previous one meets its definition of done. Start each Claude Code session with: *"Read CLAUDE.md and docs/. We are on Milestone N."*

## M0 — Scaffold (½ day)
Prompt: "Create repo structure from docs/01, Makefile, pyproject, ruff/pytest config, FastAPI skeleton with /health checking Postgres/Redis/Qdrant, Alembic init, config via pydantic-settings reading infra/.env.example. Use infra/docker-compose.yml."
Done: `make up && make api` -> `/health` all green; tests run.

## M1 — Document intelligence foundation (Week 1)  ⭐ start here
First read docs/14-source-corpus.md; set up data/raw_pdfs/ and manifest.yaml, add `bank` and `status` to chunk metadata.
Prompt: "Implement Pipeline A from docs/04: PyMuPDF extraction with page refs, text-quality check, OCR fallback, section-aware chunking, metadata, BGE embeddings, Qdrant upsert, Postgres chunk rows. CLI `make ingest`. Add a `search` CLI returning top-5 passages with doc+page citations. Idempotent by SHA-256."
Done: ask an entity question -> correct passages with page refs; Recall@5 measured on ≥15 hand-labeled queries. **First, inspect the real PDFs and write `data/processed/corpus_report.md`** (what each doc is, quality, entity coverage).

## M2 — Retrieval & form intelligence (Week 2)
Prompts: (a) "Add real BM25 keyword retrieval using `rank_bm25` (index built in-process from Postgres chunk rows, rebuilt after each ingestion, metadata-filtered before scoring) + RRF fusion with dense results; compare dense-only vs BM25-only vs hybrid on the labeled set and keep whichever wins." (b) "Add optional cross-encoder rerank behind a flag." (c) "Implement Pipeline B with MultimodalProvider (Gemini), Pydantic form schema validation, versioning, Redis cache."
Done: hybrid ≥ dense baseline (or documented why not); form schema JSON validated + stored with page refs.

## M3 — LangGraph end-to-end (Week 3)
Prompt: "Implement KYCState, nodes security/intake/retrieval/form/synthesis/output, conditional routing, Postgres checkpointer, interrupt/resume for clarification, Redis pub/sub progress events, SSE endpoint, LLMProvider with Groq + fallback + Redis rate limiter + cache."
Done: LLP question w/ missing info pauses, resumes, returns a cited checklist.

## M4 — Claim verification (Week 4; locked plan: `docs/milestones/M4_PLAN.md`)
Scope (decision D1): typed claim extraction, deterministic obligation/negation analysis, local DeBERTa NLI with uncalibrated scores and dev-selected thresholds, downgrade-only effects, bounded contradiction and source-conflict flags, open review cases (no decision endpoint until authentication), claim-verification evaluation sets with a frozen held-out split.
Done: an unsupported or overstated claim is caught and shown as unverified; no MANDATORY without a primary citation that supports the obligation; held-out results recorded as measured.
Moved to the **Future security milestone — number TBD** (M5 below remains the frontend): Prompt Guard, the rules layer, the adversarial suite and attack success rate, reviewer decisions with authentication.

## M5 — Frontend (Week 5)
Prompt: "Build the 5 screens + review queue per docs/09 with typed API client and SSE progress."
Done: full demo flow in browser.
Locked scope (owner, 2026-11-13; `docs/milestones/M5_PLAN.md`): KYC Assistant, Compliance Report, read-only Review Cases and a minimal Dashboard on the existing API; Document Intelligence and Observability move to M6; no upload, no review decisions, no new endpoint (response models only).

## M6 — Evaluation & observability (Week 6)
Prompt: "Build 50-case benchmark runner, metrics, ablations A–E, LangSmith tracing, graph_runs mirroring, Observability screen, markdown eval report."
Done: reproducible `make eval` + honest results table.

## M7 — Packaging (Week 7)
Prompt: "Dockerfiles, deployment to free tiers (docs/02), README with architecture diagram + limitations, demo script, final docs pass."
Done: public repo + demo video + live/demo-able deployment.

## Prompt hygiene tips
- One milestone per session; ask Claude Code to write tests first for retrieval/verification.
- After each milestone ask: "Summarize what's done, what's deferred, and any spec deviations."
- Commit per feature. Never paste real API keys into chat; use `.env`.

## Build-order priorities (read this first)
1. **Inspect the six real PDFs first**; decide which entity types are genuinely covered. Build only for those.
2. **M1 only until retrieval works**: Recall@5 measured on 15–20 hand-labeled questions. Bad retrieval poisons everything above it.
3. **Hybrid = dense (BGE) + real BM25 (`rank_bm25`) + RRF.** Compare against dense-only; keep the winner.
4. **Core path next**: intake -> retrieval -> synthesis -> claim verification. Verification is the differentiator. Form analysis (Gemini) comes after.
5. **Cut/defer if time is short**: reranking, RBAC, audit logs, PDF export, advanced OCR, fancy frontend polish.
6. **Show numbers**: ablation A (plain RAG) vs D (RAG + claim NLI) vs E (full) is the strongest interview artifact.
7. **Protect free-tier quotas**: cache in Redis, develop on local Docker, minimize Groq/Gemini calls, use the 20B model for cheap steps.
