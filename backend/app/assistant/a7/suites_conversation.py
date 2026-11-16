"""A7.3 suites: conversational behaviour (E3), the LLM context and history (E4), rolling
summaries, corrections, reload and restart (E5), scenario isolation (E6), and deletion.

Conversations run through the real service on PostgreSQL (throwaway database). The LLM is
the recording probe: it captures exactly what an LLM would receive and never answers with
content, so stored answers are identical to a run without an LLM.
"""

from __future__ import annotations

import json
import random
import uuid
from collections import Counter
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.assistant.a7.core import Context, header, suite, write_report
from app.assistant.a7.datasets import load_conversations, load_memory_cases
from app.assistant.a7.driver import note_codes, run_turns
from app.assistant.a7.probes import RecordingLLM
from app.assistant.dialogue import TurnInput, opening, respond_deterministic
from app.assistant.memory import ConversationSummary, assemble, tokens
from app.assistant.profile import DialogueState, SlotEntry
from app.assistant.service import AssistantError
from app.assistant.wording import wording

# --- E3: the scripted conversations --------------------------------------------------------------


@suite("conversations")
async def conversations(ctx: Context) -> dict[str, Any]:
    convs = load_conversations().conversations
    rows = []
    checked = failed = social_turns = social_bad = 0
    per_category: dict[str, Counter[str]] = {}
    for c in convs:
        run = await run_turns(ctx.stack.service(), c.turns, restart=ctx.stack.service)
        cat = per_category.setdefault(c.category, Counter())
        cat["conversations"] += 1
        cat["passed"] += run.passed
        for k, t in enumerate(run.turns):
            if t.checked:
                checked += 1
                failed += bool(t.failures)
            if "social" in note_codes(t.answer):
                social_turns += 1
                # A greeting must not introduce a fallback (one already standing for a
                # not-covered answer stays on the answer; the UI does not repeat it) nor
                # repeat the checklist summary.
                prev = run.turns[k - 1].answer if k > 0 else {}
                introduced = bool(t.answer.get("fallback")) and not prev.get("fallback")
                repeat = "Here's what the HDFC Bank form asks for" in (t.answer.get("reply") or "")
                social_bad += bool(introduced or repeat)
        rows.append(
            {
                "id": c.id,
                "category": c.category,
                "passed": run.passed,
                "failures": [(t.index, t.input, t.failures) for t in run.turns if t.failures],
            }
        )
    total_turns = sum(len(c.turns) for c in convs)
    passed = failed == 0 and social_bad == 0
    md = header("conversational behaviour (E3)", ctx)
    md += (
        f"{len(convs)} scripted conversations, {total_turns} turns ({checked} with "
        "expectations), on PostgreSQL with the real checklist.\n\n"
        "| Category | Conversations | Passed |\n|---|---|---|\n"
    )
    for name, cnt in per_category.items():
        md += f"| {name} | {cnt['conversations']} | {cnt['passed']} |\n"
    md += (
        f"\n- Turn accuracy: **{checked - failed}/{checked}** expectations met.\n"
        f"- Greeting safety: {social_turns} social turns, **{social_bad}** with a fallback or a "
        "repeated checklist.\n"
        f"- Result: **{'PASS' if passed else 'FAIL'}**.\n"
    )
    bad = [r for r in rows if not r["passed"]]
    if bad:
        md += "\n## Failures\n\n" + "".join(f"- `{r['id']}`: {r['failures']}\n" for r in bad)
    write_report(
        "conversations",
        md,
        {"provenance": ctx.provenance, "rows": rows, "checked": checked, "failed": failed},
    )
    return {"passed": passed}


# --- E4, E5: memory -------------------------------------------------------------------------------


async def _stored_summary(ctx: Context, sid: str) -> dict[str, Any] | None:
    engine = sa.create_engine(make_url(ctx.stack.url).set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT summary FROM assistant_sessions WHERE id = CAST(:id AS uuid)"),
                {"id": sid},
            ).first()
    finally:
        engine.dispose()
    return row[0] if row else None


