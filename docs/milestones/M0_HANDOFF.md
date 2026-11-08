# M0 Handoff: Scaffold

Status: **complete**, committed as `a5fbbf7` and tagged `v0.1.0-m0`. Verified on 2026-11-07 (Windows 11, Python 3.13.5, Docker 28.3.2).
This file records what M0 *actually* contains. For the project's rules and spec, see `CLAUDE.md` and `docs/`.

## 1. Project structure
```
Makefile                      dev commands (see §5)
.gitignore                    ignores .env, data/raw_pdfs/**/*.pdf, data/processed/*, .venv, caches
infra/docker-compose.yml      Postgres / Redis / Qdrant
infra/.env.example            non-secret defaults; also read by app config
backend/pyproject.toml        package `kyc-engine`, deps + ruff/black/mypy/pytest config
backend/alembic.ini, backend/alembic/   async Alembic (no revisions yet)
backend/app/
  main.py                     create_app() factory + lifespan; module-level `app`
  config.py                   Settings (pydantic-settings), get_settings()
  api/v1/router.py, health.py GET /api/v1/health
  core/health.py              check functions + run_health_checks()
  core/loop.py                selector_loop_factory (see §6)
  core/logging.py             basicConfig; DEBUG in dev
  db/session.py               create_engine(), create_sessionmaker()
  db/models.py                DeclarativeBase `Base` (no tables)
  schemas/health.py           CheckResult, HealthResponse
  llm/ ingestion/ retrieval/ rules/ verification/ security/ graph/nodes/   empty packages (docs/01 layout)
backend/tests/
  conftest.py                 autouse: synthetic DATABASE_URL/REDIS_URL for unit tests
  unit/test_config.py, unit/test_health.py
  integration/conftest.py     overrides the autouse fixture (uses real settings)
  integration/test_health_live.py
data/raw_pdfs/{regulatory,policy,forms,reference}/   9 PDFs, gitignored (0 PDFs tracked)
data/processed/.gitkeep, data/eval/.gitkeep, eval/.gitkeep, frontend/.gitkeep
```
The PDFs were moved from `data/raw_pdf/` to `data/raw_pdfs/`, and `hdfc_netbanking_company_application.pdf` from `forms/` to `reference/`, so the layout matches docs/14. **`manifest.yaml` does not exist yet.**

## 2. Infrastructure (`infra/docker-compose.yml`)
| Service | Image | Host port | Notes |
|---|---|---|---|
| postgres | `postgres:16-alpine` | **5433**→5432 | user/db `kyc`, dev password in compose; volume `pgdata`; healthcheck `pg_isready` |
| redis | `redis:7-alpine` | 6379 | persistence disabled (`--save ""`, no AOF); healthcheck `redis-cli ping` |
| qdrant | `qdrant/qdrant:v1.20.0` | 6333 (HTTP), 6334 (gRPC) | volume `qdrantdata`; no compose healthcheck; no collections yet |

Installed Python versions (`.venv`, repo root): fastapi 0.162.1, uvicorn 0.54.0, pydantic 2.14.3, pydantic-settings 2.16.2, SQLAlchemy 2.1.8, psycopg 3.3.8, alembic 1.20.1, redis 8.1.0, qdrant-client 1.20.0. Dev tools: pytest 9.2.0, pytest-asyncio 1.4.1.post1, httpx 0.28.1, ruff 0.17.4, black 26.10.1, mypy 2.4.1.
`pyproject.toml` uses `>=` lower bounds only (for example `qdrant-client>=1.10`); nothing is locked.

## 3. Backend foundation
- **Config:** `Settings` loads, highest precedence first: process env vars, then `<repo>/.env`, then `<repo>/infra/.env.example`. Paths are absolute from `REPO_ROOT`, so it works from any working directory. API keys and connection URLs are `SecretStr`. Blank keys and model IDs become `None`. `MAX_REGENERATIONS` is constrained to 0–2. `DATABASE_URL` and `REDIS_URL` are required and have no default in code. `get_settings()` is cached with `lru_cache`.
- **App:** `create_app(settings=None)` sets CORS for `FRONTEND_ORIGIN` and mounts routes under `/api/v1`. Lifespan creates the async engine, sessionmaker, Redis client and `AsyncQdrantClient` on `app.state`, then closes them on shutdown. It builds `app.state.health_checks` unless that is already set (tests pre-set it).
- **Endpoint:** only `GET /api/v1/health` exists. It runs the postgres (`SELECT 1`), redis (`PING`) and qdrant (`get_collections`) checks concurrently, each under `asyncio.wait_for(HEALTH_CHECK_TIMEOUT_S)` (default 2s). Response: `{"status":"ok"|"degraded","checks":{name:{ok,latency_ms,error}}}`, with HTTP 200 when all checks pass and 503 otherwise. `error` is the exception class name only.
- **DB:** `Base` has no tables. Alembic `env.py` takes its URL from `get_settings().DATABASE_URL` (`alembic.ini` has an empty `sqlalchemy.url`), sets `target_metadata = Base.metadata` and runs on a `SelectorEventLoop`. **No migration revisions exist**, so `upgrade head` is a no-op that verifies connectivity.

## 4. Testing (results as of this handoff)
| Command (run in `backend/` with `../.venv/Scripts/python -m …`) | Result |
|---|---|
| `pytest` (unit; `addopts` excludes `integration`) | 8 passed, 1 deselected, 1 warning |
| `pytest -m integration` (needs Docker services up) | 1 passed |
| `ruff check .` / `black --check .` / `mypy app` (strict) | clean / clean / no issues in 24 files |
| `alembic upgrade head` | exit 0 (no revisions) |

