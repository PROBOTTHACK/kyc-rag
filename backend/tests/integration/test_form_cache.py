"""M2 G4: Redis read-through cache for stored form schemas.

Real Postgres (throwaway, migrated) and the real Redis from `make up`, isolated by a
unique key prefix that is removed afterwards. An unreachable Redis is simulated with a
client pointed at a closed port. Covers hits, misses, outage, version coexistence (a new
immutable version is never shadowed by a cached older one), consistency (corrupt,
tampered, mismatched and deleted entries), the m1_chunks default and secret exclusion.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.core.cache import JsonCache
from app.db.corpus import document_row
from app.db.forms import (
    load_form_schema,
    load_form_schema_cached,
    save_form_schema,
)
from app.db.models import FormSchemaRow
from app.schemas.forms import FormSchema
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

pytestmark = pytest.mark.integration
GEMINI = "gemini_multimodal"


@pytest.fixture
def db(temp_db: str) -> Iterator[tuple[sessionmaker[Session], FormSchema]]:
    command.upgrade(alembic_cfg(), "head")
    engine = sync_engine(temp_db)
    sessions = sessionmaker(engine, expire_on_commit=False)
    meta = form_doc("hdfc_cache_test_form")
    m1 = schema(meta, [rec(meta, 0, "Fields: *Name | Surname", section="S", kind=FIELDS)])
    with sessions() as s:
        s.add(document_row(meta, quality="text", page_count=1))
        s.flush()
        save_form_schema(s, m1)
        save_form_schema(s, gemini(m1, 1))
        s.commit()
    yield sessions, m1
    engine.dispose()


def gemini(base: FormSchema, version: int, title: str | None = None) -> FormSchema:
    return base.model_copy(
        update={
            "extraction_method": GEMINI,
            "extractor_version": version,
            "title": title or base.title,
        }
    )


@pytest.fixture
def cache() -> Iterator[JsonCache]:
    client = redis.Redis.from_url(get_settings().REDIS_URL.get_secret_value(), socket_timeout=1)
    c = JsonCache(client, ttl_s=60, prefix=f"test:{uuid.uuid4().hex}")
    yield c
    for key in client.scan_iter(f"{c.prefix}:*"):
        client.delete(key)


def load(sessions: sessionmaker[Session], c: JsonCache | None, **kw: Any) -> FormSchema | None:
    with sessions() as s:
        return load_form_schema_cached(s, c, "hdfc_cache_test_form", **kw)


def truth(sessions: sessionmaker[Session], **kw: Any) -> FormSchema | None:
    with sessions() as s:
        return load_form_schema(s, "hdfc_cache_test_form", **kw)


def test_miss_then_hit_returns_exactly_the_postgres_schema(db: Any, cache: JsonCache) -> None:
    sessions, _ = db
    first = load(sessions, cache, extraction_method=GEMINI)
    assert (cache.stats.misses, cache.stats.hits, cache.stats.writes) == (1, 0, 1)
    second = load(sessions, cache, extraction_method=GEMINI)
    assert (cache.stats.misses, cache.stats.hits) == (1, 1)
    assert first == second == truth(sessions, extraction_method=GEMINI)
    key = cache.key("hdfc_cache_test_form", GEMINI, "v1")
    assert cache.client.exists(key) and 0 < cache.client.ttl(key) <= 60


def test_default_method_is_m1_chunks_and_keys_never_mix(db: Any, cache: JsonCache) -> None:
    sessions, m1 = db
    assert load(sessions, cache) == m1
    assert load(sessions, cache, extraction_method=GEMINI) == gemini(m1, 1)
    keys = sorted(k.decode() for k in cache.client.scan_iter(f"{cache.prefix}:*"))
    assert keys == [
        cache.key("hdfc_cache_test_form", GEMINI, "v1"),
        cache.key("hdfc_cache_test_form", "m1_chunks", "v1"),
    ]


def test_a_new_version_is_never_shadowed_by_a_cached_older_one(db: Any, cache: JsonCache) -> None:
    sessions, m1 = db
    assert load(sessions, cache, extraction_method=GEMINI).extractor_version == 1  # type: ignore[union-attr]
    with sessions() as s:  # immutable new version inserted after v1 was cached
        save_form_schema(s, gemini(m1, 2, title="v2 title"))
        s.commit()
    latest = load(sessions, cache, extraction_method=GEMINI)
    assert latest is not None and latest.extractor_version == 2 and latest.title == "v2 title"
    assert load(sessions, cache, extraction_method=GEMINI, extractor_version=1) == gemini(m1, 1)
    assert cache.stats.hits == 1  # the explicit v1 read; latest was a miss, never stale
    assert load(sessions, cache, extraction_method=GEMINI) == latest and cache.stats.hits == 2


def _key(cache: JsonCache, version: int = 1) -> str:
    return cache.key("hdfc_cache_test_form", GEMINI, f"v{version}")


def _tamper(cache: JsonCache, mutate: Any) -> None:
    key = _key(cache)
    value = json.loads(cache.client.get(key))
    mutate(value)
    cache.client.set(key, json.dumps(value))


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda c: c.client.set(_key(c), b"\xff not json"),
        lambda c: c.client.set(_key(c), json.dumps([1, 2])),
        lambda c: _tamper(c, lambda v: v["schema_json"].__setitem__("title", "forged")),
        lambda c: _tamper(c, lambda v: v.__setitem__("schema_sha256", "0" * 64)),
        lambda c: _tamper(c, lambda v: v.__setitem__("row_id", str(uuid.uuid4()))),
        lambda c: _tamper(c, lambda v: v.__setitem__("format", 999)),
        lambda c: _tamper(c, lambda v: v.__setitem__("schema_json", {"sections": "x"})),
    ],
)
def test_unusable_entries_are_rejected_deleted_and_rebuilt(
    db: Any, cache: JsonCache, corrupt: Any
) -> None:
    sessions, m1 = db
    load(sessions, cache, extraction_method=GEMINI)
    corrupt(cache)
    got = load(sessions, cache, extraction_method=GEMINI)
    assert got == gemini(m1, 1) == truth(sessions, extraction_method=GEMINI)
    assert cache.stats.rejected == 1 and cache.stats.hits == 0
    assert load(sessions, cache, extraction_method=GEMINI) == got and cache.stats.hits == 1


def test_a_deleted_row_is_never_served_from_cache(db: Any, cache: JsonCache) -> None:
    sessions, _ = db
    load(sessions, cache, extraction_method=GEMINI)
    with sessions() as s:
        s.execute(sa.delete(FormSchemaRow).where(FormSchemaRow.extraction_method == GEMINI))
        s.commit()
    assert load(sessions, cache, extraction_method=GEMINI) is None
    assert cache.stats.hits == 0


def test_redis_outage_falls_back_to_postgres_with_identical_results(db: Any) -> None:
    sessions, m1 = db
    down = JsonCache(
        redis.Redis(host="127.0.0.1", port=1, socket_timeout=0.2, socket_connect_timeout=0.2),
        ttl_s=60,
    )
    for kw in (
        {},
        {"extraction_method": GEMINI},
        {"extraction_method": GEMINI, "extractor_version": 1},
    ):
        assert load(sessions, down, **kw) == truth(sessions, **kw) == load(sessions, None, **kw)
    assert down.stats.hits == 0 and down.stats.bypassed >= 3  # no exception surfaced


def test_disabled_cache_is_a_plain_postgres_read(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions, m1 = db
    monkeypatch.setenv("FORM_CACHE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert JsonCache.from_settings(get_settings()) is None
    finally:
        get_settings.cache_clear()
    assert load(sessions, None) == m1


def test_cached_values_hold_no_secrets(db: Any, cache: JsonCache) -> None:
    sessions, _ = db
    load(sessions, cache)
    load(sessions, cache, extraction_method=GEMINI)
    settings = get_settings()
    secrets = [settings.DATABASE_URL.get_secret_value(), settings.REDIS_URL.get_secret_value()]
    secrets += [
        v.get_secret_value()
        for v in (settings.GEMINI_API_KEY, settings.GROQ_API_KEY, settings.LANGSMITH_API_KEY)
        if v is not None
    ]
    url = sa.engine.make_url(settings.DATABASE_URL.get_secret_value())
    if url.password:
        secrets.append(url.password)
    for key in cache.client.scan_iter(f"{cache.prefix}:*"):
        raw = cache.client.get(key).decode()
        assert set(json.loads(raw)) == {"format", "row_id", "schema_sha256", "schema_json"}
        assert not any(s and s in raw for s in secrets)
        assert not any(s and s in key.decode() for s in secrets)
