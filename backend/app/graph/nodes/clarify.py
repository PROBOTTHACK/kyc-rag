"""`clarify` node (M3 S3; M3_PLAN §6, decision D7).

Asks targeted questions for the missing or ambiguous intake fields with a LangGraph
`interrupt()`, then resumes with `Command(resume={"answers": {field: value}})`.

- Questions are deterministic templates with a closed set of options (no LLM).
- On resume the payload is screened again (every string, keys included) and validated:
  only fields asked in this round, only allowed enum values. A screening hit blocks the
  run (`security_status=blocked`, codes only, nothing of the answer is kept); a rejected
  answer is not merged and is noted as `<field>:answer_rejected`.
- Each round counts, accepted or not, so the loop ends after `max_clarification_rounds`.
- The graph then goes back through `input_security` (never straight to intake), so a
  resumed answer cannot bypass the security node.

The node re-runs from the top when the graph resumes (LangGraph semantics), so everything
before `interrupt()` is a pure function of the state.
"""

from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt

from app.graph.intake import flatten_strings, parse_answers, questions_for
from app.graph.state import KYCState, ScreenRecord, SecurityStatus, dump
from app.security.screen import screen_text, strictest

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def clarify(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    round_no = (state.get("clarification_rounds") or 0) + 1
    ambiguities = list(state.get("ambiguities") or [])
    questions = questions_for(list(state.get("missing_fields") or []), ambiguities)
    raw = interrupt(
        {
            "kind": "clarification",
            "round": round_no,
            "max_rounds": deps.max_clarification_rounds,
            "questions": [q.model_dump(mode="json") for q in questions],
        }
    )

    screens = [screen_text(t, max_chars=deps.query_max_chars) for t in flatten_strings(raw)]
    action = strictest(*(s.action for s in screens))
    categories = sorted({c for s in screens for c in s.categories})
    counts: dict[str, int] = {}
    for s in screens:
        for k, v in s.counts.items():
            counts[k] = counts.get(k, 0) + v
    record = ScreenRecord(stage="resume", action=action, categories=categories, counts=counts)
    update: dict[str, Any] = {
        "clarification_rounds": round_no,
        "screening": [*(state.get("screening") or []), dump(record)],
    }
    if action in ("block", "review"):
        reasons = [*(state.get("security_reasons") or []), "clarification_answer", *categories]
        status = SecurityStatus.BLOCKED if action == "block" else SecurityStatus.REVIEW
        return update | {
            "security_status": status.value,
            "security_reasons": list(dict.fromkeys(reasons)),
        }

    accepted, rejected = parse_answers(raw, asked=[q.field for q in questions], round_no=round_no)
    ambiguities += [f"{name}:answer_rejected" for name in rejected]
    return update | {
        "clarification_answers": [
            *(state.get("clarification_answers") or []),
            *(dump(a) for a in accepted),
        ],
        "ambiguities": list(dict.fromkeys(ambiguities)),
    }
