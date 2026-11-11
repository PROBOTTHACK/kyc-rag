"""The gate in front of the graph: what may enter workflow state (M3 S3; M3_PLAN §6).

LangGraph checkpoints the graph input AND every `Command(resume=...)` value. So user text
must be screened *before* it is handed to the graph, not only inside it. These two
functions are that gate; the S6 API calls them for `POST /messages` and `POST /resume`
and maps the exceptions to `422`. Nothing rejected here is stored or sent anywhere.

- `start_input`: screens the question. Identifiers (PAN, Aadhaar, passport, card,
  account numbers) -> `SensitiveDataDetected`; empty, oversized or invalid text ->
  `InputRejected`. Email and phone are redacted. Anything else (including injection
  phrases) enters the graph as screened text, where `input_security` records the
  decision and the run ends `BLOCKED` with reason codes only.
- `resume_command`: validates the structured answer `{"answers": {field: value}}` (enum
  values only, no free text, M3 D7) and screens every string in it. Only then is a
  `Command(resume=...)` built.

The graph repeats both checks (`input_security`, `clarify`), so a caller that skips the
gate still cannot get unscreened text to the LLM; it would, however, leave that text in
the checkpoint. Hence: always enter through this module.
"""

from typing import Any

from langgraph.types import Command

from app.graph.intake import answer_problems, flatten_strings
from app.graph.state import DataClass, KYCState, ScreenRecord, dump
from app.security.screen import DEFAULT_MAX_CHARS, screen_text


class InputRejected(ValueError):
    """The input cannot enter the workflow. `categories` are codes only, never content."""

    def __init__(self, message: str, categories: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.categories = categories


class SensitiveDataDetected(InputRejected):
    """A government or financial identifier was found (HTTP 422 SENSITIVE_DATA_DETECTED)."""


class InvalidAnswer(InputRejected):
    """A clarification answer is malformed or not an allowed value."""


def start_input(
    query: object,
    *,
    session_id: str,
    run_id: str,
    data_class: DataClass | str = DataClass.UNSPECIFIED,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> KYCState:
    """The graph input for a new question, or an exception (nothing is stored)."""
    result = screen_text(query, max_chars=max_chars)
    if result.sensitive:
        raise SensitiveDataDetected("sensitive data detected", result.categories)
    if result.unusable or result.text is None:
        raise InputRejected("input cannot be processed", result.categories)
    record = ScreenRecord(stage="messages", action=result.action,
                          categories=list(result.categories), counts=result.counts)  # fmt: skip
    return {
        "session_id": session_id,
        "run_id": run_id,
        "data_class": DataClass(data_class).value,
        "query": result.text,
        "clarification_rounds": 0,
        "screening": [dump(record)],
    }


def resume_command(payload: Any, *, max_chars: int = DEFAULT_MAX_CHARS) -> Command[Any]:
    """`Command(resume=payload)` for a clarification answer, or an exception."""
    for text in flatten_strings(payload):
        result = screen_text(text, max_chars=max_chars)
        if result.sensitive:
            raise SensitiveDataDetected("sensitive data detected", result.categories)
    problems = answer_problems(payload)
    if problems:
        raise InvalidAnswer("; ".join(sorted(set(problems))))
    for text in flatten_strings(payload):
        if screen_text(text, max_chars=max_chars).action != "allow":
            raise InvalidAnswer("answer failed screening")
    return Command(resume={"answers": dict(payload["answers"])})
