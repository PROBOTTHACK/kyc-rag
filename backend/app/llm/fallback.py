"""Primary + fallback multimodal provider (M2 G3 follow-up; docs/02).

The fallback model is tried for a request only when the primary gave up after exhausting
its retries on a transient failure (rate limit, server error, timeout, transport):
`ProviderError.transient`. Refusals, invalid requests, auth failures and invalid output
are never retried on another model. Each result carries the `model_id` that answered.
"""

from dataclasses import dataclass

from app.llm.multimodal import (
    MultimodalProvider,
    MultimodalRequest,
    MultimodalResult,
    ProviderError,
    T,
)


@dataclass
class FallbackMultimodalProvider:
    primary: MultimodalProvider
    fallback: MultimodalProvider
    name: str = "gemini"

    @property
    def model_id(self) -> str:
        return self.primary.model_id

    @property
    def attempts_made(self) -> int:
        return int(getattr(self.primary, "attempts_made", 0)) + int(
            getattr(self.fallback, "attempts_made", 0)
        )

    async def analyze_pages(
        self, images: list[bytes], request: MultimodalRequest, schema: type[T]
    ) -> MultimodalResult[T]:
        try:
            return await self.primary.analyze_pages(images, request, schema)
        except ProviderError as exc:
            if not exc.transient:
                raise
        return await self.fallback.analyze_pages(images, request, schema)