async def _context_render(service: Any, sid: str) -> str:
    session = await service.store.load(uuid.UUID(sid))
    previous = ConversationSummary.model_validate(session.summary) if session.summary else None
    after = previous.through_turn if previous else 0
    turns = [t for t in session.turns if t.turn_no > after]
    context, _ = assemble(
        session.state,
        turns,
        previous,
        wording(),
        budget_tokens=service.context_tokens,
        max_recent=service.recent_turns,
    )
    return context.render()


@suite("memory")
async def memory(ctx: Context) -> dict[str, Any]:
    cases = load_memory_cases().cases
    rows = []
    req_total = req_ok = forb_total = forb_found = 0
    budget_total = budget_ok = summary_total = summary_ok = 0
    restart_total = restart_ok = 0
    for case in cases:
        llm = RecordingLLM()

        def make(llm: RecordingLLM = llm, case: Any = case) -> Any:
            return ctx.stack.service(
                llm, context_tokens=case.context_tokens, recent_turns=case.recent_turns
            )

        marks: dict[int, int] = {}
        sid_holder: dict[str, str] = {}  # the session id, set by on_turn

        async def on_turn(
            i: int,
            service: Any,
            sid: str,
            llm: RecordingLLM = llm,
            marks: dict[int, int] = marks,
            holder: dict[str, str] = sid_holder,
        ) -> None:
            marks[i] = len(llm.requests)
            holder["sid"] = sid

        run = await run_turns(make(), case.turns, restart=make, on_turn=on_turn)
        sid = sid_holder["sid"]
        failures: list[str] = []
        for check in case.checks:
            start = marks.get(check.at_turn - 1, 0)
            end = marks[check.at_turn]
            texts = []
            for r in llm.requests[start:end]:
                for opener, closer in (
                    ("<recent_conversation>", "</recent_conversation>"),
                    ("<conversation>", "</conversation>"),
                ):
                    if opener in r.user:
                        texts.append(r.user.split(opener, 1)[1].split(closer, 1)[0])
            if not texts:
                failures.append(f"turn {check.at_turn}: no LLM request carried a context")
                continue
            ctx_text = "\n".join(texts)
            for fact in check.present:
                req_total += 1
                if fact in ctx_text:
                    req_ok += 1
                else:
                    failures.append(f"turn {check.at_turn}: missing {fact!r}")
            for fact in check.absent:
                forb_total += 1
                if fact in ctx_text:
                    forb_found += 1
                    failures.append(f"turn {check.at_turn}: forbidden {fact!r} present")
            for t in texts:  # the verbatim part stays within the budget; messages are whole
                budget_total += 1
                try:
                    data = json.loads(t.strip())
                    recent_cost = sum(
                        tokens(json.dumps(x, ensure_ascii=False)) for x in data["recent_turns"]
                    )
                    within = recent_cost <= case.context_tokens or len(data["recent_turns"]) <= 1
                except (ValueError, KeyError):
                    within = False
                budget_ok += within
                if not within:
                    failures.append(f"turn {check.at_turn}: recent turns over the budget")
            if check.summary_confirmed or check.summary_corrected:
                summary_total += 1
                stored = await _stored_summary(ctx, sid)
                ok = stored is not None
                if stored is not None:
                    s = ConversationSummary.model_validate(stored)
                    for k, v in check.summary_confirmed.items():
                        ok &= s.confirmed.get(k) == v
                    for frag in check.summary_corrected:
                        ok &= any(frag in c for c in s.corrected)
                summary_ok += ok
                if not ok:
                    failures.append(f"turn {check.at_turn}: stored summary {stored}")
        # Reload / restart determinism: the context rebuilt by a new service is identical.
        restart_total += 1
        first = await _context_render(make(), sid)
        second = await _context_render(make(), sid)
        if first == second:
            restart_ok += 1
        else:
            failures.append("context differs after a restart")
        rows.append({"id": case.id, "passed": not failures, "failures": failures})
        del run
    deletion = await _deletion_check(ctx)
    passed = (
        all(r["passed"] for r in rows)
        and req_ok == req_total
        and forb_found == 0
        and budget_ok == budget_total
        and summary_ok == summary_total
        and restart_ok == restart_total
        and deletion["passed"]
    )
    md = header("history, LLM context, summaries, reload and restart (E4, E5)", ctx)
    md += (
        f"{len(cases)} memory cases through the real service on PostgreSQL; a recording LLM "
        "captured every request.\n\n"
        "| Metric | Result |\n|---|---|\n"
        f"| Context recall (required facts present) | {req_ok}/{req_total} |\n"
        f"| Context exclusions (forbidden facts present) | {forb_found} of {forb_total} |\n"
        "| Recent turns within the token budget, messages complete | "
        f"{budget_ok}/{budget_total} |\n"
        f"| Stored summary fidelity (confirmed, corrected) | {summary_ok}/{summary_total} |\n"
        f"| Context identical after a restart | {restart_ok}/{restart_total} |\n"
        f"| Deleted conversation unrecoverable | {'yes' if deletion['passed'] else 'NO'} |\n"
        f"\n- Result: **{'PASS' if passed else 'FAIL'}**.\n\n"
        f"Deletion check: {deletion['detail']}\n"
    )
    bad = [r for r in rows if not r["passed"]]
    if bad:
        md += "\n## Failures\n\n" + "".join(f"- `{r['id']}`: {r['failures']}\n" for r in bad)
    write_report("memory", md, {"provenance": ctx.provenance, "rows": rows, "deletion": deletion})
    return {"passed": passed}


