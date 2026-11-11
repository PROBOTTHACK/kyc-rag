"""Workflow service: the API's only way into the graph (M3 S6; M3_PLAN §6, §9).

Security order for every POST (nothing is stored until the request passed it all):
1. the session exists, is active and not expired;
2. EVERY string and number in the raw body (keys included) is screened; an identifier
   -> 422 SENSITIVE_DATA_DETECTED with category codes only;
3. the body shape is validated (codes only, never echoed);
4. for /resume: the run is awaiting clarification, the request's `checkpoint_id` equals
   the current one (checked here, outside the graph), and the answer goes through
   `resume_command(..., asked=<the pending question fields>)`;
5. idempotency and the run claim happen in ONE transaction (compare-and-set);
6. only then does the background task submit input to the graph.

`_execute` is the single place that submits anything to the graph, and it only ever
submits a `start_input` state, a `resume_command` result or `None` (retry from the last
checkpoint). No public path passes a raw value to LangGraph.

Thread model (decision S6-D1): one LangGraph thread per question (`thread_id` = the run
id of its first run), so nothing from an earlier question in the session can carry over.
Crash recovery is explicit (`/retry`), never automatic. Bodies are never logged.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.events import NullPublisher, ProgressEvent, Publisher
from app.db import workflow as db
from app.graph.builder import GraphDeps, run_config
from app.graph.inputs import (
    InputRejected,
    InvalidAnswer,
    SensitiveDataDetected,
    resume_command,
    start_input,
)
from app.graph.intake import flatten_strings
from app.graph.report import report_id as derive_report_id
from app.graph.state import ComplianceReport, DataClass
from app.security.screen import screen_text
from app.workflow.stream import (
    RunView,
    completed_nodes,
    missing_completions,
    session_event_stream,
)

log = logging.getLogger(__name__)
SUBMITTABLE = (dict, Command, type(None))
# LangGraph checkpoint ids are server-issued UUID strings. They are opaque tokens, not user
# text: about 2 % of random UUIDs contain a digit run that passes the card-number Luhn
# check, so screening them as text wrongly rejected valid resumes (fixed in M3 S7).
CHECKPOINT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class WorkflowError(Exception):
    """An API error: a stable code, an HTTP status, a safe message (never user input)."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1)


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkpoint_id: str = Field(min_length=1, max_length=200)
    answers: dict[str, Any]


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class WorkflowRuntime:
    """Everything the service needs; built once per process (app lifespan)."""

    graph: Any  # CompiledStateGraph with the Postgres checkpointer
    deps: GraphDeps
    sessions: async_sessionmaker[AsyncSession]
    reports: Any  # ReportStore with `get`
    lease_s: float
    heartbeat_s: float
    session_ttl: timedelta
    query_max_chars: int
    owner_id: str = field(default_factory=lambda: f"api-{uuid.uuid4()}")
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    on_close: list[Any] = field(default_factory=list)  # async closers (checkpointer pool)
    # M3 S7: progress events. Redis is only a wake-up; PostgreSQL is the source.
    publisher: Publisher = field(default_factory=NullPublisher)
    subscriber: Any = None  # () -> redis.asyncio.Redis for pub/sub, or None (poll only)
    events_poll_s: float = 1.0
    sse_keepalive_s: float = 15.0
    sse_idle_timeout_s: float = 900.0


