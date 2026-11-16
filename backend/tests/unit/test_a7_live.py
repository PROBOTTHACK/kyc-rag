"""A7 E11 live-run guards, without network: the hard token cap, the abort on consecutive
provider errors, the recording wrapper (no key, no raw prompt) and the stricter audit."""

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from app.assistant.a7.live import (
    MAX_CONSECUTIVE_ERRORS,
    AuditLLM,
    BudgetLimiter,
    DryLLM,
    strict_audit,
)
from app.assistant.llm_extract import LLMSlotExtraction
from app.assistant.voice import VoiceReply, check_reply
from app.llm.common import ProviderUnavailable
from app.llm.provider import LLMRequest


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def limiter(budget: int = 1000) -> BudgetLimiter:
    return BudgetLimiter(budget=budget, tpm=6000, rpm=20, max_wait_s=1.0)


def test_the_budget_is_a_hard_cap_and_stops_every_later_call() -> None:
    lim = limiter(1000)
    run(lim.acquire(600))
    with pytest.raises(ProviderUnavailable):
        run(lim.acquire(500))  # 1100 > 1000: refused, nothing reserved
    assert lim.reserved == 600 and lim.aborted
    with pytest.raises(ProviderUnavailable):
        run(lim.acquire(1))  # stopped for good, even though 1 token would fit
    assert lim.reserved == 600 and lim.attempts == 1


def request(prompt_id: str = "assistant_voice", user: str = "<facts>\n{}\n</facts>") -> LLMRequest:
    return LLMRequest(prompt_id=prompt_id, prompt_version="v", system="S", user=user,
                      max_output_tokens=10)  # fmt: skip


@dataclass
class Failing:
    name: str = "f"
    model_id: str = "f-0"

    async def generate_structured(self, request: LLMRequest, schema: type[Any]) -> Any:
        raise ProviderUnavailable("down")


def test_consecutive_provider_errors_abort_the_run() -> None:
    lim = limiter(10_000)
    llm = AuditLLM(Failing(), lim)
    for _ in range(MAX_CONSECUTIVE_ERRORS):
        assert lim.aborted is None
        with pytest.raises(ProviderUnavailable):
            run(llm.generate_structured(request(), VoiceReply))
    assert lim.aborted == f"{MAX_CONSECUTIVE_ERRORS} consecutive provider errors"


def test_the_recording_never_keeps_the_prompt_and_counts_identifiers() -> None:
    llm = AuditLLM(DryLLM(), secrets=("ABCPE1234F",))
    user = (
        "<customer_message>\nmy pan ABCPE1234F\n</customer_message>\nSlots still unknown: holding"
    )
    run(llm.generate_structured(request("assistant_slot_extraction", user), LLMSlotExtraction))
    rec = llm.calls[0]
    assert llm.leaked_secrets == 1  # the probe sees it; the PII suite proves this stays 0
    assert "ABCPE1234F" not in str(rec) and len(rec["prompt_sha256"]) == 16
    assert rec["open_slots"] == ["holding"] and rec["outcome"] == "valid"
    assert rec["hints"] == []


def test_the_dry_voice_reply_is_always_rejected_so_the_run_stays_deterministic() -> None:
    llm = AuditLLM(DryLLM())
    run(llm.generate_structured(request(), VoiceReply))
    assert check_reply(llm.calls[0]["output"]["reply"], "{}") is not None


FACTS = '{"checklist":{"documents_to_bring":[{"title":"Passport copy"}],"documents_count":1}}'


def test_the_strict_audit_is_stricter_than_check_reply() -> None:
    clean = "Here is your checklist with 1 document:\n- Passport copy"
    assert strict_audit(clean, FACTS) == []
    for reply, flag in (
        ("You should bring it.", "obligation"),
        ("Also bring a rent agreement.", "rent"),
    ):
        assert check_reply(reply, FACTS) is None  # the product check lets these through
        assert any(flag in f for f in strict_audit(reply, FACTS)), reply
    for reply, flag in (
        ("You need a passport copy.", "obligation"),  # caught by both since A7.6
        ("- Electricity bill from last month", "bullet"),
    ):
        assert check_reply(reply, FACTS) is not None
        assert any(flag in f for f in strict_audit(reply, FACTS)), reply
