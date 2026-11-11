"""Opt-in LLM response cache for allowlisted synthetic inputs only (M3 decision D8).

Rules (M3_PLAN §8):
- Off unless `LLM_CACHE_ENABLED=true`.
- Even when on, a request is cached only if it carries `synthetic_input_digest` (set by
  the workflow for `synthetic_demo` sessions) AND that digest is in the committed
  allowlist file (`LLM_CACHE_ALLOWLIST`, rows marked `"synthetic": true`). The allowlist
  is re-checked here; no regex or "looks clean" test ever makes arbitrary text cacheable.
- Keys are hashes only (`kyc:llm:{sha256(...)}`): no prompt text, no query, no PII.
  The key covers the prompt id/version, provider, model, schema and the SHA-256 of the
  full system and user text, so different evidence never shares an entry.
- Values hold the validated structured output (derived from synthetic input and public
  corpus text) plus the answering model, never the raw model text. Every hit is
  re-validated against the schema; an unusable entry is deleted and treated as a miss.
- Redis is never a correctness dependency: any error or timeout is a miss and the call
  goes to the provider; the workflow's output validation runs on cached and live results
  alike. TTL is at most 24 h.
"""

import asyncio
import hashlib
import json
import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.config import Settings
from app.llm.common import T, text_sha256
from app.llm.provider import LLMProvider, LLMRequest, LLMResult

log = logging.getLogger(__name__)
KEY_PREFIX = "kyc:llm"
VALUE_FORMAT = 1


def normalize_input(text: str) -> str:
    """The form a synthetic input is hashed in: NFKC, whitespace collapsed, case-folded."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def synthetic_input_digest(text: str) -> str:
    return hashlib.sha256(normalize_input(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyntheticAllowlist:
    """Digests of the committed synthetic demo/benchmark inputs."""

    digests: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: Path) -> "SyntheticAllowlist":
        """Rows: `{"id": ..., "query": ..., "synthetic": true}`. A missing file is an
        empty allowlist (nothing is cached). A row not marked synthetic is an error."""
        if not path.exists():
            return cls()
        digests: set[str] = set()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("synthetic") is not True:
                raise ValueError(f"{path.name}:{n}: every allowlist row must be synthetic: true")
            query = row.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError(f"{path.name}:{n}: missing query")
            digests.add(synthetic_input_digest(query))
        return cls(frozenset(digests))

    def __contains__(self, digest: object) -> bool:
        return digest in self.digests


@dataclass
class LLMCacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    ineligible: int = 0  # not an allowlisted synthetic input: never read or written
    bypassed: int = 0  # Redis unreachable / timed out / errored
    rejected: int = 0  # entry present but unusable


@dataclass
class CachedLLMProvider:
    inner: LLMProvider
    client: Any  # redis.asyncio.Redis, or a test double with get/set/delete
    allowlist: SyntheticAllowlist
    ttl_s: int = 86400
    op_timeout_s: float = 0.25
    stats: LLMCacheStats = field(default_factory=LLMCacheStats)

    def __post_init__(self) -> None:
        if not 0 < self.ttl_s <= 86400:
            raise ValueError("ttl_s must be in (0, 86400]")

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    def eligible(self, request: LLMRequest) -> bool:
        digest = request.synthetic_input_digest
        return digest is not None and digest in self.allowlist

    def key(self, request: LLMRequest, schema: type[Any]) -> str:
        schema_sha = text_sha256(json.dumps(schema.model_json_schema(), sort_keys=True))
        material = json.dumps(
            [
                request.prompt_id,
                request.prompt_version,
                self.inner.name,
                self.inner.model_id,
                schema.__name__,
                schema_sha,
                text_sha256(request.system),
                text_sha256(request.user),
                request.max_output_tokens,
            ]
        )
        return f"{KEY_PREFIX}:{text_sha256(material)}"

    async def generate_structured(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        if not self.eligible(request):
            self.stats.ineligible += 1
            return await self.inner.generate_structured(request, schema)
        key = self.key(request, schema)
        cached = await self._get(key, schema)
        if cached is not None:
            self.stats.hits += 1
            return cached
        self.stats.misses += 1
        result = await self.inner.generate_structured(request, schema)
        if result.parsed is not None and not result.validation_errors:
            await self._set(key, schema, result)
        return result

    async def _get(self, key: str, schema: type[T]) -> LLMResult[T] | None:
        try:
            raw = await asyncio.wait_for(self.client.get(key), timeout=self.op_timeout_s)
        except (RedisError, OSError, TimeoutError) as exc:
            self.stats.bypassed += 1
            log.warning("LLM cache bypassed (get): %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or value.get("format") != VALUE_FORMAT
                or value.get("schema") != schema.__name__
            ):
                raise ValueError("unexpected cache entry")
            parsed = schema.model_validate(value["parsed"])
            return LLMResult[schema](  # type: ignore[valid-type]
                parsed=parsed,
                raw_text_sha256=str(value["raw_text_sha256"]),
                provider=str(value["provider"]),
                model_id=str(value["model_id"]),
                latency_ms=0,
                finish_reason="cache",
                cache_hit=True,
                attempts=0,
            )
        except (ValueError, TypeError, KeyError, ValidationError):
            await self._reject(key)
            return None

    async def _set(self, key: str, schema: type[T], result: LLMResult[T]) -> None:
        if result.parsed is None:
            return
        value = {
            "format": VALUE_FORMAT,
            "schema": schema.__name__,
            "provider": result.provider,
            "model_id": result.model_id,
            "raw_text_sha256": result.raw_text_sha256,
            "parsed": result.parsed.model_dump(mode="json"),
        }
        try:
            await asyncio.wait_for(
                self.client.set(key, json.dumps(value, sort_keys=True), ex=self.ttl_s),
                timeout=self.op_timeout_s,
            )
            self.stats.writes += 1
        except (RedisError, OSError, TimeoutError) as exc:
            self.stats.bypassed += 1
            log.warning("LLM cache bypassed (set): %s", type(exc).__name__)

    async def _reject(self, key: str) -> None:
        self.stats.rejected += 1
        try:
            await asyncio.wait_for(self.client.delete(key), timeout=self.op_timeout_s)
        except (RedisError, OSError, TimeoutError):
            self.stats.bypassed += 1


def cached_from_settings(settings: Settings, provider: LLMProvider) -> LLMProvider:
    """`provider` itself when the cache is off; otherwise wrapped (allowlist loaded now)."""
    if not settings.LLM_CACHE_ENABLED:
        return provider
    client = aioredis.Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        socket_timeout=settings.LLM_CACHE_TIMEOUT_S,
        socket_connect_timeout=settings.LLM_CACHE_TIMEOUT_S,
        retry_on_timeout=False,
    )
    return CachedLLMProvider(
        inner=provider,
        client=client,
        allowlist=SyntheticAllowlist.load(settings.LLM_CACHE_ALLOWLIST),
        ttl_s=settings.LLM_CACHE_TTL_S,
        op_timeout_s=settings.LLM_CACHE_TIMEOUT_S,
    )
