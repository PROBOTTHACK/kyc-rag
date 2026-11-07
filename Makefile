# Agentic KYC Compliance Engine — dev commands.
# Uses the repo-local virtualenv (.venv); create it with `make venv install`.

ifeq ($(OS),Windows_NT)
  PY := .venv/Scripts/python
else
  PY := .venv/bin/python
endif
COMPOSE := docker compose -f infra/docker-compose.yml
BACKEND := backend

.PHONY: venv install up down migrate ingest api test test-integration lint fmt eval web

venv:
	python -m venv .venv

install:
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "$(BACKEND)[dev]"

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

migrate:
	cd $(BACKEND) && ../$(PY) -m alembic upgrade head

ingest:
	@echo "make ingest: not implemented until Milestone 1 (docs/12)" && exit 1

# psycopg async needs a SelectorEventLoop (Windows defaults to Proactor), hence --loop.
api:
	cd $(BACKEND) && ../$(PY) -m uvicorn app.main:app --reload --loop app.core.loop:selector_loop_factory

test:
	cd $(BACKEND) && ../$(PY) -m pytest

test-integration:
	cd $(BACKEND) && ../$(PY) -m pytest -m integration

lint:
	cd $(BACKEND) && ../$(PY) -m ruff check . && ../$(PY) -m black --check . && ../$(PY) -m mypy app

fmt:
	cd $(BACKEND) && ../$(PY) -m ruff check --fix . && ../$(PY) -m black .

eval:
	@echo "make eval: not implemented until Milestone 6 (docs/12)" && exit 1

web:
	@echo "make web: not implemented until Milestone 5 (docs/12)" && exit 1
