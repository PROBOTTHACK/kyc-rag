# Agentic KYC Compliance Engine — dev commands.
# Uses the repo-local virtualenv (.venv); create it with `make venv install`.
#
# Portability: GNU Make runs recipes with sh on Linux/macOS (and on Windows when sh.exe is
# on PATH, e.g. Git Bash) and with cmd.exe on native Windows PowerShell/CMD. cmd.exe can't
# run a relative program path written with forward slashes ("../.venv/Scripts/python" ->
# "'..' is not recognized"), so the interpreter is an ABSOLUTE, double-quoted path (the repo
# path may contain spaces), with backslashes on Windows. Both shells accept that form.
# Recipes only use `cd DIR && ...`, quoted arguments and `>>`, which mean the same in both.

ifeq ($(OS),Windows_NT)
  VENV_PY := $(subst /,\,$(CURDIR))\.venv\Scripts\python.exe
  PYTHON ?= python
else
  VENV_PY := $(CURDIR)/.venv/bin/python
  PYTHON ?= python3
endif
PY := "$(VENV_PY)"
COMPOSE := docker compose -f infra/docker-compose.yml
BACKEND := backend
CLI = cd $(BACKEND) && $(PY) -m app.ingestion.cli
# Optional document selection for ingestion targets: make ingest DOC="rbi_kyc_faqs hdfc_nri_rekyc"
DOC ?=
DOCS = $(foreach d,$(DOC),--document $(d))

.DEFAULT_GOAL := help
.PHONY: help venv install install-locked lock up down migrate check-schema ingest ingest-dry
.PHONY: persist index retry reconcile repair runs forms search check-corpus eval eval-compare eval-select eval-heldout check-dense-golden
.PHONY: api test test-integration lint fmt web

# Printed by make itself ($(info)), so it needs no shell and looks the same everywhere.
help:
	$(info Usage: make <target> [DOC="key ..."] [Q="query"] [MODE=dense|bm25|hybrid])
	$(info )
	$(info Setup:     venv install install-locked lock)
	$(info Services:  up down migrate check-schema)
	$(info Ingestion: ingest ingest-dry persist index retry reconcile repair runs check-corpus)
	$(info Forms:     forms [DOC="key ..."]  (then: cli forms list | show KEY))
	$(info Retrieval: search Q="..."  eval  eval-compare  eval-select  check-dense-golden)
	$(info Dev:       api test test-integration lint fmt)
	$(info )
	$(info Typical first run: make up migrate check-corpus ingest)
	$(info Interpreter: $(VENV_PY))

venv:
	$(PYTHON) -m venv .venv

install:
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "$(BACKEND)[dev]"

# Reproduce the tested environment exactly (backend/requirements.lock), then the project.
install-locked:
	$(PY) -m pip install -r "$(BACKEND)/requirements.lock"
	$(PY) -m pip install --no-deps -e "$(BACKEND)[dev]"

# Refresh the lockfile after a dependency change has passed `make test lint`.
# Keeps the header comment block; replaces the pinned list.
lock:
	cd $(BACKEND) && $(PY) -c "import pathlib; p=pathlib.Path('requirements.lock'); p.write_text(''.join(l for l in p.read_text().splitlines(True) if l.startswith('#')), newline='')"
	cd $(BACKEND) && $(PY) -m pip freeze --exclude-editable >> requirements.lock

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

migrate:
	cd $(BACKEND) && $(PY) -m alembic upgrade head

# Fails if the ORM models and the migrations disagree (schema drift).
check-schema:
	cd $(BACKEND) && $(PY) -m alembic check

# --- M1 ingestion (docs/04). Every target accepts DOC="key ..."; default: all documents. ---
# Full workflow: validate PDFs -> extract -> OCR -> chunk -> Postgres -> embed -> Qdrant -> verify.
ingest:
	$(CLI) run $(DOCS)

# Report what `ingest` would change; writes nothing.
ingest-dry:
	$(CLI) run --dry-run $(DOCS)

# Stages A-C + Postgres only; documents stay `pending` until indexed.
persist:
	$(CLI) persist $(DOCS)

# Index Qdrant from committed Postgres rows (no PDF is read).
index:
	$(CLI) index --from-db $(DOCS)

# One recovery attempt for every document that is not completed/partial.
retry:
	$(CLI) retry $(DOCS)

# Postgres <-> Qdrant comparison, read-only.
reconcile:
	$(CLI) reconcile

# Re-index inconsistent documents from Postgres (never deletes points of unknown documents).
repair:
	$(CLI) reconcile --repair

# M2 C2: extract and persist form schemas from stored M1 chunks [DOC="key ..."].
# Idempotent; a changed schema under an existing version is refused, never overwritten.
forms:
	$(CLI) forms extract $(DOCS)

runs:
	$(CLI) runs --limit 30

# make search Q="documents for opening a company account" [MODE=bm25|hybrid]
# MODE empty = RETRIEVAL_MODE from the settings (dense by default).
MODE ?=
MODE_ARG = $(if $(MODE),--mode $(MODE),)
search:
	$(CLI) search "$(Q)" --expand $(MODE_ARG)

# Manifest + PDF hashes, and a real Tesseract smoke test.
check-corpus:
	$(CLI) validate-manifest
	$(CLI) check-ocr

# psycopg async needs a SelectorEventLoop (Windows defaults to Proactor), hence --loop.
api:
	cd $(BACKEND) && $(PY) -m uvicorn app.main:app --reload --loop app.core.loop:selector_loop_factory

test:
	cd $(BACKEND) && $(PY) -m pytest

test-integration:
	cd $(BACKEND) && $(PY) -m pytest -m integration

lint:
	cd $(BACKEND) && $(PY) -m ruff check . && $(PY) -m black --check . && $(PY) -m mypy app

fmt:
	cd $(BACKEND) && $(PY) -m ruff check --fix . && $(PY) -m black .

# Retrieval benchmark on the development set (Recall@5 on data/eval/m1_retrieval.jsonl);
# MODE=bm25|hybrid evaluates another mode. The frozen held-out set is refused (docs/10).
eval:
	$(CLI) evaluate $(MODE_ARG) --report data/processed/$(if $(MODE),$(if $(filter dense,$(MODE)),m1_retrieval_report.md,m2_dev_$(MODE)_report.md),dev_retrieval_report.md)

# Dense vs BM25 vs hybrid on the development set at fixed A2 defaults (selects nothing).
eval-compare:
	$(CLI) evaluate --compare --report data/processed/m2_dev_compare_report.md

# M2 A3: the 7 registered configurations on the development set + the pre-registered rule.
# Writes the decision record committed before the one-time held-out evaluation (docs/10).
eval-select:
	$(CLI) evaluate --select --report data/processed/m2_a3_selection_report.md --record data/eval/m2_a3_selection.json

# ONE-TIME held-out evaluation of C0 + the A3-selected configuration (docs/10). Refuses
# to run again once data/eval/m2_heldout_results.json exists.
eval-heldout:
	$(CLI) evaluate --final-heldout --report data/processed/m2_heldout_report.md --record data/eval/m2_heldout_results.json

# M1 dense golden regression: real model + dev collection; fails (never skips) if missing.
check-dense-golden:
	cd $(BACKEND) && $(PY) -m pytest -m integration tests/integration/test_dense_golden.py

web:
	$(error make web: not implemented until Milestone 5 (docs/12))
