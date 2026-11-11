"""Provider-neutral pieces shared by every LLM contract (M2 multimodal, M3 text).

Moved here unchanged from `app/llm/multimodal.py` in M3 S1 so the text contract
(`app/llm/provider.py`) can share the same error hierarchy and output validation.
`app.llm.multimodal` re-exports every name, so existing imports keep working and an
`except ProviderError` catches both kinds of provider.
"""

import hashlib
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

T = TypeVar("T", bound=BaseModel)

MAX_ERRORS = 20  # cap on validation messages kept per result


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TokenUsage(_Frozen):
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Exception):
    """Base class; messages must never contain secrets. `transient` is True only when a
    provider gave up after exhausting retries on rate limits, server errors or timeouts
    (never for refusals, invalid requests or auth failures)."""

    transient: bool = False


class ProviderUnavailable(ProviderError):
    """Not configured, network failure or a server error after retries."""


class ProviderRateLimited(ProviderError):
    """Quota or rate limit still exceeded after retries."""


class ProviderRefused(ProviderError):
    """The model refused or a safety filter blocked the response."""


class ProviderTimeout(ProviderError):
    """The call exceeded its timeout after retries."""


def parse_structured(raw_text: str, schema: type[T]) -> tuple[T | None, list[str]]:
    """Validate model text against `schema`: `(parsed, [])` or `(None, errors)`."""
    try:
        return schema.model_validate_json(raw_text), []
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        ]
        return None, errors[:MAX_ERRORS]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
