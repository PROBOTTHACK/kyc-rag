"""`policy_retrieval` node: S2 stub (M3_PLAN §3).

S4 implements filtered retrieval with provenance and degradation labels.

The stub makes no decision and changes nothing: routing reads the state as given and
fails closed on anything missing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def policy_retrieval(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {}
