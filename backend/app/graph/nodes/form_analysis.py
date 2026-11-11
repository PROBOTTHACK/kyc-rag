"""`form_analysis` node: S2 stub (M3_PLAN §3).

S4 implements stored form schemas only (never a live Gemini call).

The stub makes no decision and changes nothing: routing reads the state as given and
fails closed on anything missing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def form_analysis(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {}