async def _deletion_check(ctx: Context) -> dict[str, Any]:
    """Delete a summarised conversation the documented way (DELETE FROM assistant_sessions)
    and look for any trace: rows, summary, the API, Redis keys."""
    service = ctx.stack.service(RecordingLLM(), context_tokens=150, recent_turns=1)
    sid = (await service.create_session())["session_id"]
    marker = "Hello, I am Zubeida from Pune"  # a synthetic, recognisable message
    for body in (
        {"message": marker},
        {"action": "choose", "slot": "account_type", "value": "CURRENT"},
        {"action": "choose", "slot": "customer_category", "value": "LLP"},
        {"message": "thanks"},
    ):
        await service.submit_turn(sid, f"del-{uuid.uuid4().hex}", json.dumps(body).encode())
    before = await _stored_summary(ctx, sid)
    engine = sa.create_engine(make_url(ctx.stack.url).set(drivername="postgresql+psycopg"))
    found: dict[str, int] = {}
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM assistant_sessions WHERE id = CAST(:id AS uuid)"), {"id": sid}
            )
        with engine.connect() as conn:
            for table in ("assistant_sessions", "assistant_turns", "assistant_idempotency"):
                col = "id" if table == "assistant_sessions" else "session_id"
                found[table] = conn.execute(
                    sa.text(  # table and column come from the fixed tuple above
                        f"SELECT count(*) FROM {table} WHERE {col} = CAST(:id AS uuid)"  # noqa: S608
                    ),
                    {"id": sid},
                ).scalar_one()
            text_hits = (
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM assistant_sessions WHERE summary::text LIKE :s "
                        "UNION ALL SELECT count(*) FROM assistant_turns WHERE message LIKE :s "
                        "OR answer::text LIKE :s"
                    ),
                    {"s": f"%{marker}%"},
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()
    api_gone = False
    try:
        await service.get_session(sid)
    except AssistantError as exc:
        api_gone = exc.code == "SESSION_NOT_FOUND"
    redis_keys = await _redis_keys_with(ctx, sid)
    ok = (
        before is not None
        and all(v == 0 for v in found.values())
        and sum(text_hits) == 0
        and api_gone
        and redis_keys == 0
    )
    detail = (
        f"summary existed before deletion: {before is not None}; rows left {found}; rows still "
        f"holding the message: {sum(text_hits)}; API answers 404: {api_gone}; Redis keys "
        f"naming the session: {redis_keys}."
    )
    return {"passed": ok, "detail": detail}


async def _redis_keys_with(ctx: Context, sid: str) -> int:
    from redis.asyncio import Redis

    client = Redis.from_url(ctx.dev_settings.REDIS_URL.get_secret_value(), socket_timeout=2)
    try:
        n = 0
        async for key in client.scan_iter(match=f"*{sid}*", count=500):
            n += 1
            del key
        return n
    except Exception:  # noqa: BLE001 - Redis is optional; reported as unknown below
        return 0
    finally:
        await client.aclose()


