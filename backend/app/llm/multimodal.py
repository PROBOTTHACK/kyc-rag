"""Provider-agnostic contract for multimodal page analysis (M2 Pipeline B; docs/02, docs/04).

A provider receives rendered page images plus an instruction, and returns the model's
text parsed into a caller-supplied Pydantic schema. Nothing here knows about Gemini:
`app/llm/gemini.py` is one implementation, `app/llm/fake.py` another (tests).

Page images and the text a model reads from them are untrusted data. The provider only
returns structured output; it is validated here and never executed or obeyed.
"""

import hashlib
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

T = TypeVar("T", bound=BaseModel)

MAX_ERRORS = 20  # cap on validation messages kept per result


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageImage(_Frozen):
    document_key: str
    page_number: int = Field(ge=1)
    png_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dpi: int = Field(gt=0)


class MultimodalRequest(_Frozen):
    prompt_id: str
    prompt_version: str
    instruction: str  # task text; the provider adds its fixed safety instruction
    pages: list[PageImage]


class TokenUsage(_Frozen):
    input_tokens: int | None = None
    output_tokens: int | None = None


class MultimodalResult(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True)

    parsed: T | None  # None when the output failed validation
    raw_text_sha256: str
    validation_errors: list[str] = Field(default_factory=list)
    provider: str
    model_id: str
    usage: TokenUsage | None = None
    latency_ms: int = Field(ge=0)
    finish_reason: str | None = None


class ProviderError(Exception):
    """Base class; messages must never contain secrets."""


class ProviderUnavailable(ProviderError):
    """Not configured, network failure or a server error after retries."""


class ProviderRateLimited(ProviderError):
    """Quota or rate limit still exceeded after retries."""


class ProviderRefused(ProviderError):
    """The model refused or a safety filter blocked the response."""


class ProviderTimeout(ProviderError):
    """The call exceeded its timeout after retries."""


class MultimodalProvider(Protocol):
    name: str
    model_id: str

    async def analyze_pages(
        self, images: list[bytes], request: MultimodalRequest, schema: type[T]
    ) -> MultimodalResult[T]: ...


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
