"""Shared harness for the M3 S6 API tests (imported by the test modules; not collected).

The real app (`create_app`) with the real Postgres checkpointer (migration 0007), the
real insert-only report store and the real workflow tables, on a throwaway database.
Retrieval and corpus are the synthetic S4 fakes; the LLM is a schema-aware fake that can
be told to block (to simulate a crash mid-run) or to fail. No live LLM; no network.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.events import NullPublisher
from app.core.loop import selector_loop_factory
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.db.workflow import PostgresReportStore
from app.graph.builder import GraphDeps, build_graph
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.graph.state import ComplianceReport
from app.graph.synthesis import ChecklistDraft, DraftRequirement
from app.llm.common import parse_structured, text_sha256
from app.llm.provider import LLMRequest, LLMResult
from app.main import create_app
from app.workflow.runtime import checkpoint_pool
from app.workflow.service import WorkflowRuntime
from tests.unit.test_graph_s4 import NI_FIELDS, RBI_LEGAL, FakeCorpus, FakeRetriever, cid

LLP_QUESTION = "What KYC documents does an LLP need to open an account?"
PAN = "ABCPE1234F"
LLP_ONLY = IntakeExtraction(
    entity_type=ExtractedValue(value="LLP", evidence_span="an LLP"),
    residency=ExtractedValue(),
    account_type=ExtractedValue(),
)
DRAFT = ChecklistDraft(
    requirements=[
        DraftRequirement(
            name="Partnership deed",
            explanation="As cited.",
            applicability="MANDATORY",
            evidence_ids=[cid(RBI_LEGAL)],
        ),
    ]
)


def enable_app_logging() -> None:
    """Alembic's `fileConfig` (run by the migration fixtures) disables every logger that
    already exists, including the app's. Re-enable them so log assertions really observe
    the app's logging (the API process itself never runs Alembic)."""
    import logging

    for name, logger in logging.root.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and (name == "app" or name.startswith("app.")):
            logger.disabled = False


class SchemaLLM:
    """Answers by output schema. `block` makes synthesis wait forever (a crash mid-run)."""

    name = "fake"
    model_id = "fake-llm-1"

    def __init__(self, *, block_synthesis: bool = False) -> None:
        self.block_synthesis = block_synthesis
        self.requests: list[LLMRequest] = []

    async def generate_structured(self, request: LLMRequest, schema: Any) -> LLMResult[Any]:
        self.requests.append(request)
        if schema is ChecklistDraft:
            if self.block_synthesis:
                await asyncio.Event().wait()
            raw = DRAFT.model_dump_json()
        else:
            raw = LLP_ONLY.model_dump_json()
        parsed, errors = parse_structured(raw, schema)
        return LLMResult[schema](  # type: ignore[valid-type]
            parsed=parsed,
            raw_text_sha256=text_sha256(raw),
            validation_errors=errors,
            provider=self.name,
            model_id=self.model_id,
            latency_ms=0,
        )


class FailOnceAfterSave:
    """A report store whose first save stores the report, then crashes (inside finalize)."""

    def __init__(self, inner: PostgresReportStore) -> None:
        self.inner, self.calls = inner, 0

    def save(self, report: ComplianceReport) -> None:
        self.calls += 1
        self.inner.save(report)
        if self.calls == 1:
            raise RuntimeError("simulated crash after the report insert")

    def get(self, report_id: str) -> ComplianceReport | None:
        return self.inner.get(report_id)


def runtime_factory(
    llm: Any,
    *,
    lease_s: float = 60.0,
    store_wrapper: Callable[[Any], Any] | None = None,
    publisher: Any = None,
    subscriber: Any = None,
    poll_s: float = 0.1,
    keepalive_s: float = 15.0,
    idle_s: float = 60.0,
    on_runtime: Callable[[WorkflowRuntime], None] | None = None,
) -> Any:
    """S7: `publisher` / `subscriber` default to none (PostgreSQL polling only)."""

    async def factory(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> WorkflowRuntime:
        pool = checkpoint_pool(settings)
        await pool.open(wait=True)
        reports: Any = PostgresReportStore(create_sync_sessionmaker(create_sync_engine(settings)))
        if store_wrapper is not None:
            reports = store_wrapper(reports)
        deps = GraphDeps(
            llm=llm,
            retriever=FakeRetriever({"LLP": [NI_FIELDS], "LEGAL_ENTITY": [RBI_LEGAL]}),
            corpus=FakeCorpus(),
            reports=reports,
        )
        runtime = WorkflowRuntime(
            graph=build_graph(deps, checkpointer=AsyncPostgresSaver(pool)),  # type: ignore[arg-type]
            deps=deps,
            sessions=sessions,
            reports=reports,
            lease_s=lease_s,
            heartbeat_s=min(10.0, lease_s / 2),
            session_ttl=timedelta(hours=1),
            query_max_chars=2000,
            on_close=[pool.close],
            publisher=publisher if publisher is not None else NullPublisher(),
            subscriber=subscriber,
            events_poll_s=poll_s,
            sse_keepalive_s=keepalive_s,
            sse_idle_timeout_s=idle_s,
        )
        if on_runtime is not None:
            on_runtime(runtime)
        return runtime

    return factory


@contextmanager
def api(llm: Any, **kw: Any) -> Iterator[TestClient]:
    app = create_app(get_settings(), runtime_factory=runtime_factory(llm, **kw))
    with TestClient(app, backend_options={"loop_factory": selector_loop_factory}) as client:
        yield client


def new_session(client: TestClient) -> str:
    r = client.post("/api/v1/sessions", json={"data_class": "synthetic_demo"})
    assert r.status_code == 201, r.text
    return str(r.json()["session_id"])


def wait_for(client: TestClient, sid: str, status: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = client.get(f"/api/v1/sessions/{sid}").json()
        wf = view.get("workflow") or {}
        if wf.get("status") == status:
            return view
        time.sleep(0.05)
    raise AssertionError(f"workflow did not reach {status!r}: {view}")


def wait_for_node(client: TestClient, sid: str, node: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wf = client.get(f"/api/v1/sessions/{sid}").json().get("workflow") or {}
        if any(e["node"] == node for e in wf.get("node_events", [])):
            return
        time.sleep(0.05)
    raise AssertionError(f"node {node!r} never completed")


def all_db_bytes(engine: sa.Engine) -> bytes:
    """Every byte in the checkpoint tables and the workflow tables (for leak scans)."""
    out: list[bytes] = []
    with engine.connect() as conn:
        for row in conn.execute(
            sa.text("SELECT checkpoint::text, metadata::text " "FROM langgraph.checkpoints")
        ):
            out += [str(v).encode() for v in row]
        for table in ("checkpoint_blobs", "checkpoint_writes"):
            for (blob,) in conn.execute(
                sa.text(
                    f"SELECT blob FROM langgraph.{table} "  # noqa: S608
                    "WHERE blob IS NOT NULL"
                )
            ):
                out.append(bytes(blob))
        for table in ("api_idempotency", "graph_runs", "reports", "kyc_sessions"):
            for row in conn.execute(sa.text(f"SELECT * FROM {table}")):  # noqa: S608
                out.append(repr(tuple(row)).encode())
    return b"".join(out)
