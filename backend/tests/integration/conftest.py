"""Integration fixtures: real Postgres / Qdrant from `make up`.

`temp_db` gives each test its own throwaway database (created and dropped here), so
tests never touch the development database.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.ingestion import cli
from app.ingestion.manifest import load_manifest
from app.retrieval.qdrant_store import ChunkStore, store_from_settings
from tests.unit.test_indexing import FakeEmbedder

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _synthetic_connection_env() -> None:
    """Override the unit-test fixture: integration tests use the real .env settings."""


def alembic_cfg() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def sync_engine(url: str) -> sa.Engine:
    return sa.create_engine(url, poolclass=sa.pool.NullPool)


@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    base = make_url(get_settings().DATABASE_URL.get_secret_value())
    name = f"kyc_test_{uuid.uuid4().hex[:12]}"
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = base.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()  # alembic env.py reads get_settings()
    try:
        yield url
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def migrated(temp_db: str) -> sa.Engine:
    command.upgrade(alembic_cfg(), "head")
    return sync_engine(temp_db)


# --- the full corpus (M2: keyword and hybrid tests) ------------------------------------
# A migrated throwaway database holding the whole ingested corpus (real PDFs, Stages A-C)
# and a throwaway collection on the docker Qdrant with `FakeEmbedder` vectors. Module
# scoped: each test module that requests them gets its own copy, deleted afterwards.


@pytest.fixture(scope="module")
def corpus_db() -> Iterator[sessionmaker[Session]]:
    """A migrated throwaway database holding the full ingested corpus, and its store."""
    base = make_url(get_settings().DATABASE_URL.get_secret_value())
    name = f"kyc_test_{uuid.uuid4().hex[:12]}"
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = base.set(database=name).render_as_string(hide_password=False)
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = sync_engine(url)
    try:
        command.upgrade(alembic_cfg(), "head")
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        mp.undo()
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def corpus_store(corpus_db: sessionmaker[Session]) -> Iterator[ChunkStore]:
    base = store_from_settings(get_settings())
    s = ChunkStore(base.client, f"test_{uuid.uuid4().hex[:12]}")
    try:
        keys = [e.document_key for e in load_manifest(get_settings().CORPUS_MANIFEST)]
        assert (
            cli._run(get_settings(), keys, embedder=FakeEmbedder(), store=s, sessions=corpus_db)
            == 0
        )
        yield s
    finally:
        if s.client.collection_exists(s.collection):
            s.client.delete_collection(s.collection)