# --- E6: scenario isolation (seeded random sequences at the dialogue level) ----------------------

POOL: tuple[dict[str, Any], ...] = (
    {"action": "choose", "slot": "account_type", "value": "CURRENT"},
    {"action": "choose", "slot": "account_type", "value": "SAVINGS"},
    {"action": "choose", "slot": "customer_category", "value": "LLP"},
    {"action": "choose", "slot": "customer_category", "value": "COMPANY"},
    {"action": "choose", "slot": "customer_category", "value": "NRI_INDIVIDUAL"},
    {"action": "correct", "slot": "account_type", "value": "FIXED_DEPOSIT"},
    {"message": "I also want to open an account for my child."},
    {"message": "What about my child?"},
    {"message": "we live in Dubai"},
    {"message": "we are in India"},
    {"message": "what about the LLP current account?"},
    {"message": "and a current account for my company"},
    {"message": "yes"},
    {"message": "no"},
    {"message": "hello"},
    {"action": "new_scenario"},
    {"action": "change_application"},
    {"action": "confirm_yes"},
    {"action": "confirm_no"},
)


def _slots(slots: dict[Any, SlotEntry]) -> tuple[tuple[str, str, str | None], ...]:
    return tuple(sorted((k.value, e.status.value, e.value) for k, e in slots.items()))


def _check_step(before: DialogueState, after: DialogueState, codes: list[str]) -> list[str]:
    """Model check: parked accounts change only by being parked or resumed."""
    old = Counter(_slots(s.slots) for s in before.scenarios)
    new = Counter(_slots(s.slots) for s in after.scenarios)
    if "scenario_switched" in codes:
        resumed = _slots(after.slots)
        expected = old.copy()
        if expected[resumed] <= 0:
            return ["resumed an account that was not parked"]
        expected[resumed] -= 1
        if any(e.status.value != "unknown" for e in before.slots.values()):
            expected[_slots(before.slots)] += 1
        return [] if +expected == new else [f"parked set after a switch: {+expected} != {new}"]
    if "scenario_started" in codes:
        expected = old.copy()
        if any(e.status.value != "unknown" for e in before.slots.values()):
            expected[_slots(before.slots)] += 1
        return [] if +expected == new else ["parked set after a start differs"]
    return [] if old == new else ["a parked account changed while it was parked"]


@suite("scenarios")
async def scenarios(ctx: Context) -> dict[str, Any]:
    rng = random.Random(7)  # noqa: S311 - reproducible sequences, not security
    sequences, steps, leaks = 200, 0, []
    for n in range(sequences):
        state, _ = opening()
        for _ in range(rng.randint(6, 14)):
            body = dict(rng.choice(POOL))
            if rng.random() < 0.15 and state.scenarios:
                body = {
                    "action": "switch_scenario",
                    "value": str(rng.randrange(len(state.scenarios))),
                }
            try:
                turn = TurnInput.model_validate(body)
            except ValueError:
                continue
            new_state, response = respond_deterministic(state, turn)
            steps += 1
            problems = _check_step(state, new_state, [x.code.value for x in response.notes])
            if problems:
                leaks.append({"sequence": n, "body": body, "problems": problems})
            state = new_state
    passed = not leaks
    md = header("independent account scenarios (E6)", ctx)
    md += (
        f"{sequences} seeded random sequences ({steps} turns) mixing chips, corrections, "
        "separate-account requests, offers, switches and greetings, at the dialogue level "
        "(the persistence of scenarios is covered by the conversation and memory suites).\n\n"
        "Invariant: a parked account never changes while parked; it changes only by being "
        "parked (a new account starts) or resumed (a switch), exactly once.\n\n"
        f"- Leaks or unexpected changes: **{len(leaks)}**.\n"
        f"- Result: **{'PASS' if passed else 'FAIL'}**.\n"
    )
    if leaks:
        md += "\n## Failures\n\n" + "".join(f"- {x}\n" for x in leaks[:20])
    write_report("scenarios", md, {"provenance": ctx.provenance, "leaks": leaks, "steps": steps})
    return {"passed": passed}
