"""A7 LangSmith observability suite (owner requirement at the A7 approval).

Traces the scripted conversations and the PII set on the real stack into a capturing client
(the LangSmith run payloads exactly as they would be uploaded) under the evaluation project,
and checks: one trace per turn, the nested spans, latency and token-usage fields, nothing
sensitive uploaded (identifiers, raw contacts, prompts, stored passages), the cost of
tracing, and that a failing LangSmith never changes or breaks a turn. A live upload to
LangSmith is attempted only when `LANGSMITH_API_KEY` is configured.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.assistant.a7.core import Context, header, suite, write_report
from app.assistant.a7.datasets import load_conversations, load_pii
from app.assistant.a7.driver import run_turns
from app.assistant.a7.probes import RecordingLLM
from app.assistant.tracing import Tracer, tracer_from_settings

REQUIRED = {"history_retrieval", "context_assembly", "intent_resolution"}


class Capture:
    def __init__(self, fail: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.fail = fail

    def create_run(self, **kw: Any) -> None:
        if self.fail:
            raise ConnectionError("LangSmith unreachable")
        self.created.append(kw)

    def update_run(self, run_id: Any, **kw: Any) -> None:
        if self.fail:
            raise ConnectionError("LangSmith unreachable")
        self.updated.append({"id": run_id, **kw})


@suite("observability")
async def observability(ctx: Context) -> dict[str, Any]:
    project = ctx.dev_settings.ASSISTANT_TRACE_EVAL_PROJECT
    cap = Capture()
    tracer = Tracer(cap, project)
    convs = load_conversations().conversations
    turns = 0
    started = time.perf_counter()
    for c in convs:
        llm = RecordingLLM()
        run = await run_turns(
            ctx.stack.service(llm, tracer=tracer),
            c.turns,
            restart=lambda llm=llm: ctx.stack.service(llm, tracer=tracer),  # type: ignore[misc]
        )
        turns += sum(1 for t in c.turns if not (t.reload or t.restart))
        del run
    traced_s = time.perf_counter() - started
    # The PII set through the traced service.
    pii = load_pii()
    svc = ctx.stack.service(RecordingLLM(), tracer=tracer)
    for r in pii:
        sid = (await svc.create_session())["session_id"]
        await svc.submit_turn(sid, uuid.uuid4().hex, json.dumps({"message": r.message}).encode())
        turns += 1
    runs = {run_["id"]: run_ for run_ in cap.created}
    roots = [run_ for run_ in runs.values() if run_.get("parent_run_id") is None]
    names: dict[str, int] = {}
    for run_ in runs.values():
        names[run_["name"]] = names.get(run_["name"], 0) + 1
    children: dict[Any, set[str]] = {}
    for run_ in runs.values():
        if run_.get("parent_run_id") is not None:
            children.setdefault(run_["parent_run_id"], set()).add(run_["name"])
    # Every turn, blocked ones included, goes through history, context and the dialogue (the
    # dialogue produces the refusal), so every root has the core spans directly below it.
    # A blocked turn differs in what is traced: its refused message never is.
    with_core = sum(1 for root in roots if children.get(root["id"], set()) >= REQUIRED)
    blocked_ids = {
        u["id"]
        for u in cap.updated
        if u["name"] == "assistant.turn"
        and (u.get("outputs") or {}).get("answer_kind") == "blocked"
    }
    blocked_roots = len(blocked_ids)
    blocked_message_traced = sum(
        1
        for root in roots
        if root["id"] in blocked_ids and (root.get("inputs") or {}).get("message") is not None
    )
    closed = len(cap.updated) == len(cap.created)
    latency = all(
        "latency_ms" in ((u.get("extra") or {}).get("metadata") or {}) for u in cap.updated
    )
    llm_usage = all(
        "usage_metadata" in (u.get("outputs") or {}) for u in cap.updated if u["name"] == "llm"
    )
    uploaded = json.dumps([cap.created, cap.updated], default=str)
    secrets = [r.secret for r in pii if r.secret]
    leaked = [s for s in secrets if s in uploaded]
    passages = {
        c.passage[:80]
        for _, a in ctx.checklist.built
        if a.groups is not None
        for i in (*a.groups.documents, *a.groups.information)
        for c in i.citations
        if len(c.passage) >= 80
    }
    passage_leaks = [p for p in passages if p in uploaded]
    projects = {r.get("session_name") for r in roots}
    # Cost of tracing, and a failing LangSmith: the same answers, no error.
    sample = next(c for c in convs if c.id == "sep-01")
    t0 = time.perf_counter()
    plain = await run_turns(ctx.stack.service(), sample.turns)
    t1 = time.perf_counter()
    traced = await run_turns(ctx.stack.service(tracer=Tracer(Capture(), project)), sample.turns)
    t2 = time.perf_counter()
    broken = await run_turns(
        ctx.stack.service(tracer=Tracer(Capture(fail=True), project)), sample.turns
    )

    def norm(run: Any) -> list[str]:
        return [json.dumps(t.answer, sort_keys=True) for t in run.turns]

    resilient = norm(plain) == norm(traced) == norm(broken)
    overhead_ms = ((t2 - t1) - (t1 - t0)) * 1000 / max(len(sample.turns), 1)
    live = await _live_upload(ctx)
    passed = (
        len(roots) == turns
        and with_core == len(roots)
        and blocked_roots > 0
        and blocked_message_traced == 0
        and closed
        and latency
        and llm_usage
        and not leaked
        and not passage_leaks
        and projects == {project}
        and resilient
    )
    md = header("LangSmith observability", ctx)
    md += (
        f"All 70 scripted conversations and the {len(pii)} PII messages traced on the real stack "
        f"into a capturing client (the exact LangSmith run payloads), project `{project}`.\n\n"
        "| Check | Result |\n|---|---|\n"
        f"| One `assistant.turn` trace per turn | {len(roots)} traces for {turns} turns |\n"
        "| Core spans under every turn, blocked ones included (history_retrieval, "
        f"context_assembly, intent_resolution) | {with_core}/{len(roots)} |\n"
        f"| Blocked turns whose refused message was traced | {blocked_message_traced} "
        f"of {blocked_roots} |\n"
        f"| Spans recorded | {', '.join(f'{k} {v}' for k, v in sorted(names.items()))} |\n"
        f"| Every span closed with its latency | {'yes' if closed and latency else 'NO'} |\n"
        f"| Every `llm` span carries token-usage fields | {'yes' if llm_usage else 'NO'} |\n"
        f"| Synthetic identifiers in the uploads | {len(leaked)} of {len(secrets)} |\n"
        f"| Stored source passages in the uploads | {len(passage_leaks)} of {len(passages)} "
        "checked |\n"
        f"| Project of every trace | {', '.join(sorted(str(p) for p in projects))} |\n"
        "| Answers identical with tracing off, on, and LangSmith failing | "
        f"{'yes' if resilient else 'NO'} |\n"
        f"| Tracing overhead (capturing client) | {overhead_ms:+.1f} ms per turn |\n"
        f"| Live upload to LangSmith | {live} |\n"
        f"\nTraced run time for the 70 conversations: {traced_s:.1f} s.\n"
        "Token usage: the recording LLM reports none, so the usage fields are present and "
        "empty here; with Groq they carry its input/output token counts.\n"
        f"\n- Result: **{'PASS' if passed else 'FAIL'}**.\n"
    )
    write_report("observability", md, {"provenance": ctx.provenance, "spans": names,
                 "leaked": leaked, "live": live, "passed": passed})  # fmt: skip
    return {"passed": passed}


async def _live_upload(ctx: Context) -> str:
    """Send one synthetic conversation to the evaluation project and read it back. Only
    with a configured key; otherwise reported as not run (never faked)."""
    settings = ctx.dev_settings
    key = settings.LANGSMITH_API_KEY.get_secret_value() if settings.LANGSMITH_API_KEY else ""
    if not key:
        return "not run: no LANGSMITH_API_KEY configured (validated with the capturing client)"
    forced = settings.model_copy(update={"ASSISTANT_TRACING": True})
    tracer = tracer_from_settings(forced, evaluation=True)
    if not tracer.enabled:
        return "not run: the LangSmith client could not be created"
    svc = ctx.stack.service(tracer=tracer)
    sid = (await svc.create_session())["session_id"]
    for body in (
        {"message": "Hi"},
        {"action": "choose", "slot": "account_type", "value": "CURRENT"},
        {"action": "choose", "slot": "customer_category", "value": "LLP"},
    ):
        await svc.submit_turn(sid, uuid.uuid4().hex, json.dumps(body).encode())
    try:
        tracer.client.flush()
        runs = list(tracer.client.list_runs(project_name=tracer.project, is_root=True, limit=5))
        return f"uploaded and read back: {len(runs)} recent root runs in `{tracer.project}`"
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        return f"upload attempted; read-back failed: {type(exc).__name__}"
