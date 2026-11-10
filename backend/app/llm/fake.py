"""Scripted multimodal provider for tests: never touches the network."""

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.llm.multimodal import (
    MultimodalRequest,
    MultimodalResult,
    T,
    parse_structured,
    text_sha256,
)


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
