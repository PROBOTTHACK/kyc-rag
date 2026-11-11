"""`clarify` node: S2 stub (M3_PLAN §6). S3 replaces it with a LangGraph `interrupt()`
that asks enum-only questions and resumes with screened, structured answers.

The stub only advances the bounded round counter, so the intake <-> clarify loop ends
after `max_clarification_rounds` exactly as the real node will.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def clarify(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {"clarification_rounds": (state.get("clarification_rounds") or 0) + 1}
