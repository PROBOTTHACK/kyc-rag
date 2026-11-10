# 01 — Architecture

## Layers
| Layer | Responsibility | Tech |
|---|---|---|
| User interaction | UI, report presentation, SSE progress | React + Vite + Tailwind |
| Application | API, sessions, orchestration entrypoint, queue workers | FastAPI, Redis (arq) |
| Intelligence | LangGraph, LLMs, RAG, multimodal, verification | LangGraph, Groq, Gemini, DeBERTa |
| Infrastructure | Relational data, vectors, cache, files, tracing | Postgres, Qdrant, Redis, local/object storage, LangSmith |

The **graph is the orchestrator** (predictable, debuggable, testable) — not free-roaming autonomous agents.

## Request lifecycle
```mermaid
flowchart TD
  U[User] --> FE[React]
  FE --> API[FastAPI + validation]
  API --> G[LangGraph run]
  G --> SEC[Input Security]
  SEC -- unsafe --> REF[Refusal]
  SEC -- review --> HR[Human Review]
  SEC -- safe --> INT[Entity Intake]
  INT -- incomplete --> CLR[Interrupt: clarification] --> INT
  INT -- complete --> RET[Policy Retrieval]
  RET --> FORM{Form analysis needed?}
  FORM -- yes --> GEM[Form Analysis / cached schema]
  FORM -- no --> SYN
  GEM --> SYN[Compliance Synthesis]
  SYN --> CLM[Claim Extraction]
  CLM --> VER[Evidence Verification]
  VER -- supported --> OUT[Output Validation]
  VER -- unsupported --> RL{Retry < 2?}
  RL -- yes --> SYN
  RL -- no --> MARK[Mark Unverified]
  VER -- contradicted --> HR
  MARK --> OUT
  OUT --> REP[Final Report]
  HR --> REP
  G -. traces .-> LS[LangSmith]
  G -. progress events .-> RD[(Redis pub/sub)] --> FE
```

## Document ingestion (implemented in M1)
Pipeline A runs offline from the CLI (`make ingest`), one document at a time. Postgres is the source of truth; Qdrant is a derived index built from committed rows, and the two are reconciled rather than written in one transaction.
```mermaid
flowchart LR
  M[manifest.yaml + PDFs] -->|schema, SHA-256| V[Validate]
  V --> A[A: PyMuPDF extraction<br/>pages, bookmarks, footnotes, tables]
  A --> B[B: page quality + Tesseract OCR<br/>fold-out regions, cache]
  B --> C[C: section-aware chunking<br/>parent/child, OCR evidence policy]
  C --> PG[(Postgres<br/>documents, pages, chunks,<br/>exclusions, ingestion_runs)]
  PG -->|committed rows| E[D: BGE-small embeddings<br/>eligible chunks only]
  E --> Q[(Qdrant policy_chunks<br/>429 points)]
  PG <-->|reconcile / repair| Q
  Q --> S[search: top-5 + citations]
  PG -->|parent expansion| S
```
| Module | Role |
|---|---|
| `app/ingestion/manifest.py` | manifest schema rules, PDF existence + SHA-256 |
| `app/ingestion/{pdf,structure,quality,ocr,pages}.py` | Stages A–B: extraction, page quality, OCR fallback |
| `app/ingestion/{cleaning,chunking}.py`, `chunkers/` | Stage C: profile-specific chunkers, parent/child, OCR evidence policy |
| `app/ingestion/prepare.py` | Stages A–C for one document, failures attributed to a stage |
| `app/db/{models,corpus,persistence,repository,runs}.py` | Stage E: schema, row mapping, idempotent writes, reads, run log |
| `app/retrieval/{embeddings,qdrant_store,indexing,search}.py` | Stage D: BGE, Qdrant with bounded backoff, idempotent indexing, filtered dense search |
| `app/retrieval/{reconcile,evaluation}.py` | Postgres ↔ Qdrant reconciliation; Recall@k benchmark (any retriever, with latency) |
| `app/retrieval/keyword.py` | M2 A1: BM25 keyword index built in-process from Postgres rows, filter-before-scoring, version-checked freshness (A2: `xmin`) |
| `app/retrieval/{fusion,retrieve}.py` | M2 A2: RRF fusion; `dense` / `bm25` / `hybrid` modes with provenance, score kinds and degraded results |
| `app/ingestion/{pipeline,cli}.py` | Stage F: per-document orchestration (full / persist / index), dry run, retry, CLI |

Details: docs/04 (pipeline), docs/08 (tables, payload), docs/15 (vocabularies), `docs/milestones/M1_HANDOFF.md` (status and measurements).

## Repo structure
```
agentic-kyc-compliance-engine/
├── CLAUDE.md  README.md  Makefile
├── docs/                      # this spec pack
├── infra/                     # docker-compose.yml, .env.example
├── data/
│   ├── raw_pdfs/              # gitignored; see docs/14-source-corpus.md
│   │   ├── regulatory/  policy/  forms/  reference/
│   │   └── manifest.yaml
│   ├── processed/             # extracted pages, form schemas
│   └── eval/                  # benchmark JSONL
├── backend/
│   ├── pyproject.toml  alembic/
│   └── app/
│       ├── main.py  config.py
│       ├── api/v1/            # sessions, documents, reports, ingestion, health, sse
│       ├── core/              # logging, errors, rate limiter, cache (Redis)
│       ├── db/                # models.py, session.py, repositories
│       ├── llm/               # provider.py (interface), groq.py, gemini.py, fallback.py
│       ├── ingestion/         # policy_pipeline.py, form_pipeline.py, chunking.py, ocr.py, cli.py
│       ├── retrieval/         # embeddings.py, qdrant_store.py, keyword.py, fusion.py, rerank.py
│       ├── rules/             # rule models, loader, applicability engine
│       ├── verification/      # claims.py, nli.py, scoring.py
│       ├── security/          # prompt_guard.py, validators.py, sanitize.py
│       ├── graph/             # state.py, builder.py, routing.py, nodes/*.py
│       └── schemas/           # Pydantic models (API + LLM outputs)
├── backend/tests/             # unit, integration, adversarial
├── eval/                      # runners, metrics, ablation configs
└── frontend/                  # React app (5 screens)
```
