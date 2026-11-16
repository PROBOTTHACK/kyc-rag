"""LangSmith observability (app/assistant/tracing.py), with a stub client: no network.

Checks the span tree of a turn, the project separation, that identifiers, raw contact
details, prompts and source passages are never uploaded, and that tracing that is off,
failing or slow never changes or breaks a turn (PostgreSQL / the store stays authoritative).
"""

import json
from typing import Any

import pytest

from app.assistant.a7.probes import RecordingLLM
from app.assistant.checklist import ChecklistService
from app.assistant.service import AssistantService
from app.assistant.tracing import DISABLED, Tracer, scrub, tracer_from_settings
from app.config import Settings
from tests.unit.test_assistant_api import MemoryStore
from tests.unit.test_assistant_checklist import FakeChunks

PAN = "ABCPE1234F"
EMAIL = "riya.sharma@example.com"


class StubClient:
    def __init__(self, fail: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.fail = fail

    def create_run(self, **kw: Any) -> None:
        if self.fail:
            raise ConnectionError("LangSmith unreachable")
        self.created.append(kw)

    def update_run(self, run_id: Any, **kw: Any) -> None:
        if self.fail:
            raise ConnectionError("LangSmith unreachable")
        self.updated.append({"id": run_id, **kw})

    def uploaded(self) -> str:
        return json.dumps([self.created, self.updated], default=str)


def service(tracer: Tracer, llm: Any = None) -> AssistantService:
    return AssistantService(
        MemoryStore(), lambda: ChecklistService(FakeChunks()), llm, tracer=tracer
    )


async def converse(s: AssistantService, *bodies: dict[str, Any]) -> list[dict[str, Any]]:
    sid = (await s.create_session())["session_id"]
    out = []
    for i, b in enumerate(bodies):
        out.append((await s.submit_turn(sid, f"k{i}", json.dumps(b).encode()))["answer"])
    return out


TURNS = (
    {"message": "Hi"},
    {"action": "choose", "slot": "account_type", "value": "CURRENT"},
    {"action": "choose", "slot": "customer_category", "value": "LLP"},
    {"message": "Do I need a copy of the PAN card?"},
)


async def test_a_turn_is_one_trace_with_the_nested_spans() -> None:
    client = StubClient()
    await converse(service(Tracer(client, "aoa-eval"), RecordingLLM()), *TURNS)
    runs = {r["id"]: r for r in client.created}
    roots = [r for r in runs.values() if r.get("parent_run_id") is None]
    assert [r["name"] for r in roots] == ["assistant.turn"] * len(TURNS)
    assert {r["session_name"] for r in roots} == {"aoa-eval"}
    names = {r["name"] for r in runs.values()}
    assert names >= {
        "history_retrieval", "context_assembly", "intent_resolution", "llm", "voice",
        "checklist", "retrieval", "evidence_validation",
    }  # fmt: skip
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in runs.values():
        by_name.setdefault(r["name"], []).append(r)
    for child, parent in (
        ("history_retrieval", "assistant.turn"),
        ("intent_resolution", "assistant.turn"),
        ("retrieval", "checklist"),
        ("evidence_validation", "checklist"),
        ("llm", "voice"),
    ):
        assert all(runs[r["parent_run_id"]]["name"] == parent for r in by_name[child]), child
    # Every span is closed with its latency; LLM spans carry token usage fields.
    assert len(client.updated) == len(client.created)
    assert all("latency_ms" in (u["extra"] or {}).get("metadata", {}) for u in client.updated)
    llm_updates = [u for u in client.updated if u["name"] == "llm"]
    assert llm_updates and all("usage_metadata" in u["outputs"] for u in llm_updates)


async def test_identifiers_contacts_prompts_and_passages_are_never_uploaded() -> None:
    client = StubClient()
    llm = RecordingLLM()
    await converse(
        service(Tracer(client, "aoa-dev"), llm),
        {"message": f"my PAN is {PAN}"},
        {"message": f"write to {EMAIL} about a current account"},
        {"action": "choose", "slot": "customer_category", "value": "LLP"},
    )
    text = client.uploaded()
    assert PAN not in text and EMAIL not in text and "[REDACTED_EMAIL]" in text
    for request in llm.requests:  # prompts: id, size and hash only
        assert request.user not in text and request.system[:80] not in text
    for chunk in FakeChunks().rows.values():
        assert chunk.content[:60] not in text  # no source passage
    blocked_root = next(r for r in client.created if r["name"] == "assistant.turn")
    assert blocked_root["inputs"] in ({}, None) or PAN not in json.dumps(blocked_root["inputs"])


async def test_tracing_never_changes_or_breaks_a_turn() -> None:
    plain = await converse(service(DISABLED), *TURNS)
    traced = await converse(service(Tracer(StubClient(), "p")), *TURNS)
    failing = await converse(service(Tracer(StubClient(fail=True), "p")), *TURNS)
    assert plain == traced == failing


def test_scrub_withholds_identifiers_and_truncates() -> None:
    assert scrub({"m": f"pan {PAN}"}) == {"m": "[WITHHELD]"}
    assert scrub(f"mail {EMAIL}") == "mail [REDACTED_EMAIL]"
    assert len(scrub("x" * 5000)) < 500


@pytest.mark.parametrize(
    ("enabled", "key", "on"),
    [(False, "k", False), (True, "", False), (True, None, False)],
)
def test_tracing_is_off_unless_enabled_with_a_key(enabled: bool, key: str | None, on: bool) -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None, DATABASE_URL="x", REDIS_URL="y",
        ASSISTANT_TRACING=enabled, LANGSMITH_API_KEY=key,
    )  # fmt: skip
    assert tracer_from_settings(s).enabled is on


def test_development_and_evaluation_use_separate_projects() -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None, DATABASE_URL="x", REDIS_URL="y",
        ASSISTANT_TRACING=True, LANGSMITH_API_KEY="lsv2-test-not-a-real-key",
    )  # fmt: skip
    dev, ev = tracer_from_settings(s), tracer_from_settings(s, evaluation=True)
    assert dev.enabled and ev.enabled
    assert (dev.project, ev.project) == (
        "account-opening-assistant-dev",
        "account-opening-assistant-eval",
    )
