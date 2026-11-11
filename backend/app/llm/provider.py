"""Provider-agnostic text LLM contract (M3 S1; docs/02 "Provider abstraction").

Every text LLM call in the workflow goes through `LLMProvider.generate_structured`:
the model's text is parsed into a caller-supplied Pydantic schema and validated here.
Invalid output is returned as `parsed=None` with the errors, never repaired.
`generate_validated` adds the project's single re-ask, then gives up gracefully.

Untrusted content: the `user` part of a request may contain retrieved corpus text. It
must be wrapped in delimiters by the caller (M3 S5). The provider never executes or
obeys model output; it only validates it.

What never appears in a result, a log or an error: the API key, the prompt text, the
raw model output (only its SHA-256) and any provider error body.
"""

from dataclasses import dataclass
from typing import Generic, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.llm.common import (
    MAX_ERRORS,
    ProviderError,
    T,
    TokenUsage,
    _Frozen,
)

MAX_REPAIR_ERRORS = 10  # validation messages quoted back in the single re-ask


class LLMRequest(_Frozen):
    """One structured-output request. `system` is the fixed, trusted instruction;
    `user` holds the task and any (delimited, untrusted) evidence."""

    prompt_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)
    max_output_tokens: int = Field(default=2048, gt=0, le=16384)
    # Set ONLY by callers that verified the input is an allowlisted synthetic demo input
    # from a `synthetic_demo` session (M3 D8). It is the allowlist digest, re-checked by
    # the cache; None means "never cache".
    synthetic_input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class LLMResult(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True)

    parsed: T | None  # None when the output failed validation
    raw_text_sha256: str
    validation_errors: list[str] = Field(default_factory=list)
    provider: str
    model_id: str
    usage: TokenUsage | None = None
    latency_ms: int = Field(ge=0)
    finish_reason: str | None = None
    cache_hit: bool = False
    attempts: int = Field(default=1, ge=0)  # API attempts behind this result (0 = cache)


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    async def generate_structured(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]: ...


@dataclass
class FallbackLLMProvider:
    """Primary + fallback model. The fallback is tried only when the primary gave up
    after exhausting retries on a transient failure (`ProviderError.transient`).
    Refusals, invalid requests, auth failures and invalid output never fall back."""

    primary: LLMProvider
    fallback: LLMProvider
    name: str = "groq"

    @property
    def model_id(self) -> str:
        return self.primary.model_id

    async def generate_structured(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        try:
            return await self.primary.generate_structured(request, schema)
        except ProviderError as exc:
            if not exc.transient:
                raise
        return await self.fallback.generate_structured(request, schema)


def repair_request(request: LLMRequest, errors: list[str]) -> LLMRequest:
    """The single re-ask: same system instruction, the task restated with the validation
    errors (never the previous raw output, which is untrusted and may echo evidence)."""
    quoted = "\n".join(f"- {e}" for e in errors[:MAX_REPAIR_ERRORS]) or "- invalid output"
    user = (
        f"{request.user}\n\n"
        "Your previous answer did not match the required JSON schema. Errors:\n"
        f"{quoted}\n"
        "Answer again with only JSON that matches the schema."
    )
    return request.model_copy(update={"user": user, "prompt_id": f"{request.prompt_id}:repair"})


@dataclass(frozen=True)
class ValidatedCall(Generic[T]):
    """Outcome of `generate_validated`: the final result plus every attempt's result."""

    result: LLMResult[T]
    attempts: tuple[LLMResult[T], ...]

    @property
    def ok(self) -> bool:
        return self.result.parsed is not None

    @property
    def repaired(self) -> bool:
        return len(self.attempts) > 1


async def generate_validated(
    provider: LLMProvider, request: LLMRequest, schema: type[T]
) -> ValidatedCall[T]:
    """One call, then at most one re-ask if the output failed validation (CLAUDE.md:
    "on invalid JSON retry once then degrade gracefully"). Provider errors propagate:
    the caller decides how to degrade. Never more than two calls."""
    first = await provider.generate_structured(request, schema)
    if first.parsed is not None:
        return ValidatedCall(first, (first,))
    second = await provider.generate_structured(
        repair_request(request, first.validation_errors[:MAX_ERRORS]), schema
    )
    return ValidatedCall(second, (first, second))
