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

- M2 G2: `google-genai==2.33.0` (Apache-2.0, from the PyPI `License-Expression`; released 2026-10-23, Python ≥ 3.11). This is the newest release at least two weeks old on 2026-11-10. 2.34.0 (2026-10-29) and the new major 3.0.0 (2026-11-06, which switches to an `httpx2` dependency) were too recent. It is imported only inside `app/llm/gemini.py`, and lazily, so the app imports without it being loaded.

- **M3 S0 (2026-11-11):** `langgraph==1.2.18`, `langgraph-checkpoint-postgres==3.2.0` (both MIT) and `groq==1.7.0` (Apache-2.0), licences from the PyPI `License-Expression`. Each is the newest release at least two weeks old on 2026-11-11: langgraph 1.2.20 (2026-11-05) was too recent; checkpoint-postgres 3.2.0 is from 2026-10-14; groq 1.7.0 is from 2026-08-26 and is the latest.
  - **Transitive packages:** they were resolved with `pip install --uploaded-prior-to 2026-10-28`, so every package added is also at least two weeks old. They include `langchain-core 1.7.0`, `langsmith 0.14.14` and `langgraph-checkpoint 4.3.1` (all MIT), `ormsgpack`, `orjson`, `xxhash`, `zstandard` and `uuid_utils`, plus `psycopg-pool 3.3.3`. **psycopg-pool is LGPL-3.0-only:** it is used as an unmodified, separately installed library, which LGPL permits. Re-check this before redistributing a bundled binary.
  - **No existing pin changed:** the lock diff only adds lines.
  - **`langsmith` is installed but tracing stays off** (`LANGSMITH_TRACING=false`, M3 plan §8).
  - **Checkpointer API (checked from the installed source):** `AsyncPostgresSaver.setup()` creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations`, using unqualified table names. Placing them in a `langgraph` schema (decision D3) therefore relies on the connection's `search_path`, and S6 must test that. `adelete_thread(thread_id)` deletes a thread's rows from `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`, which is the deletion path for M3 retention (plan §10).

## Gemini API verification (2026-11-10, before any live call)
Sources read on 2026-11-10. The `ai.google.dev` URLs now redirect permanently to `aistudio.google.com/docs/...`.
- **Model ID:** `gemini-3.8-flash` is listed as the stable ID ("Stable: `gemini-3.8-flash`"; the model page says "Latest update: September 2026"; the What's-new page says it is GA).
  - Inputs: text, image, video, audio, PDF. Output: text.
  - Limits: 1,048,576 input tokens, 65,536 output tokens.
  - Structured outputs are supported.
  - Sources: https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash, https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.8.
- **Models actually used in the live runs (recorded 2026-11-11):**
  - **Primary:** the runs used the repo-root `.env` value `MULTIMODAL_MODEL=gemini-3.6-flash`, not `gemini-3.8-flash`. `gemini-3.6-flash` is listed as "Stable: `gemini-3.6-flash`", "a previous-generation Flash model that remains available for production use"; inputs include image; structured outputs supported; free tier "Free of charge" (https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash, read 2026-11-10). The same free-tier data terms apply.
  - **Fallback:** each run set `MULTIMODAL_FALLBACK_MODEL=gemini-3.5-flash-lite` for that process. The `.env` value `gemini-3.5-flash` was never used and has not been verified.
  - **Provenance:** every stored page records the model that answered it.
- **Pricing tier:** Gemini 3.8 Flash has a free tier (input and output "Free of charge"). The "Used to improve our products" row reads **Yes for the free tier, No for the paid tier**. Source: https://ai.google.dev/gemini-api/docs/pricing.
- **Data use (terms effective March 23, 2026):** for Unpaid Services, Google uses submitted content and responses "to provide, improve, and develop Google products and services and machine learning technologies", and "human reviewers may read, annotate, and process your API input and output". The terms also say: "Do not submit sensitive, confidential, or personal information to the Unpaid Services." Source: https://ai.google.dev/gemini-api/terms.
- **Rate limits:** applied per project, not per API key. RPD resets at midnight Pacific time. The page publishes **no numeric free-tier RPM/TPM/RPD** for this model; the actual values are shown only in AI Studio. The in-process limiter therefore keeps the conservative configured `RATE_LIMIT_GEMINI_RPM=10`; check the project's real limit in AI Studio before relying on more. Source: https://ai.google.dev/gemini-api/docs/rate-limits.
- **Suitability decision:** the free tier is acceptable **only** for the five approved public blank HDFC forms (`data/raw_pdfs/forms_approved.yaml`). These contain no personal or confidential data, and they may be used for product improvement and human review. Nothing else may be sent: no customer documents, filled forms, ID images or PII. Sending such data would need the paid tier and a separate decision.
- **Enforcement in code:** live calls stay off unless `GEMINI_LIVE_ENABLED=true` and `GEMINI_DATA_TERMS_VERIFIED_ON` (this review date) are set, along with the key and the model ID. The provider also enforces a 60 s timeout per attempt, the retry policy below and the RPM limiter. The key is a `SecretStr` and is never logged, chained into exceptions or stored.
- **Retry policy (owner-approved 2026-11-10, after the first live run failed with 503/429):** this is an explicit override, for Gemini transport retries only, of the CLAUDE.md default hard max of 2.
  - **Attempts:** 5 per model (1 + 4 retries; hard max `GEMINI_MAX_RETRIES ≤ 4`).
  - **Waits:** exponential with no jitter, 2 → 4 → 8 → 16 s (`GEMINI_BACKOFF_INITIAL_S=2`), each capped at `GEMINI_BACKOFF_CAP_S=32` (≤ 60).
  - **Which errors:** only 429, 5xx, timeouts and transport errors. Refusals, other 4xx and invalid output are never retried this way; invalid output keeps the pipeline's single re-ask.
- **Fallback model `gemini-3.5-flash-lite` (verified 2026-11-10):**
  - **Why it qualifies:** "Stable: `gemini-3.5-flash-lite`"; inputs text, image, video, audio, PDF; structured outputs supported; free tier "Free of charge" for input and output; no deprecation date shown. Sources: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite and https://ai.google.dev/gemini-api/docs/pricing.
  - **When it is used:** only after the primary exhausts its 5 attempts on transient errors. It gets the same 5-attempt policy and shares the same RPM limiter.
  - **Provenance:** each stored page records the model that answered it.
  - **Data use:** the same free-tier terms apply, so only the approved blank forms are ever sent.

## Groq API verification (2026-11-11, M3 S0, before any live call)
The official pages were read on 2026-11-11. Quotes are verbatim. **No live Groq call has been made.**

**Models.**
- **Configured:** `PRIMARY_LLM_MODEL=openai/gpt-oss-120b` and `FALLBACK_LLM_MODEL=openai/gpt-oss-20b` (`infra/.env.example` and `.env` agree).
- **Status:** both are under "Production Models" (https://console.groq.com/docs/models). Neither appears among preview or deprecated models, and neither is named by any entry on https://console.groq.com/docs/deprecations.
- **Limits:** each has a context window of 131,072 tokens and up to 65,536 completion tokens.
- **Deprecation can happen at any time:** the Services Agreement §4.4 says "Groq may deprecate the AI Model Services at any time". Model IDs therefore stay env-only.

**Structured output.** Both models are listed under "Models with Strict Mode (`strict: true`)". The page says strict mode "uses constrained decoding to guarantee schema-compliant output" (https://console.groq.com/docs/structured-outputs).
- **No streaming or tools with it:** "Streaming and tool use are not currently supported with Structured Outputs."
- **Pydantic validation still applies:** the plan's validation and its single re-ask stay in place regardless.

**Rate limits are not settled.**
- **Published figures:** https://console.groq.com/docs/rate-limits shows one table, described as "the base limits for the Developer plan". Both models are listed at RPM 30, RPD 1K, TPM 8K and TPD 200K.
- **Free-plan figures:** the free-plan tab could not be read.
- **Conflict:** the models page lists "250K TPM / 1K RPM" for the Developer plan.
- **Where the real limits are:** the per-organisation limits are shown only on the console's limits page.
- **Decision:** keep the configured `RATE_LIMIT_GROQ_RPM=20` (below every published figure). Check the organisation's real limits in the console before relying on more.
- **TPM 8K may be the binding limit:** synthesis prompts carry evidence text, so the S1 limiter must also respect a per-minute token budget, or prompts must be capped.

**Data use.**
- **Which terms apply:** https://groq.com/terms-of-use says the Groq Services Agreement governs cloud/API use. The privacy policy excludes data processed "as a 'data processor' on behalf of customers".
- **Training:** the Services Agreement §4.2 (https://console.groq.com/docs/legal/services-agreement) says: "Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models … unless explicitly granted permission or instructed by Customer."
- **Retention:** https://console.groq.com/docs/your-data says: "By default, Groq does not retain customer data for inference requests." Logs for reliability or abuse investigation "are retained for up to 30 days, unless legally required to retain longer". The same page says this storage can be opted out of in Data Controls.
- **Zero Data Retention:** the your-data page says "All customers may enable Zero Data Retention (ZDR)". The Services Agreement says "Eligible Customers". The two pages disagree, and plan eligibility was not confirmed.
- **Not established:** no page read says anything about human review of API content, or distinguishes free from paid API data handling.

**Suitability decision (M3).**
- **What may be sent:** these terms are better than Gemini's free tier (no training use), but they do **not** make Groq a place for real personal data. Retention for up to 30 days, the ZDR eligibility ambiguity and the absence of a human-review statement all leave that open.
- **M3 sends Groq only:** the **screened query** and **public corpus text**, under the synthetic-data-only policy (M3 plan §6). Clarification answers are enum values merged without an LLM call.
- **Live gate:** live calls stay off unless `GROQ_LIVE_ENABLED=true` and `GROQ_DATA_TERMS_VERIFIED_ON=2026-11-11` (this review) are set, alongside `GROQ_API_KEY`. The repo-root `.env` holds a Groq key (checked by length only, never printed). It is unused until S1 adds the gate.
- **Before a live run:** optionally enable ZDR, or opt out of log storage, in the console Data Controls.

## Groq live run (2026-11-12, M3 S8; the only live Groq session in M3)
- **Authorisation:** the owner opened the gate (`GROQ_LIVE_ENABLED`, `GROQ_DATA_TERMS_VERIFIED_ON` set by the owner) and explicitly authorised one run. Settings were checked by presence only.
- **Model check:** one metadata-only `models.list` call. `openai/gpt-oss-120b` and `openai/gpt-oss-20b` were both available (11 models listed).
- **The run:** `make demo-live` (synthetic `m3-demo-01`, enum answer `account_type=CURRENT_ACCOUNT`, public corpus text). **2 calls**, both on `openai/gpt-oss-120b` and both `ok`, with no cache hit and no fallback:
  - intake: 2153 ms, 623 input / 312 output tokens;
  - synthesis: 3154 ms, 2886 input / 1093 output tokens.
  - Far below every published limit.
- **Gemini:** not called. Forms came from stored schemas (D4).
- **Found:** in dev (`APP_ENV=dev`, root logger at DEBUG), `groq._base_client` logged the full request options, prompt included, to the API log. No key appeared. Fixed in `app/core/logging.py`: payload-logging libraries are held at WARNING in every environment, with a regression test. The local log file was deleted.

## Retention settings (M3 S8)
`REPORT_RETENTION_D=30` (days after session expiry) and `IDEMPOTENCY_TTL_H=24`. The purge is a CLI (`make purge`); there is no background worker.

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

**Implemented so far (M2 G4): the form-schema read cache** (`app/core/cache.py` `JsonCache`, `app/db/forms.py` `load_form_schema_cached`; used by `forms show`).
- **Key and value:** the key is `kyc:form_schema:{form_key}:{extraction_method}:v{extractor_version}`. The value is `{format, row_id, schema_sha256, schema_json}`, which is public form structure with no secrets. TTL is `FORM_CACHE_TTL_S` (7 days).
- **Postgres stays the source of truth.** Each read first asks Postgres, without the JSON payload, which row is current (id, version, SHA-256).
  - **When an entry is used:** only if its row id and SHA-256 match that row, and its JSON re-hashes to the same SHA-256.
  - **What that guarantees:** a newly inserted version is found at once and never shadowed by a cached older one, and a deleted row is never served.
- **Fallback:** corrupt, tampered or mismatched entries are deleted. Misses, those entries and any Redis error or timeout (`FORM_CACHE_TIMEOUT_S=0.25`) load from Postgres with an identical result, then refresh the entry.
- **Turning it off:** `FORM_CACHE_ENABLED=false` makes every read a plain Postgres read.
- **M3 S1:** the shared LLM rate limiter and the opt-in synthetic-only LLM cache are implemented (see "Provider abstraction" below).
- **Still to come in M3:** pub/sub progress events (S7). Run locks live in Postgres, not Redis (M3 decision D6).

## Provider abstraction (required)
```python
class LLMProvider(Protocol):          # app/llm/provider.py (M3 S1)
    async def generate_structured(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]: ...
