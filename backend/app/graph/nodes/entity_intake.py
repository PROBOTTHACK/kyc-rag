"""`entity_intake` node (M3 S3; M3_PLAN §3–§4).

An async node (the LLM layer is async; the graph runs with `ainvoke`/`astream`).

First pass (no `entity_profile` yet): one structured LLM call through the injected
`LLMProvider` (S1; `generate_validated` = one call plus at most one re-ask). The model only
proposes values; `app.graph.intake.interpret` accepts a value only if it is in the closed
vocabulary, unambiguous and backed by words that occur in the screened question.

Later passes (after clarification): no LLM call. The structured answers are merged as
`user_answered`.

Never defaults anything. The LLM sees only the screened query (the node refuses to call
it if the text would not pass screening unchanged, or if security is not `safe`). If the
LLM is missing or fails, every field stays unknown, the failure is recorded as a
degradation (outcome DEGRADED) and the clarification questions take over.
"""

from typing import TYPE_CHECKING, Any

from app.graph.intake import (
    IntakeExtraction,
    apply_answers,
    intake_request,
    interpret,
    missing_fields,
    resolved_ambiguities,
)
from app.graph.state import (
    ClarificationAnswer,
    DataClass,
    Degradation,
    EntityProfile,
    KYCState,
    LLMCallRecord,
    SecurityStatus,
    dump,
    read,
    read_list,
)
from app.llm.cache import synthetic_input_digest
from app.llm.common import ProviderError
from app.llm.provider import generate_validated
from app.security.screen import screen_text

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps

NODE = "entity_intake"


async def _call_llm(
    state: KYCState, deps: "GraphDeps"
) -> tuple[EntityProfile, list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """(profile, ambiguities, llm call records, degradations) from the first pass."""
    query = state.get("query", "")
    if deps.llm is None:
        degradation = Degradation(component="llm", reason="not_configured")
        return EntityProfile(), [], [], [dump(degradation)]
    if screen_text(query, max_chars=deps.query_max_chars).action != "allow":
        reason = "unscreened_input_refused"
        return EntityProfile(), [], [], [dump(Degradation(component="llm", reason=reason))]

    digest = None
    if state.get("data_class") == DataClass.SYNTHETIC_DEMO:
        candidate = synthetic_input_digest(query)
        digest = candidate if candidate in deps.synthetic_allowlist else None
    request = intake_request(query, synthetic_input_digest=digest)
    try:
        call = await generate_validated(deps.llm, request, IntakeExtraction)
    except ProviderError as exc:
        record = LLMCallRecord(node=NODE, provider=deps.llm.name, model_id=deps.llm.model_id,
                               latency_ms=0, attempts=0, result="error")  # fmt: skip
        reason = type(exc).__name__
        degradation = Degradation(component="llm", reason=reason)
        return EntityProfile(), [], [dump(record)], [dump(degradation)]

    records = [
        dump(
            LLMCallRecord(
                node=NODE,
                provider=r.provider,
                model_id=r.model_id,
                latency_ms=r.latency_ms,
                input_tokens=r.usage.input_tokens if r.usage else None,
                output_tokens=r.usage.output_tokens if r.usage else None,
                cache_hit=r.cache_hit,
                attempts=r.attempts,
                result="ok" if r.parsed is not None else "invalid",
            )
        )
        for r in call.attempts
    ]
    if call.result.parsed is None:
        degradation = Degradation(component="llm", reason="invalid_output_after_repair")
        return EntityProfile(), [], records, [dump(degradation)]
    profile, ambiguities = interpret(call.result.parsed, query)
    return profile, ambiguities, records, []


async def entity_intake(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    if state.get("security_status") != SecurityStatus.SAFE:
        return {"intake_complete": False}  # routing never sends unsafe input here

    update: dict[str, Any] = {}
    profile = read(state, "entity_profile", EntityProfile)
    ambiguities = list(state.get("ambiguities") or [])
    if profile is None:
        profile, found, calls, degradations = await _call_llm(state, deps)
        ambiguities += found
        if calls:
            update["llm_calls"] = [*(state.get("llm_calls") or []), *calls]
        if degradations:
            update["degradations"] = [*(state.get("degradations") or []), *degradations]

    profile = apply_answers(profile, read_list(state, "clarification_answers", ClarificationAnswer))
    missing = missing_fields(profile)
    update |= {
        "entity_profile": dump(profile),
        "missing_fields": missing,
        "ambiguities": resolved_ambiguities(ambiguities, profile),
        "intake_complete": not missing,
    }
    return update
