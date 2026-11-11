"""`compliance_synthesis` node (M3 S5; M3_PLAN §3, §8).

An async node: one structured LLM call through the injected `LLMProvider`
(`generate_validated` = one call plus at most one re-ask), under the node time limit.
The model sees only profile values, delimited evidence, bounded form labels and gap codes
(`app.graph.synthesis`), and returns a `ChecklistDraft`: a PROPOSAL. Nothing here is
trusted; `output_validation` decides what survives.

If the LLM is missing, times out, fails or returns invalid output twice, the draft stays
None and a degradation is recorded (outcome DEGRADED, with the fallback message). Nothing
fills the gap.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from app.graph.state import (
    DataClass,
    Degradation,
    EntityProfile,
    EvidenceItem,
    FormRef,
    KYCState,
    LLMCallRecord,
    RetrievalMeta,
    ValidationFinding,
    dump,
    read,
    read_list,
)
from app.graph.synthesis import ChecklistDraft, synthesis_request
from app.llm.cache import synthetic_input_digest
from app.llm.common import ProviderError
from app.llm.provider import generate_validated

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps

NODE = "compliance_synthesis"


def _degraded(state: KYCState, reason: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    update: dict[str, Any] = {
        "draft": None,
        "degradations": [
            *(state.get("degradations") or []),
            dump(Degradation(component="llm", reason=reason)),
        ],
    }
    if records:
        update["llm_calls"] = [*(state.get("llm_calls") or []), *records]
    return update


async def compliance_synthesis(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    profile = read(state, "entity_profile", EntityProfile) or EntityProfile()
    evidence = read_list(state, "evidence", EvidenceItem)
    forms = read_list(state, "forms", FormRef)
    meta = read(state, "retrieval_meta", RetrievalMeta)
    if deps.llm is None:
        return _degraded(state, "not_configured", [])

    digest = None
    if state.get("data_class") == DataClass.SYNTHETIC_DEMO:
        candidate = synthetic_input_digest(state.get("query", ""))
        digest = candidate if candidate in deps.synthetic_allowlist else None
    request, omitted = synthesis_request(
        profile, evidence, forms, meta.gaps if meta else [], synthetic_input_digest=digest
    )
    attempts = (state.get("synthesis_attempts") or 0) + 1
    try:
        async with asyncio.timeout(deps.node_timeout_s):
            call = await generate_validated(deps.llm, request, ChecklistDraft)
    except (ProviderError, TimeoutError) as exc:
        record = LLMCallRecord(node=NODE, provider=deps.llm.name, model_id=deps.llm.model_id,
                               latency_ms=0, attempts=0, result="error")  # fmt: skip
        reason = "node_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        return _degraded(state, reason, [dump(record)]) | {"synthesis_attempts": attempts}

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
        return _degraded(state, "invalid_output_after_repair", records) | {
            "synthesis_attempts": attempts
        }
    update: dict[str, Any] = {
        "draft": call.result.parsed.model_dump(mode="json"),
        "synthesis_attempts": attempts,
        "llm_calls": [*(state.get("llm_calls") or []), *records],
    }
    if omitted:
        finding = ValidationFinding(
            kind="evidence_omitted_from_prompt",
            detail=f"{omitted} lower-ranked passage(s) left out to fit the token budget",
        )
        update["validation_findings"] = [*(state.get("validation_findings") or []), dump(finding)]
    return update
