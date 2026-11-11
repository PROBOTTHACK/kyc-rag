"""`input_security` node (M3 S3; M3_PLAN §3, §6; docs/06 A).

Deterministic and monotonic:
- It screens the query and every stored clarification answer with `screen_text` on every
  pass (the first one and after each clarification round), so a resumed answer can never
  bypass it.
- The status is recomputed from the content: a `security_status` supplied in the input
  is ignored, so a caller cannot declare its own input safe. A previous `blocked` or
  `review` is never relaxed.
- Redaction (email, phone) replaces `query`; identifier-bearing text is removed from
  state entirely.
- Reasons are category codes only, never the matched text.

`review` is not processed in M3: routing sends it to `finalize` (outcome BLOCKED); M4
turns it into a review case. This is a narrow pattern filter, not Prompt Guard (M4), and
it does not guarantee that every injection attempt or personal detail is caught.
"""

from typing import TYPE_CHECKING, Any

from app.graph.state import KYCState, ScreenRecord, SecurityStatus, dump
from app.security.screen import screen_text, strictest

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps

_STATUS = {"allow": "safe", "redact": "safe", "review": "review", "block": "blocked"}
_SEVERITY = {"safe": 0, "review": 1, "blocked": 2}


def input_security(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    query = screen_text(state.get("query", ""), max_chars=deps.query_max_chars)
    answers = [
        screen_text(a.get("value"), max_chars=deps.query_max_chars)
        for a in state.get("clarification_answers") or []
    ]
    action = strictest(query.action, *(a.action for a in answers))
    categories = sorted(set(query.categories).union(*(a.categories for a in answers)))
    counts: dict[str, int] = dict(query.counts)
    for a in answers:
        for k, v in a.counts.items():
            counts[k] = counts.get(k, 0) + v

    previous = state.get("security_status")
    status = _STATUS[action]
    if previous in (SecurityStatus.BLOCKED, SecurityStatus.REVIEW):
        status = max(status, str(previous), key=lambda s: _SEVERITY[s])  # never relaxed

    record = ScreenRecord(stage="input_security", action=action, categories=categories,
                          counts=counts)  # fmt: skip
    reasons = list(dict.fromkeys([*(state.get("security_reasons") or []), *categories]))
    update: dict[str, Any] = {
        "security_status": status,
        "security_reasons": reasons,
        "screening": [*(state.get("screening") or []), dump(record)],
    }
    if query.text is None:
        update["query"] = ""  # identifier-bearing or unusable text never stays in state
    elif query.text != state.get("query"):
        update["query"] = query.text
    return update
