"""`input_security` node: S2 stub (M3_PLAN §3).

S3 implements deterministic input checks and the in-graph PII re-screen.

The stub makes no decision and changes nothing: routing reads the state as given and
fails closed on anything missing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def input_security(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {}
