"""`finalize` node: S2 stub (M3_PLAN §5, §9). S5 adds the insert-only report and S6 the
run bookkeeping.

The stub only decides the single report outcome by precedence (`decide_outcome`); it
persists nothing.
"""

from typing import TYPE_CHECKING, Any

from app.graph.routing import decide_outcome
from app.graph.state import KYCState

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def finalize(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    return {"outcome": decide_outcome(state).value}
