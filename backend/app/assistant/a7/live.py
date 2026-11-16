"""A7 E11: the opt-in live LLM evaluation (docs/assistant/A7_PLAN.md §6). Synthetic data only.

    python -m app.assistant.a7.live --dry-run      (no network: the token estimate only)
    GROQ_LIVE_ENABLED=true python -m app.assistant.a7.live --live   (`make a7-live`)

Inputs: only the committed §2.3 conversation set and the §2.5 PII set. No other text.

Safety (plan §6.3), enforced here, not just documented:
- **Budget.** A dry run of the same inputs with a no-network LLM is made first; the token
  reservation it implies (prompt characters / 3 + each call's output cap, exactly what the
  rate limiter reserves) plus a 20 % allowance for re-asks and retries is printed and
  becomes a hard cap. Every live attempt (re-asks, retries and the fallback model included)
  reserves against it; when the next attempt would exceed it, the run stops.
- **Rate.** The project's free-tier limits (`RATE_LIMIT_GROQ_TPM`, `..._RPM`) in a local
  sliding window; the runner waits for capacity between turns, so the 10 s voice timeout
  measures the provider, not the queue.
- **Errors.** At most 2 retries per call (the provider's existing cap); the run aborts on 3
  consecutive provider errors. No LLM cache (refused if `LLM_CACHE_ENABLED` is set).
- **Capture.** Per call: prompt id, sizes, a hash of the prompt, the delimited fact block
  (voice; no customer PII), the parsed output, latency and token usage. Never the API key
  or the raw prompt.

Every accepted voice reply is then re-audited offline by `strict_audit`, which is stricter
than the product's `check_reply`. Anything it flags is listed for manual classification in
the report; a confirmed violation that `check_reply` let through is a defect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.assistant.a7.core import RECORD_DIR, REPORT_DIR, provenance
from app.assistant.a7.datasets import load_conversations, load_pii
from app.assistant.a7.driver import profile_values, run_turns
from app.assistant.a7.probes import CapturingChecklist
from app.assistant.a7.stack import Stack, development_checklist, throwaway_database
from app.assistant.llm_extract import LLMSlotExtraction, accept
from app.assistant.profile import Slot
from app.assistant.voice import DOCUMENT_TERMS, NUMBER, check_reply
from app.config import Settings, get_settings
from app.core.loop import selector_loop_factory
from app.core.ratelimit import estimate_tokens
from app.llm.common import ProviderError, ProviderRateLimited, ProviderUnavailable, text_sha256
from app.llm.multimodal import parse_structured
from app.llm.provider import LLMRequest, LLMResult
from app.security.screen import screen_text

ALLOWANCE = 1.2  # re-asks and retries on top of the dry run's first attempts
MAX_CONSECUTIVE_ERRORS = 3
WINDOW_S = 60.0


# --- budget and rate --------------------------------------------------------------------------


@dataclass
class BudgetLimiter:
    """A `RateLimiter` for the live run: the free-tier window plus a hard token cap."""

    budget: int
    tpm: int
    rpm: int
    max_wait_s: float
    reserved: int = 0
    attempts: int = 0
    aborted: str | None = None
    window: deque[tuple[float, int]] = field(default_factory=deque)

    def _used(self) -> tuple[int, int]:
        now = time.monotonic()
        while self.window and now - self.window[0][0] >= WINDOW_S:
            self.window.popleft()
        return sum(t for _, t in self.window), len(self.window)

    async def settle(self, tokens: int) -> None:
        """Wait (unbounded by max_wait, between turns) until `tokens` would fit."""
        tokens = min(tokens, self.tpm)
        while True:
            used, count = self._used()
            if used + tokens <= self.tpm and count < self.rpm:
                return
            await asyncio.sleep(max(0.2, WINDOW_S - (time.monotonic() - self.window[0][0])))

    async def acquire(self, tokens: int) -> None:
        if self.aborted:
            raise ProviderUnavailable(f"live run stopped: {self.aborted}")
        if self.reserved + tokens > self.budget:
            self.aborted = f"token budget reached ({self.reserved} of {self.budget} reserved)"
            raise ProviderUnavailable(f"live run stopped: {self.aborted}")
        if tokens > self.tpm:
            raise ProviderUnavailable(f"request needs ~{tokens} tokens, above {self.tpm}/min")
        waited = 0.0
        while True:
            used, count = self._used()
            if used + tokens <= self.tpm and count < self.rpm:
                break
            wait = max(0.2, WINDOW_S - (time.monotonic() - self.window[0][0]))
            if waited + wait > self.max_wait_s:
                raise ProviderRateLimited(f"rate-limit capacity not available in {waited:.0f} s")
            await asyncio.sleep(wait)
            waited += wait
        self.reserved += tokens
        self.attempts += 1
        self.window.append((time.monotonic(), tokens))


# --- the recording wrapper --------------------------------------------------------------------


def _between(text: str, start: str, end: str) -> str | None:
    if start not in text:
        return None
    return text.split(start, 1)[1].split(end, 1)[0].strip("\n")


@dataclass
class AuditLLM:
    """Wraps the provider: records every call (never the key or the raw prompt) and counts
    consecutive provider errors for the abort rule."""

    inner: Any
    limiter: BudgetLimiter | None = None
    secrets: tuple[str, ...] = ()
    calls: list[dict[str, Any]] = field(default_factory=list)
    consecutive_errors: int = 0
    leaked_secrets: int = 0

    @property
    def name(self) -> str:
        return str(self.inner.name)

    @property
    def model_id(self) -> str:
        return str(self.inner.model_id)

    async def generate_structured(self, request: LLMRequest, schema: type[Any]) -> LLMResult[Any]:
        prompt = request.system + request.user
        self.leaked_secrets += sum(1 for s in self.secrets if s in prompt)
        rec: dict[str, Any] = {
            "prompt_id": request.prompt_id,
            "repair": "Your previous answer did not match" in request.user,
            "prompt_chars": len(prompt),
            "prompt_sha256": text_sha256(prompt)[:16],
            "reserve_tokens": estimate_tokens(
                request.system, request.user, max_output_tokens=request.max_output_tokens
            ),
        }
        message = ""
        open_slots: tuple[Slot, ...] = ()
        if request.prompt_id == "assistant_voice":
            rec["facts"] = _between(request.user, "<facts>\n", "\n</facts>")
        else:  # kept only to filter the proposals below; never recorded
            message = _between(request.user, "<customer_message>\n", "\n</customer_message>") or ""
            found = re.search(r"Slots still unknown: ([^\n]*)", request.user)
            names = [s.strip() for s in found.group(1).split(",")] if found else []
            open_slots = tuple(Slot(s) for s in names if s in Slot._value2member_map_)
            rec["open_slots"] = [s.value for s in open_slots]
        self.calls.append(rec)
        started = time.perf_counter()
        try:
            result = await self.inner.generate_structured(request, schema)
        except ProviderError as exc:
            rec.update(outcome="provider_error", error=type(exc).__name__,
                       latency_ms=round((time.perf_counter() - started) * 1000))  # fmt: skip
            self.consecutive_errors += 1
            if self.limiter is not None and self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.limiter.aborted = self.limiter.aborted or (
                    f"{MAX_CONSECUTIVE_ERRORS} consecutive provider errors"
                )
            raise
        except BaseException as exc:  # the caller's timeout cancels the call
            rec.update(outcome="cancelled" if isinstance(exc, asyncio.CancelledError)
                       else type(exc).__name__,
                       latency_ms=round((time.perf_counter() - started) * 1000))  # fmt: skip
            raise
        self.consecutive_errors = 0
        usage = result.usage
        rec.update(
            outcome="valid" if result.parsed is not None else "invalid",
            model=result.model_id,
            latency_ms=round((time.perf_counter() - started) * 1000),
            provider_latency_ms=result.latency_ms,
            attempts=result.attempts,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            finish=result.finish_reason,
        )
        if result.parsed is not None:
            rec["output"] = result.parsed.model_dump(mode="json")
            if isinstance(result.parsed, LLMSlotExtraction):  # what the product would keep
                rec["hints"] = [
                    [h.slot.value, h.value] for h in accept(result.parsed, message, open_slots)
                ]
        return result  # type: ignore[no-any-return]


@dataclass
class DryLLM:
    """No network. Extraction proposes nothing; the voice reply always fails `check_reply`
    (a URL), so the deterministic reply is kept and no re-ask happens: the dry run follows
    exactly the deterministic conversation."""

    name: str = "a7-dry"
    model_id: str = "a7-dry-0"

    async def generate_structured(self, request: LLMRequest, schema: type[Any]) -> LLMResult[Any]:
        raw = (
            '{"mentions": []}'
            if schema is LLMSlotExtraction
            else '{"reply": "see https://example.invalid"}'
        )
        parsed, errors = parse_structured(raw, schema)
        return LLMResult[schema](  # type: ignore[valid-type]
            parsed=parsed, raw_text_sha256=text_sha256(raw), validation_errors=errors,
            provider=self.name, model_id=self.model_id, latency_ms=0, finish_reason="stop",
        )  # fmt: skip


# --- the stricter offline audit ---------------------------------------------------------------

OBLIGATION = re.compile(
    r"\b(mandatory|compulsor\w*|required?|requires|requirement\w*|must|need(?:s|ed)?|"
    r"have to|has to|should|necessary|essential|obligator\w*|approv\w*|reject\w*|eligib\w*|"
    r"guarantee\w*|qualif\w*|accepted|will be opened|promise\w*)\b",
    re.IGNORECASE,
)
EXTRA_DOCUMENT_TERMS = (
    "birth certificate", "driving", "driver", "electricity", "telephone", "lease", "rent",
    "pension", "employer", "payslip", "pay slip", "form 16", "board resolution", "incorporation",
    "registration", "llp agreement", "bye-laws", "by-laws", "affidavit", "declaration",
    "stamp", "seal", "specimen", "fatca", "crs", "w-8", "w-9", "tin", "ssn", "residence permit",
    "work permit", "overseas address", "foreign address", "remittance", "nre", "nro", "fcnr",
    "rfc", "minor", "joint", "deposit", "cheque book", "debit card", "net banking", "fee",
    "charge", "interest", "rate", "balance", "branch", "website", "online", "video kyc",
)  # fmt: skip
WORDS = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    ["a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "your", "you",
     "by", "as", "is", "are", "be", "at", "from", "this", "that", "it", "if", "any", "its",
     "their", "per"]
)  # fmt: skip


def _content_words(text: str) -> set[str]:
    return {w for w in WORDS.findall(text.lower()) if w not in STOP and len(w) > 2}


def strict_audit(reply: str, fact_text: str) -> list[str]:
    """Flags (empty = clean). Stricter than `check_reply`: wider obligation and document
    vocabularies, every bullet must correspond to a fact, any number including "one" of a
    count, and the reply is screened for identifiers."""
    flags: list[str] = []
    lowered_facts = fact_text.lower()
    reason = check_reply(reply, fact_text)
    if reason is not None:
        flags.append(f"check_reply: {reason}")
    for m in OBLIGATION.finditer(reply):
        if m.group(0).lower() not in lowered_facts:
            flags.append(f"obligation/approval wording: {m.group(0)!r}")
    lowered = reply.lower()
    for term in (*DOCUMENT_TERMS, *EXTRA_DOCUMENT_TERMS):
        if re.search(rf"\b{re.escape(term)}\b", lowered) and term not in lowered_facts:
            flags.append(f"term not in the facts: {term!r}")
    fact_numbers = set(NUMBER.findall(fact_text))
    for n in NUMBER.findall(reply):
        if n not in fact_numbers:
            flags.append(f"number not in the facts: {n}")
    fact_words = _content_words(fact_text)
    for line in reply.splitlines():
        s = line.strip()
        if s[:1] in "-•*" and len(s) > 2:
            words = _content_words(s[1:])
            if words and len(words & fact_words) / len(words) < 0.6:
                flags.append(f"bullet not traceable to the facts: {s[:80]!r}")
    if screen_text(reply).sensitive:
        flags.append("identifier pattern in the reply")
    return flags


# --- running ----------------------------------------------------------------------------------


@dataclass
class TurnRecord:
    conversation: str
    category: str
    index: int
    input: str
    kind: str | None
    reply: str | None
    profile: dict[str, str | None]
    confirmation: dict[str, Any] | None
    latency_ms: float
    calls: list[dict[str, Any]]
    failures: list[str]


async def _drive(
    stack: Stack,
    llm_factory: Any,
    limiter: BudgetLimiter | None,
    turn_reserve: int,
    pii: list[Any],
    tracer: Any = None,
) -> tuple[list[TurnRecord], list[dict[str, Any]], str | None]:
    """All conversations, then the PII set. Returns the turns, the PII outcomes and why the
    run stopped early (None = completed)."""
    records: list[TurnRecord] = []
    convs = load_conversations().conversations
    for conv in convs:
        llm = llm_factory()
        marker = 0
        last = time.perf_counter()
        pending: list[tuple[int, float, list[dict[str, Any]]]] = []

        async def on_turn(
            i: int, _svc: Any, _sid: str, llm: Any = llm, pending: list[Any] = pending
        ) -> None:
            nonlocal marker, last
            now = time.perf_counter()
            pending.append((i, round((now - last) * 1000, 1), llm.calls[marker:]))
            marker = len(llm.calls)
            if limiter is not None:
                if limiter.aborted:
                    raise _Stop(limiter.aborted)
                await limiter.settle(turn_reserve)
            last = time.perf_counter()

        stopped: str | None = None
        try:
            run = await run_turns(
                stack.service(llm, tracer=tracer),
                conv.turns,
                restart=lambda llm=llm: stack.service(llm, tracer=tracer),  # type: ignore[misc]
                on_turn=on_turn,
            )
            results = run.turns
        except _Stop as stop:
            stopped, results = str(stop), []
        by_index = {t.index: t for t in results}
        for i, latency, calls in pending:
            t = by_index.get(i)
            turn = conv.turns[i - 1]
            ans = t.answer if t is not None else {}
            records.append(
                TurnRecord(
                    conv.id, conv.category, i,
                    "[reload]" if turn.reload else "[restart]" if turn.restart
                    else json.dumps(turn.body(), ensure_ascii=False),
                    ans.get("kind"), ans.get("reply"), profile_values(ans) if ans else {},
                    ans.get("confirmation"), latency, calls, t.failures if t else [],
                )
            )  # fmt: skip
        if stopped:
            return records, [], stopped
    outcomes: list[dict[str, Any]] = []
    llm = llm_factory()
    svc = stack.service(llm, tracer=tracer)
    for r in pii:
        if limiter is not None:
            if limiter.aborted:
                return records, outcomes, limiter.aborted
            await limiter.settle(turn_reserve)
        before = len(llm.calls)
        sid = (await svc.create_session())["session_id"]
        out = await svc.submit_turn(
            sid, uuid.uuid4().hex, json.dumps({"message": r.message}).encode()
        )
        ans = out["answer"]
        outcomes.append({
            "id": r.id, "expect": r.expect, "kind": ans.get("kind"),
            "llm_calls": len(llm.calls) - before,
            "secret_in_reply": bool(r.secret and r.secret in json.dumps(ans)),
        })  # fmt: skip
    return records, outcomes, None


class _Stop(Exception):
    pass


def _voice_outcomes(records: list[TurnRecord]) -> list[dict[str, Any]]:
    """One entry per voice invocation (a first call plus its re-ask, if any)."""
    out = []
    for t in records:
        voice = [c for c in t.calls if c["prompt_id"] == "assistant_voice"]
        if not voice:
            continue
        last = voice[-1]
        reply = (last.get("output") or {}).get("reply")
        facts = last.get("facts") or ""
        if last.get("outcome") == "valid" and reply is not None:
            reason = check_reply(reply, facts)
            accepted = reason is None
            fallback = None if accepted else f"rejected: {reason}"
        else:
            accepted = False
            fallback = last.get("error") or last.get("outcome")
        out.append({
            "conversation": t.conversation, "turn": t.index, "accepted": accepted,
            "fallback": fallback, "reply": reply if accepted else None, "facts": facts,
            "final_reply": t.reply,
            "consistent": (not accepted) or t.reply == (reply or "").strip(),
            "calls": len(voice),
        })  # fmt: skip
    return out


def _extraction(records: list[TurnRecord]) -> list[dict[str, Any]]:
    """Every accepted LLM proposal, and what happened to it on that turn."""
    out = []
    for t in records:
        for c in t.calls:
            if c["prompt_id"] != "assistant_slot_extraction" or c.get("outcome") != "valid":
                continue
            conf = t.confirmation or {}
            for slot, value in c.get("hints", []):
                out.append({
                    "conversation": t.conversation, "turn": t.index, "input": t.input,
                    "slot": slot, "value": value,
                    "confirmed_first": (conf.get("slot"), conf.get("value")) == (slot, value),
                    "in_profile_after_turn": t.profile.get(slot) == value,
                })  # fmt: skip
    return out


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(p * len(s)) - 1)]


async def _dry(stack: Stack, pii: list[Any]) -> dict[str, Any]:
    llm_holder: list[AuditLLM] = []

    def factory() -> AuditLLM:
        a = AuditLLM(DryLLM())
        llm_holder.append(a)
        return a

    records, _, _ = await _drive(stack, factory, None, 0, pii)
    calls = [c for a in llm_holder for c in a.calls]
    per_turn = [sum(c["reserve_tokens"] for c in t.calls) for t in records]
    first = sum(c["reserve_tokens"] for c in calls)
    return {
        "turns": len(records), "calls": len(calls),
        "voice_calls": sum(c["prompt_id"] == "assistant_voice" for c in calls),
        "extraction_calls": sum(c["prompt_id"] == "assistant_slot_extraction" for c in calls),
        "first_attempt_tokens": first, "budget_tokens": math.ceil(first * ALLOWANCE),
        "max_turn_tokens": max(per_turn or [0]), "max_call_tokens": max(
            (c["reserve_tokens"] for c in calls), default=0),
    }  # fmt: skip


def _live_settings(dev: Settings) -> Settings:
    if dev.LLM_CACHE_ENABLED:
        raise SystemExit("refused: LLM_CACHE_ENABLED must be off for the live run")
    if not dev.GROQ_LIVE_ENABLED:
        raise SystemExit("refused: start the live run with GROQ_LIVE_ENABLED=true")
    return dev


async def _main(
    args: argparse.Namespace, url: str, checklist: CapturingChecklist, dev: Settings
) -> int:
    stack = Stack(url, checklist)
    pii = load_pii()
    try:
        dry = await _dry(stack, pii)
        minutes = dry["budget_tokens"] / dev.RATE_LIMIT_GROQ_TPM
        print(
            f"dry run (no network): {dry['turns']} conversation turns + {len(pii)} PII messages, "
            f"{dry['calls']} LLM calls ({dry['voice_calls']} voice, "
            f"{dry['extraction_calls']} extraction); reservation {dry['first_attempt_tokens']} "
            f"tokens; BUDGET {dry['budget_tokens']} tokens (x{ALLOWANCE}); at "
            f"{dev.RATE_LIMIT_GROQ_TPM} TPM at least {minutes:.0f} min",
            flush=True,
        )
        if args.dry_run:
            return 0
        settings = _live_settings(dev)
        from app.assistant.a7.suites_observability import Capture
        from app.assistant.tracing import Tracer
        from app.llm.groq import llm_from_settings

        limiter = BudgetLimiter(
            budget=dry["budget_tokens"], tpm=settings.RATE_LIMIT_GROQ_TPM,
            rpm=settings.RATE_LIMIT_GROQ_RPM, max_wait_s=settings.RATE_LIMIT_MAX_WAIT_S,
        )  # fmt: skip
        provider = llm_from_settings(settings, limiter=limiter)
        secrets = tuple(r.secret for r in pii if r.secret)
        audits: list[AuditLLM] = []

        def factory() -> AuditLLM:
            a = AuditLLM(provider, limiter, secrets)
            if audits:
                a.consecutive_errors = audits[-1].consecutive_errors  # the rule spans the run
            audits.append(a)
            return a

        capture = Capture()  # local tracing only: nothing is uploaded (no LangSmith key)
        tracer = Tracer(capture, settings.ASSISTANT_TRACE_EVAL_PROJECT)
        started = time.perf_counter()
        records, pii_out, stopped = await _drive(
            stack, factory, limiter, min(dry["max_turn_tokens"], settings.RATE_LIMIT_GROQ_TPM),
            pii, tracer,
        )  # fmt: skip
        elapsed = time.perf_counter() - started
        _report(dev, dry, limiter, audits, records, pii_out, stopped, elapsed, capture)
        return 0
    finally:
        await stack.close()


def _report(
    dev: Settings, dry: dict[str, Any], limiter: BudgetLimiter, audits: list[AuditLLM],
    records: list[TurnRecord], pii_out: list[dict[str, Any]], stopped: str | None,
    elapsed: float, capture: Any,
) -> None:  # fmt: skip
    calls = [c for a in audits for c in a.calls]
    voice = _voice_outcomes(records)
    accepted = [v for v in voice if v["accepted"]]
    received = [v for v in voice if v["accepted"] or str(v["fallback"]).startswith("rejected")]
    audit = [
        {"conversation": v["conversation"], "turn": v["turn"], "reply": v["reply"],
         "flags": strict_audit(v["reply"], v["facts"])}
        for v in accepted
    ]  # fmt: skip
    flagged = [a for a in audit if a["flags"]]
    extraction = _extraction(records)
    unconfirmed = [e for e in extraction if e["in_profile_after_turn"] and not e["confirmed_first"]]
    voice_turn_latency = [
        t.latency_ms for t in records if any(c["prompt_id"] == "assistant_voice" for c in t.calls)
    ]
    reasons: dict[str, int] = {}
    for v in voice:
        if not v["accepted"]:
            key = str(v["fallback"])
            reasons[key] = reasons.get(key, 0) + 1
    in_tok = sum(c.get("input_tokens") or 0 for c in calls)
    out_tok = sum(c.get("output_tokens") or 0 for c in calls)
    models = sorted({str(c.get("model")) for c in calls if c.get("model")})
    pii_blocked = [p for p in pii_out if p["kind"] == "blocked"]
    leaked = sum(a.leaked_secrets for a in audits)
    spans: dict[str, int] = {}
    for r in capture.created:
        spans[r["name"]] = spans.get(r["name"], 0) + 1

    def usage_of(u: dict[str, Any]) -> Any:
        return ((u.get("outputs") or {}).get("usage_metadata") or {}).get("input_tokens")

    llm_usage = [u for u in capture.updated if u["name"] == "llm" and usage_of(u)]
    echoed = sum(p["secret_in_reply"] for p in pii_out)
    validity = len(accepted) / len(received) if received else 0.0
    record: dict[str, Any] = {
        "provenance": provenance(), "models": models, "dry": dry, "stopped": stopped,
        "reserved_tokens": limiter.reserved, "attempts": limiter.attempts,
        "usage": {"input": in_tok, "output": out_tok}, "elapsed_s": round(elapsed, 1),
        "voice": voice, "audit": audit, "extraction": extraction, "pii": pii_out,
        "turns": [t.__dict__ for t in records], "fallback_reasons": reasons,
        "spans": spans,
    }  # fmt: skip
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    (RECORD_DIR / "live.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n", "utf-8"
    )
    p = record["provenance"]
    turn_acc = [t for t in records if t.failures is not None]
    checked_fail = sum(1 for t in turn_acc if t.failures)
    md = (
        "# A7: live LLM evaluation (E11)\n\n"
        f"Run with `make a7-live` on {p['date']} at commit `{p['commit']}`"
        f"{' (with uncommitted changes)' if p['dirty'] else ''}; coverage "
        f"`{p['coverage_version']}`, catalogue `{p['catalogue_version']}`. Development stack, "
        "throwaway conversation database. **Live Groq, opt-in by the owner (2026-11-16); "
        "synthetic data only** (the committed §2.3 conversations and §2.5 PII set). "
        f"Models: {', '.join(models) or 'none answered'}. No LLM cache. Local tracing only "
        "(no LangSmith upload: no key configured).\n\n"
        "## Budget and run\n\n"
        "| Item | Value |\n|---|---|\n"
        f"| Dry run (no network) | {dry['turns']} turns + {len(load_pii())} PII messages, "
        f"{dry['calls']} first-attempt calls |\n"
        f"| Budget (dry reservation x{ALLOWANCE}), printed before the live run | "
        f"{dry['budget_tokens']} tokens |\n"
        f"| Reserved by the live run (every attempt) | {limiter.reserved} tokens, "
        f"{limiter.attempts} attempts |\n"
        f"| Provider-reported usage | {in_tok} input + {out_tok} output tokens |\n"
        f"| Rate limits | {dev.RATE_LIMIT_GROQ_TPM} TPM, {dev.RATE_LIMIT_GROQ_RPM} RPM; "
        "10 s voice timeout; at most 2 retries |\n"
        f"| Completed | {'yes' if stopped is None else 'NO, stopped: ' + stopped} |\n"
        f"| Turns run | {len(records)} conversation turns, {len(pii_out)} PII messages |\n"
        f"| Wall time | {elapsed / 60:.1f} min |\n\n"
        "## Metrics (A7_PLAN §3)\n\n"
        "| Metric | Result | Threshold |\n|---|---|---|\n"
        f"| Live voice validity (accepted by `check_reply` / replies received) | "
        f"{len(accepted)}/{len(received)} = {validity:.0%} | >= 80 % (soft) |\n"
        f"| Voice fallback rate (deterministic reply used / voice invocations) | "
        f"{len(voice) - len(accepted)}/{len(voice)} | reported |\n"
        f"| Accepted replies flagged by the stricter offline audit | {len(flagged)} of "
        f"{len(accepted)} (classified below) | 0 confirmed violations (hard) |\n"
        f"| Replies consistent with the stored answer | "
        f"{sum(v['consistent'] for v in voice)}/{len(voice)} | all |\n"
        f"| Turn time with the live voice, p50 / p95 / max | "
        f"{_pct(voice_turn_latency, 0.5) / 1000:.1f} / {_pct(voice_turn_latency, 0.95) / 1000:.1f}"
        f" / {max(voice_turn_latency or [0]) / 1000:.1f} s | p95 <= 12 s |\n"
        f"| LLM proposals accepted by the extraction filter | {len(extraction)} | reported |\n"
        f"| Proposals committed without a confirmation | {len(unconfirmed)} | 0 (hard) |\n"
        f"| Synthetic identifiers in any LLM request | {leaked} | 0 (hard) |\n"
        f"| PII messages blocked (expected {sum(p['expect'] == 'block' for p in pii_out)}) / "
        f"with an LLM call | {len(pii_blocked)} / "
        f"{sum(1 for p in pii_blocked if p['llm_calls'])} | blocked: 0 calls |\n"
        f"| Identifier echoed in a PII answer | {echoed} | 0 |\n"
        f"| Scripted expectations missed with the live LLM (not a gate) | {checked_fail} of "
        f"{len(records)} turns | reported |\n\n"
        "### Fallback reasons (voice)\n\n"
        + "".join(f"- {k}: {v}\n" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        + "\n### Local tracing (capturing client; nothing uploaded)\n\n"
        + f"- spans: {', '.join(f'{k} {v}' for k, v in sorted(spans.items()))}\n"
        + f"- `llm` spans carrying provider token counts: {len(llm_usage)}\n"
    )
    (REPORT_DIR / "live_llm_report.md").write_text(md, encoding="utf-8", newline="\n")
    print(md)


RECHECK_MARK = "\n## Offline re-check with the current `check_reply`\n"


def recheck() -> int:
    """No network: re-apply the CURRENT `check_reply` to every reply the live run received
    (data/processed/a7/live.json) and re-audit what it still accepts. Used after a validator
    fix, so the live findings are measured against the fixed check without a second run."""
    record = json.loads((RECORD_DIR / "live.json").read_text("utf-8"))
    received = [
        v for v in record["voice"]
        if v["accepted"] or str(v["fallback"]).startswith("rejected")
    ]  # fmt: skip
    replies = {(t["conversation"], t["index"]): t for t in record["turns"]}
    kept: list[dict[str, Any]] = []
    for v in received:
        turn = replies.get((v["conversation"], v["turn"]))
        calls = [c for c in (turn or {}).get("calls", []) if c["prompt_id"] == "assistant_voice"]
        reply = ((calls[-1].get("output") if calls else None) or {}).get("reply")
        if reply is not None and check_reply(reply, v["facts"]) is None:
            kept.append({**v, "reply": reply, "flags": strict_audit(reply, v["facts"])})
    flagged = [k for k in kept if k["flags"]]
    section = (
        RECHECK_MARK
        + "\nThe validator was fixed after this run (A7.6: obligation wording such as \"you'll "
        'need" / "requirement", and profile words not in the facts; owner decision '
        "2026-11-16). The recorded replies were re-checked offline with the fixed check; no "
        "second live run was made (budget).\n\n"
        "| Metric | Result | Threshold |\n|---|---|---|\n"
        f"| Voice validity with the fixed check | {len(kept)}/{len(received)} = "
        f"{len(kept) / max(len(received), 1):.0%} | >= 80 % (soft) |\n"
        f"| Accepted replies still flagged by the stricter audit | {len(flagged)} | "
        "classified below |\n\n"
        + "".join(
            f"- `{k['conversation']}#{k['turn']}`: {'; '.join(k['flags'])}\n" for k in flagged
        )
    )
    path = REPORT_DIR / "live_llm_report.md"
    md = path.read_text("utf-8").split(RECHECK_MARK)[0].rstrip("\n") + "\n"
    path.write_text(md + section, encoding="utf-8", newline="\n")
    print(section)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="no network: the estimate only")
    mode.add_argument("--live", action="store_true", help="the opt-in live run")
    mode.add_argument("--recheck", action="store_true", help="no network: fixed-check re-audit")
    args = parser.parse_args(argv)
    if args.recheck:
        return recheck()
    dev = get_settings()
    checklist = CapturingChecklist(development_checklist(dev))
    with (
        throwaway_database("kyc_a7_live") as url,
        asyncio.Runner(loop_factory=selector_loop_factory) as runner,
    ):
        return runner.run(_main(args, url, checklist, dev))


if __name__ == "__main__":
    sys.exit(main())