def _sha(endpoint: str, body: Mapping[str, Any]) -> str:
    canonical = json.dumps({"endpoint": endpoint, "body": body}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _screen_raw(body: Any, max_chars: int) -> None:
    """Step 2: every string and number in the raw body, keys included."""
    for text_value in flatten_strings(body):
        result = screen_text(text_value, max_chars=max_chars)
        if result.sensitive:
            raise WorkflowError(
                422,
                "SENSITIVE_DATA_DETECTED",
                "The request contains sensitive personal data and was not "
                "processed. Use synthetic data only.",
                categories=list(result.categories),
            )


def _without_checkpoint_id(body: Any) -> Any:
    """The resume body minus a well-formed `checkpoint_id`, for PII screening. A
    `checkpoint_id` that is not exactly a lowercase UUID is rejected here (codes only,
    nothing stored), so the only value exempt from screening is pure hex."""
    if not isinstance(body, dict) or "checkpoint_id" not in body:
        return body
    value = body["checkpoint_id"]
    if not isinstance(value, str) or not CHECKPOINT_ID.fullmatch(value):
        raise WorkflowError(422, "VALIDATION_ERROR", "The request body is not valid.")
    return {k: v for k, v in body.items() if k != "checkpoint_id"}


def _validate(model: type[BaseModel], body: Any) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError:
        raise WorkflowError(422, "VALIDATION_ERROR", "The request body is not valid.") from None


class WorkflowService:
    def __init__(self, runtime: WorkflowRuntime) -> None:
        self.rt = runtime

    # --- sessions ----------------------------------------------------------------------------

    async def create_session(self, body: Any) -> dict[str, Any]:
        body = body if body is not None else {}
        _screen_raw(body, self.rt.query_max_chars)
        if not isinstance(body, dict) or set(body) - {"data_class"}:
            raise WorkflowError(422, "VALIDATION_ERROR", "The request body is not valid.")
        try:
            data_class = DataClass(body.get("data_class", DataClass.UNSPECIFIED))
        except ValueError:
            raise WorkflowError(422, "VALIDATION_ERROR", "Unknown data_class.") from None
        async with self.rt.sessions() as s, s.begin():
            row = await s.run_sync(
                lambda sync: db.create_session(sync, ttl=self.rt.session_ttl, data_class=data_class)
            )
        return self._session_view(row)

    @staticmethod
    def _session_view(row: Any) -> dict[str, Any]:
        return {
            "session_id": str(row.id),
            "status": row.status,
            "data_class": row.data_class,
            "expires_at": row.expires_at.isoformat(),
        }

    async def _active_session(self, s: AsyncSession, session_id: str) -> Any:
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise WorkflowError(404, "SESSION_NOT_FOUND", "Session not found.") from None
        row = await db.get_session_row(s, sid)
        if row is None:
            raise WorkflowError(404, "SESSION_NOT_FOUND", "Session not found.")
        if row.status != "active" or row.expires_at <= datetime.now(UTC):
            raise WorkflowError(410, "SESSION_EXPIRED", "The session has expired.")
        return row

    async def get_session(self, session_id: str) -> dict[str, Any]:
        async with self.rt.sessions() as s:
            try:
                sid = uuid.UUID(session_id)
            except ValueError:
                raise WorkflowError(404, "SESSION_NOT_FOUND", "Session not found.") from None
            row = await db.get_session_row(s, sid)
            if row is None:
                raise WorkflowError(404, "SESSION_NOT_FOUND", "Session not found.")
            run = await db.latest_run(s, sid)
        view = self._session_view(row)
        if row.expires_at <= datetime.now(UTC):
            view["status"] = "expired"
        view["workflow"] = None
        if run is not None:
            wf: dict[str, Any] = {
                "run_id": str(run.id),
                "thread_id": run.thread_id,
                "status": run.status,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "node_events": run.node_events,
            }
            if run.status == "awaiting_clarification":
                wf["clarification"] = await self._pending(run.thread_id)
            if run.status == "completed":
                rid = derive_report_id(str(row.id), run.thread_id)
                report = await asyncio.to_thread(self.rt.reports.get, rid)
                wf["report_id"] = rid if report is not None else None
                wf["outcome"] = report.outcome.value if report is not None else None
            view["workflow"] = wf
        return view

    async def _pending(self, thread_id: str) -> dict[str, Any] | None:
        """The pending clarification: questions and the current checkpoint id."""
        snapshot = await self.rt.graph.aget_state(run_config(self.rt.deps, thread_id=thread_id))
        interrupts = [i for t in snapshot.tasks for i in t.interrupts]
        if not interrupts or "clarify" not in snapshot.next:
            return None
        payload = interrupts[0].value
        return {
            "checkpoint_id": snapshot.config["configurable"]["checkpoint_id"],
            "round": payload.get("round"),
            "max_rounds": payload.get("max_rounds"),
            "questions": payload.get("questions", []),
        }

    async def get_report(self, report_id: str) -> dict[str, Any]:
        try:
            uuid.UUID(report_id)
        except ValueError:
            raise WorkflowError(404, "REPORT_NOT_FOUND", "Report not found.") from None
        report: ComplianceReport | None = await asyncio.to_thread(self.rt.reports.get, report_id)
        if report is None:
            raise WorkflowError(404, "REPORT_NOT_FOUND", "Report not found.")
        return report.model_dump(mode="json")

    # --- the three state-changing endpoints ----------------------------------------------------

    @staticmethod
    def _require_key(key: str | None) -> str:
        if not key or len(key) > 200:
            raise WorkflowError(
                400,
                "IDEMPOTENCY_KEY_REQUIRED",
                "An Idempotency-Key header (at most 200 characters) is required.",
            )
        return key

    async def _replay(
        self, session_id: uuid.UUID, key: str, endpoint: str, sha: str
    ) -> dict[str, Any] | None:
        """Read-only early check: the stored response of an identical earlier request, a
        409 for a different request under the same key, or None (not seen yet)."""
        async with self.rt.sessions() as s:
            existing = await db.get_idempotency(s, session_id, key)
        if existing is None:
            return None
        if existing.endpoint != endpoint or existing.request_sha256 != sha:
            raise WorkflowError(
                409,
                "IDEMPOTENCY_MISMATCH",
                "This Idempotency-Key was already used for a different request.",
            )
        return dict(existing.response_json)

    async def _reserve(
        self,
        s: AsyncSession,
        session_id: uuid.UUID,
        key: str,
        endpoint: str,
        sha: str,
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        """None when newly reserved; the stored response for a repeated identical request."""
        existing = await db.reserve_idempotency(
            s,
            session_id=session_id,
            key=key,
            endpoint=endpoint,
            request_sha256=sha,
            response=response,
        )
        if existing is None:
            return None
        if existing.endpoint != endpoint or existing.request_sha256 != sha:
            raise WorkflowError(
                409,
                "IDEMPOTENCY_MISMATCH",
                "This Idempotency-Key was already used for a different request.",
            )
        return dict(existing.response_json)

    async def submit_message(self, session_id: str, key: str | None, body: Any) -> dict[str, Any]:
        key = self._require_key(key)
        async with self.rt.sessions() as s:
            session = await self._active_session(s, session_id)
        _screen_raw(body, self.rt.query_max_chars)
        message = _validate(MessageBody, body).message
        run_id = uuid.uuid4()
        try:
            state = start_input(
                message,
                session_id=str(session.id),
                run_id=str(run_id),
                data_class=session.data_class,
                max_chars=self.rt.query_max_chars,
            )
        except SensitiveDataDetected as exc:
            raise WorkflowError(
                422,
                "SENSITIVE_DATA_DETECTED",
                "The request contains sensitive personal data and was not "
                "processed. Use synthetic data only.",
                categories=list(exc.categories),
            ) from None
        except InputRejected as exc:
            raise WorkflowError(
                422,
                "INPUT_REJECTED",
                "The message cannot be processed.",
                categories=list(exc.categories),
            ) from None
        sha = _sha("messages", {"message": state["query"]})
        replay = await self._replay(session.id, key, "messages", sha)
        if replay is not None:
            return replay
        response = {"session_id": str(session.id), "run_id": str(run_id), "status": "running"}
        async with self.rt.sessions() as s:
            try:
                async with s.begin():
                    stored = await self._reserve(s, session.id, key, "messages", sha, response)
                    if stored is not None:
                        return stored
                    await db.insert_running(
                        s,
                        run_id=run_id,
                        session_id=session.id,
                        thread_id=str(run_id),
                        owner_id=self.rt.owner_id,
                        lease_s=self.rt.lease_s,
                    )
            except IntegrityError:
                raise WorkflowError(
                    409,
                    "RUN_IN_PROGRESS",
                    "A workflow is already running or awaiting clarification " "in this session.",
                ) from None
        self._start(session.id, run_id, str(run_id), state)
        return response

    async def resume(self, session_id: str, key: str | None, body: Any) -> dict[str, Any]:
        key = self._require_key(key)
        async with self.rt.sessions() as s:
            session = await self._active_session(s, session_id)
            run = await db.latest_run(s, session.id)
        _screen_raw(_without_checkpoint_id(body), self.rt.query_max_chars)  # before any read
        parsed: ResumeBody = _validate(ResumeBody, body)
        sha = _sha("resume", {"checkpoint_id": parsed.checkpoint_id, "answers": parsed.answers})
        replay = await self._replay(session.id, key, "resume", sha)
        if replay is not None:
            return replay
        if run is None or run.status != "awaiting_clarification":
            raise WorkflowError(
                409,
                "NOT_AWAITING_CLARIFICATION",
                "No workflow in this session is waiting for an answer.",
            )
        pending = await self._pending(run.thread_id)
        if pending is None:
            raise WorkflowError(
                409,
                "NOT_AWAITING_CLARIFICATION",
                "No workflow in this session is waiting for an answer.",
            )
        if parsed.checkpoint_id != pending["checkpoint_id"]:
            raise WorkflowError(
                409, "STALE_CHECKPOINT", "The question you answered is no longer the current one."
            )
        asked = [q["field"] for q in pending["questions"]]
        try:
            command = resume_command(
                {"answers": parsed.answers}, asked=asked, max_chars=self.rt.query_max_chars
            )
        except SensitiveDataDetected as exc:
            raise WorkflowError(
                422,
                "SENSITIVE_DATA_DETECTED",
                "The request contains sensitive personal data and was not "
                "processed. Use synthetic data only.",
                categories=list(exc.categories),
            ) from None
        except InvalidAnswer:
            raise WorkflowError(
                422,
                "INVALID_ANSWER",
                "Answer only the questions asked, with one of the listed options.",
            ) from None
        response = {"session_id": str(session.id), "run_id": str(run.id), "status": "running"}
        async with self.rt.sessions() as s, s.begin():
            stored = await self._reserve(s, session.id, key, "resume", sha, response)
            if stored is not None:
                return stored
            if not await db.claim_awaiting(s, run.id, self.rt.owner_id, self.rt.lease_s):
                raise WorkflowError(409, "RUN_IN_PROGRESS", "This workflow was already resumed.")
        self._start(session.id, run.id, run.thread_id, command)
        return response

    async def retry(self, session_id: str, key: str | None, body: Any) -> dict[str, Any]:
        key = self._require_key(key)
        async with self.rt.sessions() as s:
            session = await self._active_session(s, session_id)
            run = await db.latest_run(s, session.id)
        _screen_raw(body if body is not None else {}, self.rt.query_max_chars)
        _validate(RetryBody, body if body is not None else {})
        sha = _sha("retry", {"session_id": str(session.id)})
        replay = await self._replay(session.id, key, "retry", sha)
        if replay is not None:
            return replay
        if run is None or run.status in ("completed", "abandoned"):
            raise WorkflowError(
                409, "NOTHING_TO_RETRY", "There is no interrupted workflow to retry."
            )
        if run.status == "awaiting_clarification":
            raise WorkflowError(
                409, "RUN_IN_PROGRESS", "The workflow is waiting for an answer; use /resume."
            )
        new_id = uuid.uuid4()
        response = {"session_id": str(session.id), "run_id": str(new_id), "status": "running"}
        async with self.rt.sessions() as s:
            try:
                async with s.begin():
                    stored = await self._reserve(s, session.id, key, "retry", sha, response)
                    if stored is not None:
                        return stored
                    if not await db.abandon(s, run.id, require_expired=run.status == "running"):
                        raise WorkflowError(
                            409,
                            "RUN_IN_PROGRESS",
                            "The workflow is still running; retry after its " "lease expires.",
                            retry_after=int(self.rt.lease_s),
                        )
                    await db.insert_running(
                        s,
                        run_id=new_id,
                        session_id=session.id,
                        thread_id=run.thread_id,
                        owner_id=self.rt.owner_id,
                        lease_s=self.rt.lease_s,
                    )
            except IntegrityError:
                raise WorkflowError(409, "RUN_IN_PROGRESS", "Another retry won.") from None
        self._start(session.id, new_id, run.thread_id, None)
        return response

    # --- background execution ------------------------------------------------------------------

    def _start(
        self, session_id: uuid.UUID, run_id: uuid.UUID, thread_id: str, graph_input: Any
    ) -> None:
        task = asyncio.create_task(self._execute(session_id, run_id, thread_id, graph_input))
        self.rt.tasks.add(task)
        task.add_done_callback(self.rt.tasks.discard)

    async def _heartbeat(self, run_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.rt.heartbeat_s)
            async with self.rt.sessions() as s, s.begin():
                if not await db.renew_lease(s, run_id, self.rt.owner_id, self.rt.lease_s):
                    return

    async def _publish_node(self, session_id: uuid.UUID, run_id: uuid.UUID, event: Any) -> None:
        """After the node event is COMMITTED in PostgreSQL (M3_PLAN §9 order)."""
        if event is None:
            return
        await self.rt.publisher.publish(
            str(session_id),
            ProgressEvent(kind="node", run_id=str(run_id), seq=int(event["seq"]),
                          node=str(event["node"]), status=str(event["status"]),
                          at=str(event["at"])),
        )  # fmt: skip

    async def _publish_run(self, session_id: uuid.UUID, run_id: uuid.UUID, status: str) -> None:
        """A current-status notification (S7-D3), after the status is committed."""
        await self.rt.publisher.publish(
            str(session_id),
            ProgressEvent(kind="run", run_id=str(run_id), status=status,
                          at=datetime.now(UTC).isoformat()),
        )  # fmt: skip

    async def _recover_unrecorded(
        self, session_id: uuid.UUID, run_id: uuid.UUID, thread_id: str, config: Any
    ) -> None:
        """S7-D5: node completions that the checkpoint history has but no earlier run of
        this thread recorded (a crash between the checkpoint and the `node_events` write)
        are appended to this retry run as `recovered`, in execution order, before it
        resumes. Already-recorded completions are never duplicated."""
        history = [h async for h in self.rt.graph.aget_state_history(config)][::-1]
        checkpointed = completed_nodes(
            [(tuple(h.next), (h.metadata or {}).get("source")) for h in history]
        )
        async with self.rt.sessions() as s:
            recorded = await db.recorded_completions(s, thread_id, exclude_run=run_id)
        for node in missing_completions(checkpointed, recorded):
            async with self.rt.sessions() as s, s.begin():
                event = await db.append_node_event(s, run_id, self.rt.owner_id, node, "recovered")
            await self._publish_node(session_id, run_id, event)

    async def _execute(
        self, session_id: uuid.UUID, run_id: uuid.UUID, thread_id: str, graph_input: Any
    ) -> None:
        """The ONLY place the application submits input to the graph."""
        if not isinstance(graph_input, SUBMITTABLE):  # defensive: never a raw value
            raise TypeError("only a start_input state, a resume_command or None is submitted")
        config = run_config(self.rt.deps, thread_id=thread_id)
        heartbeat = asyncio.create_task(self._heartbeat(run_id))
        started = time.monotonic()
        try:
            await self._publish_run(session_id, run_id, "running")
            if graph_input is None:
                await self._recover_unrecorded(session_id, run_id, thread_id, config)
            async for update in self.rt.graph.astream(graph_input, config, stream_mode="updates"):
                for node in update:
                    if node != "__interrupt__":
                        async with self.rt.sessions() as s, s.begin():
                            event = await db.append_node_event(s, run_id, self.rt.owner_id, node)
                        await self._publish_node(session_id, run_id, event)
            snapshot = await self.rt.graph.aget_state(config)
            status = "awaiting_clarification" if snapshot.next else "completed"
            calls = list(snapshot.values.get("llm_calls") or []) if status == "completed" else []
            async with self.rt.sessions() as s, s.begin():
                await db.finish_run(
                    s,
                    run_id,
                    self.rt.owner_id,
                    status=status,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    llm_calls=calls,
                )
            await self._publish_run(session_id, run_id, status)
        except asyncio.CancelledError:
            # Shutdown or crash: the row stays `running` until its lease expires (S6-D2).
            raise
        except Exception as exc:  # noqa: BLE001 - recorded as `failed`, class name only
            log.warning("workflow run %s failed: %s", run_id, type(exc).__name__)
            async with self.rt.sessions() as s, s.begin():
                event = await db.append_node_event(
                    s, run_id, self.rt.owner_id, f"error:{type(exc).__name__}", "failed"
                )
                await db.finish_run(s, run_id, self.rt.owner_id, status="failed")
            await self._publish_node(session_id, run_id, event)
            await self._publish_run(session_id, run_id, "failed")
        finally:
            heartbeat.cancel()

    # --- progress stream (M3 S7) ----------------------------------------------------------------

    async def open_event_stream(
        self, session_id: str, last_event_id: str | None, is_disconnected: Any
    ) -> Any:
        """The SSE body for a session. The session must exist (expired sessions may still
        read their events). Access is possession of the session id (S6-D4)."""
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise WorkflowError(404, "SESSION_NOT_FOUND", "Session not found.") from None
        async with self.rt.sessions() as s:
            if await db.get_session_row(s, sid) is None:
                raise WorkflowError(404, "SESSION_NOT_FOUND", "Session not found.")

        async def load() -> list[RunView]:
            async with self.rt.sessions() as s:  # short-lived: never held while waiting
                runs = await db.session_runs(s, sid)
            return [
                RunView(
                    run_id=str(r.id),
                    status=r.status,
                    node_events=list(r.node_events),
                    status_at=(r.finished_at or r.heartbeat_at or r.started_at).isoformat(),
                )
                for r in runs
            ]

        return session_event_stream(
            session_id=str(sid),
            load=load,
            last_event_id=last_event_id,
            is_disconnected=is_disconnected,
            subscriber=self.rt.subscriber,
            poll_s=self.rt.events_poll_s,
            keepalive_s=self.rt.sse_keepalive_s,
            idle_timeout_s=self.rt.sse_idle_timeout_s,
        )

    async def log_stale_leases(self) -> int:
        """Startup: report runs whose lease expired; never re-execute them (M3_PLAN §9)."""
        async with self.rt.sessions() as s:
            count = await db.count_stale_leases(s)
        if count:
            log.warning(
                "%d workflow run(s) have expired leases; recover them with POST /retry", count
            )
        return count

    async def shutdown(self) -> None:
        for task in list(self.rt.tasks):
            task.cancel()
        if self.rt.tasks:
            await asyncio.gather(*self.rt.tasks, return_exceptions=True)
        for close in self.rt.on_close:
            await close()
