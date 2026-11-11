"""Groq implementation of `LLMProvider` (M3 S1; docs/02 "Groq API verification").

Boundaries:
- The `groq` SDK (pinned) is imported only here, and lazily, so the app and every test
  run without Groq configuration and without network.
- Live calls are OFF by default. `from_settings` refuses unless `GROQ_LIVE_ENABLED`,
  `GROQ_API_KEY`, `PRIMARY_LLM_MODEL` and `GROQ_DATA_TERMS_VERIFIED_ON` are all set.
- The key is a `SecretStr`, revealed only to build the SDK client. It never appears in a
  log, exception, repr or result; error text is built from the exception class and status
  code only (never the provider's error body, which can echo the request).
- Each attempt has a timeout (default 30 s). Transient failures (429, 408, 5xx, timeouts,
  transport) are retried at most `max_retries` (default and hard max 2, CLAUDE.md) times
  with jittered exponential backoff; the SDK's own retries are disabled. A final transient
  error is marked `transient=True` so `FallbackLLMProvider` may try the fallback model.
- Every attempt, retries included, reserves its estimated tokens with the rate limiter.
- Output is requested as JSON constrained by the schema (`response_format=json_schema`,
  best-effort mode by default: Groq's strict mode needs every property required) and is
  validated again here. Invalid output is returned as `parsed=None` with the errors; the
  caller's single re-ask (`generate_validated`) decides what happens next.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from app.config import Settings
from app.core.ratelimit import RateLimiter, estimate_tokens
from app.llm.common import (
    ProviderRefused,
    ProviderUnavailable,
    T,
    TokenUsage,
    parse_structured,
    text_sha256,
)
from app.llm.provider import LLMProvider, LLMRequest, LLMResult
from app.llm.transport import (
    Clock,
    Sleep,
    TransientError,
    final_error,
    is_transport_error,
    jittered_backoff,
)

PROVIDER_NAME = "groq"
DEFAULT_TIMEOUT_S = 30.0
MAX_RETRIES_CAP = 2
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 8.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
TIMEOUT_ERROR_NAMES = frozenset({"APITimeoutError", "ReadTimeout", "TimeoutException"})
REFUSAL_FINISH = frozenset({"content_filter"})
# Groq rejects output that fails its own JSON/schema check with HTTP 400 and this code.
# That is invalid output (the caller's re-ask applies), not a transport failure.
OUTPUT_VALIDATION_CODES = frozenset({"json_validate_failed"})

# (request, schema, timeout_s) -> SDK ChatCompletion-like object
CompletionFn = Callable[[LLMRequest, type[Any], float], Awaitable[Any]]


class _InvalidOutput(Exception):
    """Groq refused its own generation as schema-invalid (HTTP 400 json_validate_failed)."""


@dataclass(repr=False)
class GroqProvider:
    model_id: str
    api_key: SecretStr
    limiter: RateLimiter
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = MAX_RETRIES_CAP
    backoff_initial_s: float = BACKOFF_INITIAL_S
    backoff_cap_s: float = BACKOFF_CAP_S
    strict_schema: bool = False
    sleep: Sleep = asyncio.sleep
    clock: Clock = time.monotonic
    rng: Callable[[], float] | None = None  # jitter source; injected in tests
    complete: CompletionFn | None = None  # injected in tests; default = live SDK call
    name: str = PROVIDER_NAME
    _client: Any = field(init=False, default=None)
    attempts_made: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= MAX_RETRIES_CAP:
            raise ValueError(f"max_retries must be 0..{MAX_RETRIES_CAP}")
        if not 0 < self.timeout_s <= 60:
            raise ValueError("timeout_s must be in (0, 60]")
        if not 0 < self.backoff_initial_s <= self.backoff_cap_s <= 30:
            raise ValueError("need 0 < backoff_initial_s <= backoff_cap_s <= 30")

    def __repr__(self) -> str:  # never shows the key
        return f"GroqProvider(model_id={self.model_id!r})"

    @classmethod
    def from_settings(
        cls, settings: Settings, *, limiter: RateLimiter, model_id: str | None = None
    ) -> "GroqProvider":
        """The live provider (for `model_id`, default `PRIMARY_LLM_MODEL`), or
        `ProviderUnavailable` naming every missing gate."""
        missing = [
            name
            for name, ok in (
                ("GROQ_LIVE_ENABLED=true", settings.GROQ_LIVE_ENABLED),
                ("GROQ_API_KEY", settings.GROQ_API_KEY is not None),
                ("PRIMARY_LLM_MODEL", settings.PRIMARY_LLM_MODEL is not None),
                ("GROQ_DATA_TERMS_VERIFIED_ON", settings.GROQ_DATA_TERMS_VERIFIED_ON),
            )
            if not ok
        ]
        if missing:
            raise ProviderUnavailable("Groq live calls are disabled; set " + ", ".join(missing))
        key, model = settings.GROQ_API_KEY, settings.PRIMARY_LLM_MODEL
        if key is None or model is None:  # unreachable after the check above (for mypy)
            raise ProviderUnavailable("Groq is not configured")
        return cls(
            model_id=model_id or model,
            api_key=key,
            limiter=limiter,
            timeout_s=settings.GROQ_TIMEOUT_S,
            max_retries=settings.GROQ_MAX_RETRIES,
            backoff_initial_s=settings.GROQ_BACKOFF_INITIAL_S,
            backoff_cap_s=settings.GROQ_BACKOFF_CAP_S,
        )

    # --- public API -------------------------------------------------------------------

    async def generate_structured(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        tokens = estimate_tokens(
            request.system, request.user, max_output_tokens=request.max_output_tokens
        )
        attempt = 0
        while True:
            await self.limiter.acquire(tokens)
            self.attempts_made += 1
            started = self.clock()
            try:
                response = await asyncio.wait_for(
                    self._complete(request, schema), timeout=self.timeout_s
                )
                return self._result(
                    response, schema, latency_s=self.clock() - started, attempts=attempt + 1
                )
            except _InvalidOutput as exc:
                return self._invalid(str(exc), latency_s=self.clock() - started,
                                     attempts=attempt + 1)  # fmt: skip
            except TimeoutError as exc:
                failure = TransientError("timeout", f"no response within {self.timeout_s:.0f} s")
                cause: BaseException = exc
            except TransientError as exc:
                failure, cause = exc, exc
            if attempt >= self.max_retries:
                raise final_error(failure, attempts=attempt + 1) from cause
            await self.sleep(self._backoff(attempt))
            attempt += 1

    # --- internals --------------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        if self.rng is None:
            return jittered_backoff(
                attempt, initial_s=self.backoff_initial_s, cap_s=self.backoff_cap_s
            )
        return jittered_backoff(
            attempt, initial_s=self.backoff_initial_s, cap_s=self.backoff_cap_s, rng=self.rng
        )

    def _redact(self, text: str) -> str:
        key = self.api_key.get_secret_value()
        return text.replace(key, "[redacted]") if key else text

    async def _complete(self, request: LLMRequest, schema: type[T]) -> Any:
        call = self.complete or self._live_complete
        try:
            return await call(request, schema, self.timeout_s)
        except (TransientError, _InvalidOutput, ProviderRefused, ProviderUnavailable):
            raise
        except TimeoutError:
            raise
        except Exception as exc:  # SDK / transport errors -> classified, redacted
            classified = self._classify(exc)
        # Raised outside the handler: the SDK exception (whose text or body may echo the
        # request) is neither the cause nor the context of what propagates.
        raise classified

    def _classify(self, exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        names = {c.__name__ for c in type(exc).__mro__}
        detail = self._redact(f"{type(exc).__name__} {status or ''}".strip())
        if names & TIMEOUT_ERROR_NAMES:
            return TransientError("timeout", detail)
        if isinstance(status, int):
            if status == 429:
                return TransientError("rate_limited", detail)
            if status in RETRYABLE_STATUS:
                return TransientError("server", detail)
            if status == 400 and _error_code(exc) in OUTPUT_VALIDATION_CODES:
                return _InvalidOutput(f"<root>: provider rejected the output ({detail})")
            return ProviderUnavailable(f"Groq request rejected: {detail}")
        if is_transport_error(exc):
            return TransientError("transport", detail)
        return ProviderUnavailable(f"Groq call failed: {detail}")

    def _invalid(self, error: str, *, latency_s: float, attempts: int) -> LLMResult[Any]:
        return LLMResult[Any](
            parsed=None,
            raw_text_sha256=text_sha256(""),
            validation_errors=[error],
            provider=self.name,
            model_id=self.model_id,
            latency_ms=max(0, round(latency_s * 1000)),
            finish_reason="json_validate_failed",
            attempts=attempts,
        )

    def _result(
        self, response: Any, schema: type[T], *, latency_s: float, attempts: int
    ) -> LLMResult[T]:
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        finish = getattr(choice, "finish_reason", None)
        if finish in REFUSAL_FINISH or getattr(message, "refusal", None):
            raise ProviderRefused(f"Groq stopped generation: {finish or 'refusal'}")
        raw = str(getattr(message, "content", None) or "")
        parsed, errors = parse_structured(raw, schema)
        if not raw:
            errors = ["<root>: empty response"]
        elif finish == "length":
            errors = ["<root>: output truncated (length)", *errors]
        usage_obj = getattr(response, "usage", None)
        usage = (
            TokenUsage(
                input_tokens=getattr(usage_obj, "prompt_tokens", None),
                output_tokens=getattr(usage_obj, "completion_tokens", None),
            )
            if usage_obj is not None
            else None
        )
        return LLMResult[schema](  # type: ignore[valid-type]
            parsed=parsed if not errors else None,
            raw_text_sha256=text_sha256(raw),
            validation_errors=errors,
            provider=self.name,
            model_id=self.model_id,
            usage=usage,
            latency_ms=max(0, round(latency_s * 1000)),
            finish_reason=str(finish) if finish is not None else None,
            attempts=attempts,
        )

    def request_kwargs(self, request: LLMRequest, schema: type[Any]) -> dict[str, Any]:
        """The chat-completions arguments (public for tests; contains the prompt)."""
        return {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": self.strict_schema,
                },
            },
            "temperature": 0.0,
            "max_completion_tokens": request.max_output_tokens,
            "stream": False,
        }

    async def _live_complete(self, request: LLMRequest, schema: type[Any], timeout_s: float) -> Any:
        client = self._live_client()
        return await client.chat.completions.create(**self.request_kwargs(request, schema))

    def _live_client(self) -> Any:
        if self._client is None:
            from groq import AsyncGroq  # lazy: only live calls need the SDK

            self._client = AsyncGroq(
                api_key=self.api_key.get_secret_value(),
                timeout=self.timeout_s,
                max_retries=0,  # our loop only
            )
        return self._client


def _error_code(exc: Exception) -> str | None:
    """The provider's machine-readable error code, if any (never its message text)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            code = err.get("code")
            return str(code) if code is not None else None
    return None


