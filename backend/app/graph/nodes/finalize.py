"""`finalize` node (M3 S5; M3_PLAN §5): the single report of the run.

Every terminal path ends here (BLOCKED, INSUFFICIENT_INFORMATION, INSUFFICIENT_EVIDENCE,
DEGRADED, CHECKLIST_REVIEW_REQUIRED). The outcome comes from `decide_outcome`; the report
and its deterministic user message from `app.graph.report.build_report`. The report id is
derived from the session and run, so running finalize twice stores one report.

Persistence goes through the injected `ReportStore` (insert-only; S6 wires the Postgres
store into the API). Without a store the report is kept in state only. Run bookkeeping
(`graph_runs`) is S6.
"""

from typing import TYPE_CHECKING, Any

from app.graph.report import build_report
from app.graph.routing import decide_outcome
from app.graph.state import KYCState, dump

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def finalize(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    outcome = decide_outcome(state)
    report = build_report(state, outcome)
    if deps.reports is not None:
        deps.reports.save(report)
    return {"outcome": outcome.value, "report_id": report.report_id, "report": dump(report)}
