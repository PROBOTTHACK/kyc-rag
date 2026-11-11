"""`compliance_synthesis` node: S2 stub (M3_PLAN §3).

S5 implements the LLM-proposed checklist citing supplied evidence ids only.

The stub makes no decision and changes nothing: routing reads the state as given and
fails closed on anything missing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def compliance_synthesis(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {}