class MultimodalProvider(Protocol):   # app/llm/multimodal.py (M2)
    async def analyze_pages(self, images: list[bytes], request: MultimodalRequest, schema: type[T]) -> MultimodalResult[T]: ...
```
**As implemented (M3 S1):**
- **Results:** they carry `parsed` (or `None` plus validation errors), the SHA-256 of the raw output, provider, the model that answered, usage, latency and `cache_hit`. Neither the prompt nor the raw output is kept.
- **`generate_text`:** not implemented. Every workflow call is structured.
- **Retries and fallback:** each provider retries transient failures itself. `FallbackLLMProvider` / `FallbackMultimodalProvider` try the fallback model only after the primary exhausts its retries on a transient failure.
- **Invalid output:** handled by `generate_validated`, which makes one re-ask that quotes the validation errors (never the previous output) and then returns the invalid result for the caller to degrade.
- **Groq stack:** `app.llm.groq.llm_from_settings` builds it as primary → fallback (one shared limiter) → opt-in cache. Models are swapped by env var only.

**Rate limiting and the LLM cache (M3 S1):**
- **`app/core/ratelimit.py`:**
  - **What it counts:** every attempt, retries included, reserves one request and its estimated tokens. The estimate is prompt characters / 3 plus `max_output_tokens`.
  - **Limits:** `RATE_LIMIT_GROQ_RPM=20` and `RATE_LIMIT_GROQ_TPM=6000`.
  - **Shared window:** one minute, shared through Redis (`rl:groq:{minute}`).
  - **If Redis fails:** any error falls back to an in-process sliding window with the same limits. The failure is counted and the results don't change.
  - **Bounded:** a wait longer than `RATE_LIMIT_MAX_WAIT_S` fails as a transient rate limit, and a request larger than the whole budget fails at once.
- **`app/llm/cache.py` (decision D8):**
  - **Off by default:** `LLM_CACHE_ENABLED=false`.
  - **When on, it caches only allowlisted inputs:** a request is cached only when it carries a `synthetic_input_digest` that appears in the committed allowlist (`LLM_CACHE_ALLOWLIST`, every row `"synthetic": true`). Nothing else ever reads or writes the cache, however clean the text looks.
  - **Keys and values:** keys are hashes; values hold the validated output and the answering model.
  - **Hits are re-validated,** and unusable entries are deleted.
  - **Redis errors** count as misses.

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
