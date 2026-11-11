"""Provider-agnostic contract for multimodal page analysis (M2 Pipeline B; docs/02, docs/04).

A provider receives rendered page images plus an instruction, and returns the model's
text parsed into a caller-supplied Pydantic schema. Nothing here knows about Gemini:
`app/llm/gemini.py` is one implementation, `app/llm/fake.py` another (tests).

Page images and the text a model reads from them are untrusted data. The provider only
returns structured output; it is validated here and never executed or obeyed.
"""

from typing import Generic, Protocol

from pydantic import BaseModel, ConfigDict, Field

# Shared with the M3 text contract; re-exported so existing imports keep working.
from app.llm.common import (
    MAX_ERRORS,
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
    T,
    TokenUsage,
    _Frozen,
    parse_structured,
    text_sha256,
)

__all__ = [
    "MAX_ERRORS",
    "Citation",
    "MultimodalProvider",
    "MultimodalRequest",
    "MultimodalResult",
    "PageImage",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRecitation",
    "ProviderRefused",
    "ProviderTimeout",
    "ProviderUnavailable",
    "RecitationDiagnostics",
    "T",
    "TokenUsage",
    "parse_structured",
    "text_sha256",
]


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


class Citation(_Frozen):
    """One source the provider says its output recited (public citation metadata)."""

    uri: str | None = None
    title: str | None = None
    license: str | None = None
    publication_date: str | None = None
    start_index: int | None = None
    end_index: int | None = None


class RecitationDiagnostics(_Frozen):
    """What a RECITATION stop looked like. The partial generated text itself is never
    kept: only its length and SHA-256."""

    provider: str
    model_id: str
    finish_reason: str  # RECITATION | IMAGE_RECITATION
    finish_message: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    partial_output_chars: int = Field(ge=0)
    partial_output_sha256: str | None = None  # None when there was no partial output


class ProviderRecitation(ProviderRefused):
    """The provider stopped because its output would recite source material. A refusal:
    never retried and never sent to another model (`transient` stays False)."""

    def __init__(self, message: str, diagnostics: RecitationDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class MultimodalProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    async def analyze_pages(
        self, images: list[bytes], request: MultimodalRequest, schema: type[T]
    ) -> MultimodalResult[T]: ...