Unit tests cover these cases: config loads `.env.example` defaults; blank values become None; env vars override files; secrets are masked in repr and dump; `MAX_REGENERATIONS=5` is rejected; health returns 200 when all checks pass, returns 503 with `TimeoutError` on a hanging check, and never leaks connection details in errors.
I also checked the running server by hand: `uvicorn` with and without `--reload` returned 200 with all three checks ok. With Redis stopped it returned 503 with `redis: ConnectionError`. Nothing secret appeared in the server log.

## 5. Environment setup
```
python -m venv .venv && .venv/Scripts/python -m pip install -e "backend[dev]"   # = make venv install
docker compose -f infra/docker-compose.yml up -d --wait                          # = make up
cp infra/.env.example .env    # optional: only needed to add API keys; config falls back to .env.example
```
- Variables are those in `infra/.env.example`, plus `HEALTH_CHECK_TIMEOUT_S`, which M0 added. M0 needs only `DATABASE_URL`, `REDIS_URL`, `QDRANT_URL` and optionally `QDRANT_API_KEY`. The LLM, LangSmith and NLI variables are read but unused.
- There is currently **no root `.env`**; the app runs on `.env.example` defaults.
- **GNU Make is not installed on this machine** (`winget install ezwinports.make`). Until it is, run the commands behind each Makefile target directly. Targets: `venv install up down migrate api test test-integration lint fmt`, plus the stubs `ingest eval web`, which print "not implemented until Milestone 1/6/5 (docs/12)" and exit 1.

## 6. Technical decisions
- **Host port 5433 for Postgres:** a native Windows PostgreSQL service is listening on 0.0.0.0:5432 and was catching `localhost` connections ("password authentication failed for user kyc").
- **SelectorEventLoop everywhere:** psycopg async can't use Windows' ProactorEventLoop. `make api` passes `--loop app.core.loop:selector_loop_factory`. uvicorn ≥0.36 uses a custom `--loop` target directly as the loop factory, so the function must return a loop instance. Alembic and the integration test use the same loop.
- **psycopg 3 driver** (`postgresql+psycopg://`): matches `.env.example`, and the LangGraph Postgres checkpointer planned for M3 uses psycopg 3.
- **Qdrant pinned to v1.20.0:** the newest release at the time (pushed 2026-10-21), matching qdrant-client 1.20.0. `latest` had been resolving to a stale cached v1.15.3 locally. The volume was empty when the image was upgraded.
- **Health checks are injectable:** they live on `app.state.health_checks`, so unit tests swap in fakes and never need live services.
- **Only M0 dependencies were installed.** LangGraph, PyMuPDF, sentence-transformers and the rest are deferred to the milestones that need them.

## 7. Known limitations / warnings
- Test runs emit one `StarletteDeprecationWarning` ("Using `httpx` with `starlette.testclient` is deprecated; install `httpx2`"). It comes from FastAPI's TestClient import, and I took no action.
- Dependencies are not locked. `qdrant-client>=1.10` is looser than the pinned server; tightening it to `>=1.20,<1.21` was suggested but **not applied**.
- The Qdrant service has no compose healthcheck. `up --wait` only waits for the container to start.
- No DB tables or migrations, no logging redaction filter (code just avoids logging secrets), no rate limiter, cache, LLM provider or SSE yet.
- `frontend/` and `eval/` are placeholder directories only.
- The Docker services were left running after the session.

## 8. Git state
- Remote `origin`: `https://github.com/PROBOTTHACK/kyc-rag.git`.
- Commit `a5fbbf7` "M0: scaffold" is the only commit. `main`, `origin/main` and `feat/m1-document-ingestion` all point to it.
- Annotated tag `v0.1.0-m0` points to `a5fbbf7` and is on origin.
- Current branch: **`feat/m1-document-ingestion`**. This handoff file is uncommitted.

## 9. M1 starting point
**Ready:** package layout, settings (`EMBEDDING_MODEL`, `QDRANT_COLLECTION=policy_chunks` already defined), async engine/sessionmaker, Alembic wired to `Base.metadata`, a reachable Qdrant client, PDFs in the docs/14 layout, and the `make ingest` stub to replace.

**Not implemented:** `data/raw_pdfs/manifest.yaml`, `data/processed/corpus_report.md`, any ORM model or migration (documents/chunks), PDF extraction, OCR fallback, chunking, embeddings, the Qdrant collection or upserts, the ingestion/search CLIs, a labeled query set, and Recall@5.

**Do first** (CLAUDE.md, docs/12): inspect the real PDFs and write `data/processed/corpus_report.md`, then the manifest (unknown values stay `null`), and only then ingestion.
New dependencies (PyMuPDF, sentence-transformers, Tesseract binding) are in the locked stack, but CLAUDE.md says to ask before adding them.

**Read:** `docs/14-source-corpus.md` (corpus, manifest, `bank`/`status`, version handling), `docs/04-ingestion-pipelines.md` (Pipeline A, CLI `python -m app.ingestion.cli run --path data/raw_pdfs`), `docs/08-data-model.md` (tables), `docs/10-evaluation-plan.md` (Recall@5), `docs/12-claude-code-playbook.md` §M1.
