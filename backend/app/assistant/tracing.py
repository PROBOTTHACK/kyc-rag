"""LangSmith observability for the assistant (A7; owner request, 2026-11-15).

One trace per customer turn (`assistant.turn`) with nested spans:
`history_retrieval`, `context_assembly`, `intent_resolution`, `llm` (every model call, with
latency and token usage), `voice`, `retrieval` and `evidence_validation`.

Rules:
- **Off unless configured.** `ASSISTANT_TRACING=true` and a `LANGSMITH_API_KEY` are both
  required; otherwise every span is a no-op. LangChain's global `LANGSMITH_TRACING` is not
  used (it would also trace the legacy LangGraph code).
- **Never in the way.** Tracing errors are swallowed (logged by class name at debug level);
  a turn never fails or changes because LangSmith is slow, down or misconfigured. The
  LangSmith client batches uploads in a background thread.
- **Nothing sensitive.** Inputs and outputs are small dicts of codes, counts and ids. The
  only customer text is the message *as screening stored it* (identifiers refused, e-mail and
  phone redacted); a refused message is never traced. Every string is screened again here
  and truncated. Prompts and source passages are never uploaded: model calls carry the
  prompt id, its size and a hash; checklists carry item ids, document keys and pages.
- **PostgreSQL stays the authoritative history.** Traces are diagnostics only; nothing is
  read back from LangSmith.
- **Separate projects** for development (`ASSISTANT_TRACE_PROJECT`) and evaluation
  (`ASSISTANT_TRACE_EVAL_PROJECT`, used by the A7 runner).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.llm.provider import LLMProvider, LLMRequest, LLMResult
from app.security.screen import screen_text

log = logging.getLogger(__name__)

MAX_STRING = 400  # characters kept of any traced string
MAX_ITEMS = 60  # list entries kept


def scrub(value: Any, depth: int = 0) -> Any:
    """What may be uploaded: primitives, small lists and dicts, screened strings."""
    if depth > 4:
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        res = screen_text(value)
        if res.sensitive or res.text is None:
            return "[WITHHELD]"
        text = res.text
        return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "…"
    if isinstance(value, dict):
        return {str(k): scrub(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, list | tuple | set):
        return [scrub(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    return scrub(str(value), depth + 1)


@dataclass
class Span:
    run: Any = None  # a langsmith RunTree, or None for a no-op span
    outputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def set(self, **outputs: Any) -> None:
        self.outputs.update(outputs)

    def meta(self, **metadata: Any) -> None:
        self.metadata.update(metadata)


_NOOP = Span()
_tracer: ContextVar[Tracer | None] = ContextVar("assistant_tracer", default=None)
_parent: ContextVar[Span | None] = ContextVar("assistant_span", default=None)


class Tracer:
    """Creates LangSmith runs for one project. `client` is a `langsmith.Client` (or any
    object with `create_run` / `update_run`, as in the tests)."""

    def __init__(self, client: Any, project: str) -> None:
        self.client = client
        self.project = project

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _tracer.set(self if self.enabled else None)
        try:
            yield
        finally:
            _tracer.reset(token)


DISABLED = Tracer(None, "")


@contextmanager
def span(
    name: str,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """A nested span under the current one (or a root span). A no-op when tracing is off."""
    tracer = _tracer.get()
    if tracer is None:
        yield _NOOP
        return
    parent = _parent.get()
    run = None
    try:
        from langsmith.run_trees import RunTree

        clean = scrub(inputs or {})
        if parent is not None and parent.run is not None:
            run = parent.run.create_child(name=name, run_type=run_type, inputs=clean)
        else:
            run = RunTree(
                name=name,
                run_type=run_type,
                inputs=clean,
                project_name=tracer.project,
                client=tracer.client,
            )
        if metadata:
            run.add_metadata(scrub(metadata))
        run.post()
    except Exception as exc:  # noqa: BLE001 - tracing never breaks a turn
        log.debug("tracing: span %s not started: %s", name, type(exc).__name__)
        run = None
    current = Span(run)
    token = _parent.set(current)
    started = time.perf_counter()
    error: str | None = None
    try:
        yield current
    except BaseException as exc:
        error = type(exc).__name__  # the class only: messages may carry data
        raise
    finally:
        _parent.reset(token)
        if run is not None:
            try:
                current.metadata["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
                run.end(
                    outputs=scrub(current.outputs),
                    error=error,
                    metadata=scrub(current.metadata),
                )
                run.patch()
            except Exception as exc:  # noqa: BLE001 - tracing never breaks a turn
                log.debug("tracing: span %s not ended: %s", name, type(exc).__name__)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class TracingLLM:
    """Wraps an `LLMProvider`: one `llm` span per model call, with the prompt id, its size
    and a hash (never the prompt), the model, latency, token usage and validity."""

    inner: LLMProvider

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    async def generate_structured(self, request: LLMRequest, schema: type[Any]) -> LLMResult[Any]:
        inputs = {
            "prompt_id": request.prompt_id,
            "prompt_version": request.prompt_version,
            "prompt_chars": len(request.system) + len(request.user),
            "prompt_sha256": sha(request.system + request.user),
            "schema": schema.__name__,
        }
        with span("llm", "llm", inputs, metadata={"ls_provider": self.inner.name}) as s:
            result = await self.inner.generate_structured(request, schema)
            usage = result.usage
            s.set(
                valid=result.parsed is not None,
                validation_errors=len(result.validation_errors),
                finish_reason=result.finish_reason,
                usage_metadata={
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "total_tokens": (
                        (usage.input_tokens or 0) + (usage.output_tokens or 0) if usage else None
                    ),
                },
            )
            s.meta(ls_model_name=result.model_id, provider_latency_ms=result.latency_ms)
            return result


def traced(llm: LLMProvider | None, tracer: Tracer) -> LLMProvider | None:
    return TracingLLM(llm) if llm is not None and tracer.enabled else llm


def tracer_from_settings(settings: Any, *, evaluation: bool = False) -> Tracer:
    """The configured tracer, or the disabled one. Never raises."""
    key = settings.LANGSMITH_API_KEY.get_secret_value() if settings.LANGSMITH_API_KEY else ""
    if not settings.ASSISTANT_TRACING:
        return DISABLED
    if not key:
        log.warning("assistant tracing is enabled but LANGSMITH_API_KEY is not set: disabled")
        return DISABLED
    project = (
        settings.ASSISTANT_TRACE_EVAL_PROJECT if evaluation else settings.ASSISTANT_TRACE_PROJECT
    )
    try:
        from langsmith import Client

        return Tracer(Client(api_key=key, timeout_ms=5000), project)
    except Exception as exc:  # noqa: BLE001 - observability is optional
        log.warning("assistant tracing unavailable: %s", type(exc).__name__)
        return DISABLED
