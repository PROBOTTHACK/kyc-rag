"""`entity_intake` node: S2 stub (M3_PLAN §3).

S3 implements structured intake from what the user stated, with enum validation.

The stub makes no decision and changes nothing: routing reads the state as given and
fails closed on anything missing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def entity_intake(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {}
