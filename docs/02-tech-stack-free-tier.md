# 02 — Tech Stack & Free-Tier Plan (with Postgres + Redis)

> Free-tier limits change often. **Verify limits, model IDs and data-use terms at implementation time.** Numbers below are guidance, not guarantees. Everything has a local-Docker fallback so development costs nothing.

## Stack
| Concern | Choice | Free path |
|---|---|---|
| Frontend | React + Vite + Tailwind | Vercel / Netlify / Cloudflare Pages free |
| Backend | Python + FastAPI | Render / Fly.io / Hugging Face Spaces (Docker) free tiers; or run local |
| Orchestration | LangGraph + `langgraph-checkpoint-postgres` | OSS |
| Primary LLM | Groq: `openai/gpt-oss-120b` (fallback `openai/gpt-oss-20b`) | Groq free tier (rate-limited) |
| Multimodal | Gemini Flash-class model (ID from env; verify exact stable ID) | Gemini API free tier |
| Prompt-injection model | Llama Prompt Guard 2 (86M) via Groq, or local HF model | free tier / local |
| Embeddings | `BAAI/bge-small-en-v1.5` (local, sentence-transformers) | free, CPU OK |
| NLI | DeBERTa-v3 fine-tuned on MNLI (local, CPU) | free |
| Vector DB | Qdrant | Local Docker (dev) / Qdrant Cloud free cluster (demo) |
| Relational DB | **PostgreSQL** | Local Docker (dev) / Neon or Supabase free (demo) |
| Cache / queue / pub-sub | **Redis** | Local Docker (dev) / Upstash Redis free (demo) |
| Observability | LangSmith Developer | free traces/month (verify quota) |
| PDF | PyMuPDF, Tesseract OCR fallback | OSS |
| Files | Local disk / `data/` | OSS (private bucket only if deploying publicly) |

## Dependency pins and licences
- Exact tested versions live in `backend/requirements.lock` (`make install-locked`; refresh with `make lock` after tests pass). `pyproject.toml` keeps lower bounds only.
- **PyMuPDF is AGPL-3.0** (dual-licensed with an Artifex commercial licence). It was verified from the installed package metadata (`pymupdf 2.1`: "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License"). `pymupdf4llm 2.1`, which `pymupdf 2.1` requires, carries AGPL-3.0 headers in its source. `pymupdf_layout 2.1` is a metadata-only package with no code. This is fine for this open-source, non-commercial portfolio project. **Using the code in a closed-source or network-served product requires either releasing the source under AGPL-3.0 or buying the commercial licence.** Re-check before any such use.
- Other new M1 dependencies: sentence-transformers (Apache-2.0), torch (Apache-2.0 + BSD components per its metadata; CPU build on Windows), PyYAML (MIT), onnxruntime (MIT; pulled in by pymupdf 2.1). Tesseract (Apache-2.0) is a system binary, not a Python dependency.
- M2 A1: `rank_bm25==0.2.2` (Apache-2.0: verified from the bundled LICENSE file, whose metadata field reads "Apache2.0"). It is pure Python, its only runtime dependency is numpy (already locked), and its last release was in 2022. It is used only behind `app/retrieval/keyword.py`, so a small in-house BM25Okapi could replace it if needed.

## Where Postgres is used
- **LangGraph checkpointer** (replaces SQLite) -> pause/resume for clarification & human review.
- Sessions, documents, reports, claims, review cases, form schemas, rules, evaluation runs (see `08-data-model.md`).
- **Keyword retrieval leg:** real BM25 via `rank_bm25`, in-process. Chunk text lives in Postgres (source of truth); the BM25 index is loaded at startup and rebuilt after each ingestion (milliseconds at this scale). Fused with Qdrant dense results via RRF. Fallbacks if ever needed: Qdrant sparse vectors with a BM25 encoder. Postgres FTS (`ts_rank`) is NOT used — it isn't true BM25.
- Optional: `pgvector` is NOT used — Qdrant stays the vector store (spec lock).

## Where Redis is used
1. **Rate limiting / token buckets** for Groq and Gemini calls (shared across workers; avoids 429 storms).
2. **LLM + embedding + form-schema cache** (keyed by content hash + model + prompt version) to save quota.
3. **Pub/Sub for SSE** workflow progress: nodes publish `status` events, API streams them to the UI.
4. **Job queue** (`arq`) for long ingestion/evaluation runs.
5. **Idempotency + locks** (don't run ingestion twice for the same file hash; one active graph run per session).
6. **Short-lived session/UI state** with TTL (retention limits).
Never store secrets or raw PII in Redis; set TTLs on everything.

## Provider abstraction (required)
```python
class LLMProvider(Protocol):
    async def generate_structured(self, prompt, schema: type[BaseModel], **opts) -> BaseModel: ...
    async def generate_text(self, prompt, **opts) -> str: ...
class MultimodalProvider(Protocol):
    async def analyze_pages(self, images: list[bytes], prompt, schema) -> BaseModel: ...
```
`FallbackProvider` wraps primary + fallback with retry/backoff and Redis rate limiter. Swap models by env var only.

## Free-tier risks & mitigations
| Risk | Mitigation |
|---|---|
| Groq/Gemini rate limits | Redis token bucket, cache, backoff, fallback model, `gpt-oss-20b` for cheap steps |
| Qdrant Cloud free cluster suspends when idle | Dev on local Docker; keep-alive ping or re-index script (`make ingest`) |
| Neon/Supabase free DB sleeps/limits | Migrations + seed scripts; small data volume |
| Free backend hosts have low RAM | DeBERTa + BGE are small, but test memory; fall back to local-only demo or HF Spaces |
| Free API tiers may use your inputs to improve products | Use only public/authorized/synthetic documents; read each provider's data terms first |
| LangSmith trace quota | Sample traces; trace only on eval/demo runs; never log PII |

## Deployment recommendation
- **Dev:** everything in `infra/docker-compose.yml` (Postgres, Redis, Qdrant) + local backend/frontend.
- **Public demo:** frontend on Vercel/Cloudflare Pages; backend on a free Docker host; Neon (Postgres), Upstash (Redis), Qdrant Cloud (free). Also record a demo video as insurance against free-tier sleep.
