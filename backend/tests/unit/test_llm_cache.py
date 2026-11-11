"""M3 S1 (decision D8): the opt-in LLM cache caches only allowlisted synthetic inputs.

Redis is faked (an in-memory double that records every operation); the real Redis is
exercised in tests/integration/test_llm_redis.py.
"""

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import REPO_ROOT, Settings
from app.llm.cache import (
    CachedLLMProvider,
    SyntheticAllowlist,
    cached_from_settings,
    normalize_input,
    synthetic_input_digest,
)
from app.llm.fake import scripted_llm
from app.llm.provider import LLMRequest

EXAMPLE_ENV = REPO_ROOT / "infra" / ".env.example"
DEMO = "What KYC documents does a synthetic LLP need for a current account?"
SYSTEM = "SYSTEM-MARKER fixed instruction"
USER = f"USER-MARKER {DEMO} <evidence id='c1'>public text</evidence>"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


VALID = Answer(items=["partnership deed"])


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.ops: list[tuple[str, str]] = []
        self.fail = fail

    async def get(self, key: str) -> str | None:
        self.ops.append(("get", key))
        if self.fail:
            raise RedisConnectionError("down")
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        self.ops.append(("set", key))
        if self.fail:
            raise RedisConnectionError("down")
        assert 0 < ex <= 86400
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.ops.append(("delete", key))
        self.data.pop(key, None)


def request(digest: str | None, user: str = USER) -> LLMRequest:
    return LLMRequest(prompt_id="intake", prompt_version="1", system=SYSTEM, user=user,
                      synthetic_input_digest=digest)  # fmt: skip


ALLOW = SyntheticAllowlist(frozenset({synthetic_input_digest(DEMO)}))


def wrap(*responses: Any, client: FakeRedis | None = None) -> tuple[CachedLLMProvider, Any]:
    inner = scripted_llm(*responses)
    return CachedLLMProvider(inner=inner, client=client or FakeRedis(), allowlist=ALLOW), inner


def call(p: CachedLLMProvider, r: LLMRequest) -> Any:
    return asyncio.run(p.generate_structured(r, Answer))


def test_normalisation_and_digest() -> None:
    assert normalize_input("  What\tKYC\n DOCS ") == "what kyc docs"
    assert synthetic_input_digest("What KYC docs") == synthetic_input_digest(" what  kyc DOCS ")


def test_requests_without_a_digest_never_touch_the_cache() -> None:
    p, inner = wrap(VALID, VALID)
    call(p, request(None))
    call(p, request(None))
    assert p.client.ops == [] and len(inner.requests) == 2 and p.stats.ineligible == 2


def test_a_digest_not_in_the_allowlist_is_never_cached_even_if_it_looks_clean() -> None:
    clean_but_unlisted = synthetic_input_digest("What documents does a trust need?")
    p, inner = wrap(VALID, VALID)
    call(p, request(clean_but_unlisted))
    call(p, request(clean_but_unlisted))
    assert p.client.ops == [] and len(inner.requests) == 2


def test_allowlisted_input_misses_then_hits_with_identical_validated_output() -> None:
    p, inner = wrap(VALID)
    first = call(p, request(synthetic_input_digest(DEMO)))
    second = call(p, request(synthetic_input_digest(DEMO)))
    assert len(inner.requests) == 1
    assert first.cache_hit is False and second.cache_hit is True and second.attempts == 0
    assert second.parsed == first.parsed == VALID
    assert (second.provider, second.model_id) == (first.provider, first.model_id)
    assert (p.stats.misses, p.stats.hits, p.stats.writes) == (1, 1, 1)


def test_keys_and_values_hold_no_prompt_or_query_text() -> None:
    p, _ = wrap(VALID)
    call(p, request(synthetic_input_digest(DEMO)))
    [(key, value)] = p.client.data.items()
    assert key.startswith("kyc:llm:") and len(key) == len("kyc:llm:") + 64
    for marker in ("SYSTEM-MARKER", "USER-MARKER", "synthetic LLP", "evidence"):
        assert marker not in key and marker not in value
    assert set(json.loads(value)) == {"format", "schema", "provider", "model_id",
                                      "raw_text_sha256", "parsed"}  # fmt: skip


def test_different_prompt_text_never_shares_an_entry() -> None:
    p, inner = wrap(VALID, VALID)
    call(p, request(synthetic_input_digest(DEMO)))
    call(p, request(synthetic_input_digest(DEMO), user=USER + " other evidence"))
    assert len(inner.requests) == 2 and len(p.client.data) == 2


def test_invalid_output_is_never_cached() -> None:
    p, inner = wrap('{"items": 1}', VALID)
    assert call(p, request(synthetic_input_digest(DEMO))).parsed is None
    assert p.client.data == {}
    assert call(p, request(synthetic_input_digest(DEMO))).parsed == VALID
    assert len(inner.requests) == 2


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        json.dumps({"format": 1, "schema": "Other", "parsed": {"items": []}}),
        json.dumps({"format": 2, "schema": "Answer", "parsed": {"items": []}}),
        json.dumps({"format": 1, "schema": "Answer", "parsed": {"items": "x"},
                    "provider": "p", "model_id": "m", "raw_text_sha256": "0"}),  # fmt: skip
    ],
)
def test_unusable_entries_are_deleted_and_treated_as_a_miss(stored: str) -> None:
    p, inner = wrap(VALID)
    key = p.key(request(synthetic_input_digest(DEMO)), Answer)
    p.client.data[key] = stored
    r = call(p, request(synthetic_input_digest(DEMO)))
    assert r.parsed == VALID and r.cache_hit is False and len(inner.requests) == 1
    assert ("delete", key) in p.client.ops and p.stats.rejected == 1


def test_redis_outage_gives_the_same_result_from_the_provider() -> None:
    p, inner = wrap(VALID, VALID, client=FakeRedis(fail=True))
    a = call(p, request(synthetic_input_digest(DEMO)))
    b = call(p, request(synthetic_input_digest(DEMO)))
    assert a.parsed == b.parsed == VALID and not a.cache_hit and not b.cache_hit
    assert len(inner.requests) == 2 and p.stats.bypassed == 4  # get + set, twice


def test_allowlist_file_rules(tmp_path: Path) -> None:
    assert SyntheticAllowlist.load(tmp_path / "missing.jsonl").digests == frozenset()
    good = tmp_path / "ok.jsonl"
    good.write_text(json.dumps({"id": "d1", "query": DEMO, "synthetic": True}) + "\n\n")
    assert synthetic_input_digest(DEMO) in SyntheticAllowlist.load(good)
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "d1", "query": DEMO}) + "\n")
    with pytest.raises(ValueError, match="synthetic: true"):
        SyntheticAllowlist.load(bad)


def test_cache_is_off_by_default_and_wrapping_is_opt_in() -> None:
    inner = scripted_llm()
    off = Settings(_env_file=EXAMPLE_ENV)  # type: ignore[call-arg]
    assert cached_from_settings(off, inner) is inner
    on = Settings(_env_file=EXAMPLE_ENV, LLM_CACHE_ENABLED=True,  # type: ignore[call-arg]
                  GROQ_DATA_TERMS_VERIFIED_ON=dt.date(2026, 11, 11))  # fmt: skip
    wrapped = cached_from_settings(on, inner)
    assert isinstance(wrapped, CachedLLMProvider) and wrapped.inner is inner
    assert on.LLM_CACHE_TTL_S <= 86400
