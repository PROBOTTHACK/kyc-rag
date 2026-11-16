"""The documented contract of `/api/v2/assistant` (A5).

Documentation models, as in v1 (M5 S1): the routes never validate or re-serialise through
them, and the service output is sent unchanged. A contract test checks that real responses
validate against them (`extra="forbid"` everywhere). Names carry an `Assistant` prefix so the
published v1 schema names stay unchanged.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.answer import AssistantAnswer, ProfileSlot
from app.assistant.dialogue import Action
from app.assistant.profile import Slot


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantTurnRequest(_Doc):
    """One turn: a free-text message, a choice, or both. `value` is a chip value of `slot`
    (choose / correct only). The message is screened; identifiers are refused, never stored."""

    message: str | None = Field(default=None, description="Free text (screened).")
    action: str | None = Field(
        default=None, json_schema_extra={"enum": [a.value for a in Action] + [None]}
    )
    slot: str | None = Field(
        default=None, json_schema_extra={"enum": [s.value for s in Slot] + [None]}
    )
    value: str | None = Field(default=None, description="A chip value of `slot`.")


class AssistantSessionStarted(_Doc):
    session_id: str
    turn: int = Field(description="Always 0: the opening question.")
    answer: AssistantAnswer


class AssistantTurnResult(_Doc):
    session_id: str
    turn: int = Field(description="The latest stored turn.")
    answer: AssistantAnswer


class AssistantChoice(_Doc):
    action: str | None
    slot: str | None
    value: str | None


class AssistantTurnView(_Doc):
    turn: int
    message: str | None = Field(description="The screened message; null when refused or absent.")
    choice: AssistantChoice | None
    answer: AssistantAnswer


class AssistantSessionView(_Doc):
    session_id: str
    turn: int
    profile: dict[Slot, ProfileSlot]
    turns: list[AssistantTurnView]
    answer: AssistantAnswer | None
