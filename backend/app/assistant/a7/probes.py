"""Test-only probes that plug into the existing interfaces (never the product path).

- `RecordingLLM`: an `LLMProvider` that records every request (what the LLM would have
  received) and answers without the network: no slot mentions for extraction, and an
  invalid voice reply, so the deterministic reply is used and stored answers stay identical
  to a run without an LLM.
- `CapturingChecklist`: wraps the checklist factory and keeps every internal
  `ChecklistAnswer` (with chunk ids, which the API never exposes) for the evidence audit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.assistant.checklist import ChecklistAnswer, ChecklistService
from app.assistant.dialogue import ReadyProfile
from app.llm.multimodal import parse_structured
from app.llm.provider import LLMRequest, LLMResult


@dataclass
class RecordingLLM:
    name: str = "a7-recording"
    model_id: str = "a7-recording-0"
    requests: list[LLMRequest] = field(default_factory=list)

    async def generate_structured(self, request: LLMRequest, schema: type[Any]) -> LLMResult[Any]:
        self.requests.append(request)
        raw = '{"mentions": []}' if schema.__name__ == "LLMSlotExtraction" else "{}"
        parsed, errors = parse_structured(raw, schema)
        return LLMResult[schema](  # type: ignore[valid-type]
            parsed=parsed,
            raw_text_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            validation_errors=errors,
            provider=self.name,
            model_id=self.model_id,
            latency_ms=0,
            finish_reason="stop",
        )

    def contexts(self) -> list[str]:
        """The delimited conversation blocks of every request, in order."""
        out = []
        for r in self.requests:
            for start, end in (
                ("<recent_conversation>", "</recent_conversation>"),
                ("<conversation>", "</conversation>"),
            ):
                if start in r.user:
                    out.append(r.user.split(start, 1)[1].split(end, 1)[0])
        return out


class CapturingChecklist:
    """A `ChecklistFactory` that records each internal checklist answer it builds."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.built: list[tuple[ReadyProfile, ChecklistAnswer]] = []

    def __call__(self) -> ChecklistService:
        inner = self._factory()
        outer = self

        class _Recording:
            def build(self, ready: ReadyProfile) -> ChecklistAnswer:
                answer: ChecklistAnswer = inner.build(ready)
                outer.built.append((ready, answer))
                return answer

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        return _Recording()  # type: ignore[return-value]
