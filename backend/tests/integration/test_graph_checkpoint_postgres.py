"""M3 S3: clarification interrupt/resume through the real Postgres checkpointer.

`AsyncPostgresSaver` on a throwaway database (its tables are created here with the
library's `setup()`; the production schema arrives with S6 migration 0007). The run is
paused, every connection is closed, and a NEW saver and NEW graph object resume it, as a
restarted API process would. The fake LLM keeps it offline; no live Groq call.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from app.core.loop import selector_loop_factory
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.inputs import SensitiveDataDetected, resume_command, start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.llm.fake import scripted_llm

pytestmark = pytest.mark.integration
R = TypeVar("R")
QUERY = "What KYC documents does an LLP need to open an account? Mail test.user@example.com"
LLP_ONLY = IntakeExtraction(
    entity_type=ExtractedValue(value="LLP", evidence_span="an LLP"),
    residency=ExtractedValue(),
    account_type=ExtractedValue(),
)
SECRETS = (b"test.user@example.com", b"ABCPE1234F")


def run_async(fn: Callable[[], Awaitable[R]]) -> R:
    with asyncio.Runner(loop_factory=selector_loop_factory) as runner:  # psycopg on Windows
        return runner.run(fn())


@pytest.fixture
def dsn(temp_db: str) -> Iterator[str]:
    url = make_url(temp_db).set(drivername="postgresql").render_as_string(hide_password=False)

    async def setup() -> None:
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()

    run_async(setup)
    yield url


def checkpoint_bytes(dsn: str) -> bytes:
    """Every byte the checkpointer stored, for leak scans."""
    with psycopg.connect(dsn) as conn:
        parts: list[bytes] = []
        for row in conn.execute("SELECT checkpoint::text, metadata::text FROM checkpoints"):
            parts += [str(v).encode() for v in row]
        for (blob,) in conn.execute("SELECT blob FROM checkpoint_blobs WHERE blob IS NOT NULL"):
            parts.append(bytes(blob))
        for (blob,) in conn.execute("SELECT blob FROM checkpoint_writes"):
            parts.append(bytes(blob))
        return b"".join(parts)


def test_interrupt_survives_a_restart_and_resumes_from_postgres(dsn: str) -> None:
    deps = GraphDeps(llm=scripted_llm(LLP_ONLY))
    config = run_config(deps, thread_id="session-pg-1")

    async def first_process() -> dict[str, Any]:
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            graph = build_graph(deps, checkpointer=saver)
            out = await graph.ainvoke(start_input(QUERY, session_id="s", run_id="r"), config)
            return dict(out)

    paused = run_async(first_process)
    assert [q["field"] for q in paused["__interrupt__"][0].value["questions"]] == ["account_type"]

    async def second_process() -> tuple[Any, dict[str, Any], Any]:
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            graph = build_graph(GraphDeps(llm=scripted_llm()), checkpointer=saver)  # no LLM left
            before = await graph.aget_state(config)
            final = await graph.ainvoke(
                resume_command({"answers": {"account_type": "CURRENT_ACCOUNT"}}), config
            )
            after = await graph.aget_state(config)
            return before, dict(final), after

    before, final, after = run_async(second_process)
    assert before.next == ("clarify",)
    assert before.values["missing_fields"] == ["account_type"]
    assert before.values["query"].endswith("[REDACTED_EMAIL]")
    assert final["intake_complete"] is True and not after.next
    assert final["clarification_answers"] == [
        {"field": "account_type", "value": "CURRENT_ACCOUNT", "round": 1}
    ]
    assert final["entity_profile"]["entity_type"]["source"] == "user_stated"
    assert final["entity_profile"]["account_type"]["source"] == "user_answered"
    assert final["outcome"] == "INSUFFICIENT_EVIDENCE"  # retrieval is a stub until S4
    assert after.values == final

    stored = checkpoint_bytes(dsn)
    assert b"CURRENT_ACCOUNT" in stored  # the answer really is in Postgres
    assert all(secret not in stored for secret in SECRETS)


def test_rejected_input_and_answers_never_reach_the_checkpoint_tables(dsn: str) -> None:
    deps = GraphDeps(llm=scripted_llm(LLP_ONLY))
    config = run_config(deps, thread_id="session-pg-2")

    async def scenario() -> None:
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            graph = build_graph(deps, checkpointer=saver)
            with pytest.raises(SensitiveDataDetected):
                start_input("LLP with PAN ABCPE1234F", session_id="s", run_id="r")
            await graph.ainvoke(start_input(QUERY, session_id="s", run_id="r"), config)
            with pytest.raises(SensitiveDataDetected):
                resume_command({"answers": {"account_type": "ABCPE1234F"}})
            snapshot = await graph.aget_state(config)
            assert snapshot.next == ("clarify",)  # still waiting for a valid answer

    run_async(scenario)
    stored = checkpoint_bytes(dsn)
    assert stored and all(secret not in stored for secret in SECRETS)