def llm_from_settings(settings: Settings, *, limiter: RateLimiter | None = None) -> LLMProvider:
    """The live Groq provider (primary, plus `FALLBACK_LLM_MODEL` when configured and
    different), sharing one rate limiter, wrapped in the opt-in response cache.

    Raises `ProviderUnavailable` when live calls are not enabled. The default limiter is
    the shared Redis one, falling back to an in-process window."""
    from app.core.ratelimit import redis_rate_limiter
    from app.llm.cache import cached_from_settings
    from app.llm.provider import FallbackLLMProvider

    if limiter is None:
        limiter = redis_rate_limiter(
            settings.REDIS_URL.get_secret_value(),
            provider=PROVIDER_NAME,
            rpm=settings.RATE_LIMIT_GROQ_RPM,
            tpm=settings.RATE_LIMIT_GROQ_TPM,
            max_wait_s=settings.RATE_LIMIT_MAX_WAIT_S,
            op_timeout_s=settings.RATE_LIMIT_REDIS_TIMEOUT_S,
        )
    primary = GroqProvider.from_settings(settings, limiter=limiter)
    provider: LLMProvider = primary
    fallback_model = settings.FALLBACK_LLM_MODEL
    if fallback_model is not None and fallback_model != primary.model_id:
        fallback = GroqProvider.from_settings(settings, limiter=limiter, model_id=fallback_model)
        provider = FallbackLLMProvider(primary, fallback)
    return cached_from_settings(settings, provider)


__all__ = ["GroqProvider", "llm_from_settings"]
