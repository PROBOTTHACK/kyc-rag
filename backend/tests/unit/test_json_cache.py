"""M2 G4: JsonCache never raises and never trusts undecodable values (no Redis needed)."""

from typing import Any

import pytest
import redis

from app.core.cache import KEY_PREFIX, JsonCache


class Broken:
    def get(self, key: str) -> Any:
        raise redis.ConnectionError("down")

    def set(self, *a: Any, **k: Any) -> Any:
        raise redis.TimeoutError("slow")

    def delete(self, key: str) -> Any:
        raise OSError("gone")


class Memory:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int) -> None:
        self.data[key], self.ttls[key] = value.encode(), ex

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def test_key_layout() -> None:
    c = JsonCache(Memory(), ttl_s=10)
    assert c.key("form", "gemini_multimodal", "v2") == f"{KEY_PREFIX}:form:gemini_multimodal:v2"


def test_failures_are_counted_never_raised() -> None:
    c = JsonCache(Broken(), ttl_s=10)
    assert c.get("k") is None
    c.set("k", {"a": 1})
    c.reject("k")
    assert (c.stats.bypassed, c.stats.rejected, c.stats.writes) == (3, 1, 0)


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"', b"\xff\xfe"])
def test_undecodable_or_non_object_values_are_rejected_and_deleted(raw: bytes) -> None:
    mem = Memory()
    mem.data["k"] = raw
    c = JsonCache(mem, ttl_s=10)
    assert c.get("k") is None and "k" not in mem.data and c.stats.rejected == 1


def test_round_trip_with_ttl() -> None:
    mem = Memory()
    c = JsonCache(mem, ttl_s=42)
    c.set("k", {"b": 2, "a": 1})
    assert c.get("k") == {"a": 1, "b": 2} and mem.ttls["k"] == 42 and c.stats.writes == 1
