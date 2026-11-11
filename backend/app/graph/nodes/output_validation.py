"""`output_validation` node: S2 stub (M3_PLAN §3).

S5 implements the authoritative deterministic gate on citations and MANDATORY.

The stub makes no decision and changes nothing: routing reads the state as given and
fails closed on anything missing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def output_validation(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {}
