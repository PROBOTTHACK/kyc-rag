"""Scripted providers for tests (multimodal, and the M3 text LLM): never touch the network."""

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.llm.multimodal import (
    MultimodalRequest,
    MultimodalResult,
    T,
    parse_structured,
    text_sha256,
)
from app.llm.provider import LLMRequest, LLMResult


@dataclass
class FakeCall:
    request: MultimodalRequest
    image_count: int


@dataclass
class FakeMultimodalProvider:
    """Returns the scripted responses in order: a string is the model's raw text
    (valid or malformed JSON), an exception is raised as-is (refusal, timeout, ...)."""

    responses: list[str | Exception]
    name: str = "fake"
    model_id: str = "fake-multimodal-1"
    calls: list[FakeCall] = field(default_factory=list)

    @property
    def attempts_made(self) -> int:
        return len(self.calls)

    async def analyze_pages(
        self, images: list[bytes], request: MultimodalRequest, schema: type[T]
    ) -> MultimodalResult[T]:
        self.calls.append(FakeCall(request=request, image_count=len(images)))
        if not self.responses:
            raise AssertionError("FakeMultimodalProvider: no scripted response left")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        parsed, errors = parse_structured(nxt, schema)
        return MultimodalResult[schema](  # type: ignore[valid-type]
            parsed=parsed,
            raw_text_sha256=text_sha256(nxt),
            validation_errors=errors,
            provider=self.name,
            model_id=self.model_id,
            latency_ms=0,
            finish_reason="stop",
        )


def scripted(*responses: BaseModel | str | Exception) -> FakeMultimodalProvider:
    """Convenience: models are serialised to JSON, strings and exceptions kept."""
    return FakeMultimodalProvider(
        [r.model_dump_json() if isinstance(r, BaseModel) else r for r in responses]
    )


# --- M3 S1: scripted text LLM -------------------------------------------------------


@dataclass
class FakeLLMProvider:
    """Scripted text LLM for tests: never touches the network. A string is the model's
    raw text (valid or malformed JSON), an exception is raised as-is. Every request is
    recorded, so tests can assert what would have been sent (for example, no PII)."""

    responses: list[str | Exception]
    name: str = "fake"
    model_id: str = "fake-llm-1"
    requests: list[LLMRequest] = field(default_factory=list)

    @property
    def attempts_made(self) -> int:
        return len(self.requests)

    async def generate_structured(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeLLMProvider: no scripted response left")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        parsed, errors = parse_structured(nxt, schema)
        return LLMResult[schema](  # type: ignore[valid-type]
            parsed=parsed,
            raw_text_sha256=text_sha256(nxt),
            validation_errors=errors,
            provider=self.name,
            model_id=self.model_id,
            latency_ms=0,
            finish_reason="stop",
        )


def scripted_llm(*responses: BaseModel | str | Exception) -> FakeLLMProvider:
    """Convenience: models are serialised to JSON, strings and exceptions kept."""
    return FakeLLMProvider(
        [r.model_dump_json() if isinstance(r, BaseModel) else r for r in responses]
    )
