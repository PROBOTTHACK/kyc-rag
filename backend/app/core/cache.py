"""Best-effort Redis JSON cache (M2 G4; docs/02 "Where Redis is used").

Never a correctness dependency: every Redis error, timeout or undecodable value is
reported as a miss (`bypassed` / `rejected`) and the caller falls back to Postgres, the
only system of record. Values hold only what the caller stores (no secrets); the Redis
URL is never logged.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import redis

from app.config import Settings

log = logging.getLogger(__name__)
KEY_PREFIX = "kyc:form_schema"


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    bypassed: int = 0  # Redis unreachable / timed out / errored
    rejected: int = 0  # entry present but unusable (corrupt, stale, mismatched)


@dataclass
class JsonCache:
    client: Any  # redis.Redis (sync), or a test double with get/set/delete
    ttl_s: int
    prefix: str = KEY_PREFIX
    stats: CacheStats = field(default_factory=CacheStats)

    @classmethod
    def from_settings(cls, settings: Settings) -> "JsonCache | None":
        """None when the cache is disabled; otherwise a client with short timeouts."""
        if not settings.FORM_CACHE_ENABLED:
            return None
        client = redis.Redis.from_url(
            settings.REDIS_URL.get_secret_value(),
            socket_timeout=settings.FORM_CACHE_TIMEOUT_S,
            socket_connect_timeout=settings.FORM_CACHE_TIMEOUT_S,
            retry_on_timeout=False,
        )
        return cls(client, ttl_s=settings.FORM_CACHE_TTL_S)

    def key(self, *parts: object) -> str:
        return ":".join([self.prefix, *(str(p) for p in parts)])

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self.client.get(key)
        except (redis.RedisError, OSError) as exc:
            self.stats.bypassed += 1
            log.warning("form cache bypassed (get): %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            self.reject(key)
            return None
        if not isinstance(value, dict):
            self.reject(key)
            return None
        return value

    def set(self, key: str, value: dict[str, Any]) -> None:
        try:
            self.client.set(key, json.dumps(value, sort_keys=True, separators=(",", ":")),
                            ex=self.ttl_s)  # fmt: skip
            self.stats.writes += 1
        except (redis.RedisError, OSError) as exc:
            self.stats.bypassed += 1
            log.warning("form cache bypassed (set): %s", type(exc).__name__)

    def reject(self, key: str) -> None:
        """Count an unusable entry and delete it (best effort)."""
        self.stats.rejected += 1
        try:
            self.client.delete(key)
        except (redis.RedisError, OSError):
            self.stats.bypassed += 1
