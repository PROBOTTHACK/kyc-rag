"""A7.4 suites: privacy, PII screening and session isolation (E8); idempotency, failure
recovery and latency (E9).

Every identifier is synthetic (data/eval/a7_pii_messages.jsonl). Leakage is searched for in
the throwaway database (every assistant table, as text), in every log record emitted during
the run, and in every request the recording LLM received.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
import uuid
from datetime import timedelta
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.assistant.a7.core import Context, header, suite, write_report
from app.assistant.a7.datasets import load_conversations, load_pii
from app.assistant.a7.probes import RecordingLLM
from app.assistant.service import AssistantError, AssistantService
from app.llm.common import ProviderRateLimited, ProviderUnavailable


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - a log line that cannot be formatted is skipped
            self.lines.append(str(record.msg))


def _db_text(ctx: Context) -> str:
    engine = sa.create_engine(make_url(ctx.stack.url).set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn:
            parts = [
                conn.execute(
                    sa.text(  # tbl comes from the fixed tuple below
                        f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {tbl} t"  # noqa: S608
                    )
                ).scalar_one()
                for tbl in ("assistant_sessions", "assistant_turns", "assistant_idempotency")
            ]
    finally:
        engine.dispose()
    return "\n".join(parts)


async def _send(
    service: AssistantService, sid: str, body: dict[str, Any], key: str | None = None
) -> dict[str, Any]:
    return await service.submit_turn(
        sid, key or f"a7-{uuid.uuid4().hex}", json.dumps(body).encode()
    )


@suite("privacy")
async def privacy(ctx: Context) -> dict[str, Any]:
    rows = load_pii()
    capture = _Capture()
    capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s %(args)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    llm = RecordingLLM()
    service = ctx.stack.service(llm)
    results = []
    try:
        for r in rows:
            sid = (await service.create_session())["session_id"]
            # Some context first, so the identifier arrives mid-conversation.
            await _send(
                service, sid, {"action": "choose", "slot": "account_type", "value": "CURRENT"}
            )
            out = await _send(service, sid, {"message": r.message})
            answer = out["answer"]
            blocked = answer["kind"] == "blocked"
            echoed = r.secret is not None and r.secret in json.dumps(answer, ensure_ascii=False)
            if r.expect == "block":
                ok = blocked and not echoed
            elif r.expect == "redact":
                ok = not blocked and not echoed
            else:
                ok = not blocked
            results.append({"id": r.id, "category": r.category, "expect": r.expect,
                            "kind": answer["kind"], "echoed": echoed, "ok": ok})  # fmt: skip
        # Session isolation: two sessions, one reads only its own turns; unknown ids are 404.
        a = (await service.create_session())["session_id"]
        b = (await service.create_session())["session_id"]
        # A unique synthetic marker (every profile lists all category labels as choices,
        # so a category name would be a false positive).
        await _send(service, a, {"message": "my Quokkaflux LLP needs a current account"})
        await _send(service, b, {"message": "hello"})
        view_b = await service.get_session(b)
        cross = "Quokkaflux" in json.dumps(view_b)
        unknown = 0
        for _ in range(20):
            try:
                await service.get_session(str(uuid.uuid4()))
            except AssistantError as exc:
                unknown += exc.code == "SESSION_NOT_FOUND"
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)
    ids = [uuid.uuid4() for _ in range(10_000)]
    id_ok = len(set(ids)) == len(ids) and all(i.version == 4 for i in ids)
    secrets = [r.secret for r in rows if r.secret]
    db = _db_text(ctx)
    logs = "\n".join(capture.lines)
    llm_text = "\n".join(q.system + q.user for q in llm.requests)
    leaks = {
        "database": [s for s in secrets if s in db],
        "logs": [s for s in secrets if s in logs],
        "llm_requests": [s for s in secrets if s in llm_text],
    }
    by = {"block": [0, 0], "redact": [0, 0], "allow": [0, 0]}
    for x in results:
        by[x["expect"]][0] += x["ok"]
        by[x["expect"]][1] += 1
    redacted_stored = "[REDACTED_EMAIL]" in db and "[REDACTED_PHONE]" in db
    passed = (
        all(x["ok"] for x in results)
        and not any(leaks.values())
        and redacted_stored
        and not cross
        and unknown == 20
        and id_ok
    )
    md = header("privacy, PII screening and session isolation (E8)", ctx)
    md += (
        f"{len(rows)} synthetic messages, each sent mid-conversation through the real service, "
        "with a recording LLM and every log record captured.\n\n"
        "| Check | Result |\n|---|---|\n"
        "| Identifiers refused (PAN, Aadhaar, passport, card, account) | "
        f"{by['block'][0]}/{by['block'][1]} |\n"
        f"| E-mail and phone redacted, request allowed | {by['redact'][0]}/{by['redact'][1]} |\n"
        f"| Negative controls allowed (false positives) | {by['allow'][0]}/{by['allow'][1]} |\n"
        f"| Identifier echoed in an answer | {sum(x['echoed'] for x in results)} |\n"
        f"| Identifiers found in the database (all assistant tables) | {len(leaks['database'])} |\n"
        f"| Identifiers found in log records ({len(capture.lines)} captured) | "
        f"{len(leaks['logs'])} |\n"
        f"| Identifiers found in LLM requests ({len(llm.requests)} captured) | "
        f"{len(leaks['llm_requests'])} |\n"
        "| Redacted forms stored instead (`[REDACTED_EMAIL]`, `[REDACTED_PHONE]`) | "
        f"{'yes' if redacted_stored else 'NO'} |\n"
        f"| Another session's content readable | {'YES' if cross else 'no'} |\n"
        f"| Unknown session ids answered 404 | {unknown}/20 |\n"
        f"| 10,000 session ids: version 4, no collision | {'yes' if id_ok else 'NO'} |\n"
        f"\n- Result: **{'PASS' if passed else 'FAIL'}**.\n"
    )
    bad = [x for x in results if not x["ok"]]
    if bad:
        md += "\n## Failures\n\n" + "".join(
            f"- `{x['id']}` ({x['category']}): kind {x['kind']}, echoed {x['echoed']}\n"
            for x in bad
        )
    write_report("privacy", md, {"provenance": ctx.provenance, "results": results,
                                 "leaks": leaks, "passed": passed})  # fmt: skip
    return {"passed": passed}


# --- E9: idempotency, failures, latency -----------------------------------------------------------


class _FailingOnce:
    """A checklist factory whose first build raises (stored evidence unreadable)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.failed = False

    def __call__(self) -> Any:
        inner = self.inner()
        outer = self

        class _Build:
            def build(self, ready: Any) -> Any:
                if not outer.failed:
                    outer.failed = True
                    raise OSError("simulated storage failure")
                return inner.build(ready)

        return _Build()


class _FailingLLM:
    name = "a7-failing"
    model_id = "a7-failing-0"

    def __init__(self, exc: BaseException | None, delay: float = 0.0) -> None:
        self.exc = exc
        self.delay = delay
        self.calls = 0

    async def generate_structured(self, request: Any, schema: Any) -> Any:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        raise AssertionError("unreachable")


SCRIPT = (
    {"message": "Hi"},
    {"action": "choose", "slot": "account_type", "value": "CURRENT"},
    {"action": "choose", "slot": "customer_category", "value": "LLP"},
    {"message": "Do I need a copy of the PAN card?"},
    {"message": "thanks"},
)


async def _answers(service: AssistantService) -> list[dict[str, Any]]:
    sid = (await service.create_session())["session_id"]
    return [(await _send(service, sid, b))["answer"] for b in SCRIPT]


@suite("reliability")
async def reliability(ctx: Context) -> dict[str, Any]:
    checks: dict[str, tuple[bool, str]] = {}
    service = ctx.stack.service()

    # Idempotency: a replay is byte-identical; a different body under the same key is 409.
    sid = (await service.create_session())["session_id"]
    body = {"action": "choose", "slot": "account_type", "value": "CURRENT"}
    first = await _send(service, sid, body, key="same-key")
    again = await _send(service, sid, body, key="same-key")
    checks["replay byte-identical"] = (json.dumps(first) == json.dumps(again), "")
    try:
        await _send(
            service,
            sid,
            {"action": "choose", "slot": "account_type", "value": "SAVINGS"},
            key="same-key",
        )
        checks["key reuse with a different body is 409"] = (False, "accepted")
    except AssistantError as exc:
        checks["key reuse with a different body is 409"] = (exc.status == 409, exc.code)

    # Ten concurrent submissions with one key store one turn; ten with distinct keys store ten.
    sid = (await service.create_session())["session_id"]
    body = {"action": "choose", "slot": "account_type", "value": "CURRENT"}
    outs = await asyncio.gather(
        *[_send(ctx.stack.service(), sid, body, key="dup") for _ in range(10)]
    )
    one = len({json.dumps(o) for o in outs}) == 1
    view = await service.get_session(sid)
    checks["10 concurrent duplicates: one stored turn, identical answers"] = (
        one and view["turn"] == 1,
        f"turns={view['turn']}",
    )
    sid = (await service.create_session())["session_id"]
    msgs = [{"message": "hello"} for _ in range(10)]
    outs = await asyncio.gather(*[_send(ctx.stack.service(), sid, m) for m in msgs])
    turns = sorted(o["turn"] for o in outs)
    checks["10 concurrent distinct turns are serialised"] = (
        turns == list(range(1, 11)),
        str(turns),
    )

    # A checklist that cannot be built: the fixed technical message, nothing stored, retry works.
    failing = ctx.stack
    flaky = _FailingOnce(failing.checklist)
    svc = AssistantService(service.store, flaky, None)
    sid = (await svc.create_session())["session_id"]
    await _send(svc, sid, {"action": "choose", "slot": "account_type", "value": "CURRENT"})
    tech = await _send(
        svc, sid, {"action": "choose", "slot": "customer_category", "value": "LLP"}, key="retry"
    )
    stored_after = (await svc.get_session(sid))["turn"]
    retry = await _send(
        svc, sid, {"action": "choose", "slot": "customer_category", "value": "LLP"}, key="retry"
    )
    checks["storage failure: technical_error, nothing stored, retry succeeds"] = (
        tech["answer"]["kind"] == "technical_error" and stored_after == 1
        and retry["answer"]["kind"] == "checklist",
        f"{tech['answer']['kind']} -> stored turn {stored_after} -> {retry['answer']['kind']}",
    )  # fmt: skip

    # LLM failures never change an answer: error, rate limit and timeout give the same
    # answers as no LLM at all.
    baseline = await _answers(ctx.stack.service())
    for label, llm in (
        ("provider error", _FailingLLM(ProviderUnavailable("down"))),
        ("rate limited", _FailingLLM(ProviderRateLimited("429"))),
        ("timeout", _FailingLLM(None, delay=30)),
    ):
        started = time.perf_counter()
        got = await _answers(ctx.stack.service(llm))
        elapsed = time.perf_counter() - started
        same = [json.dumps(a, sort_keys=True) for a in got] == [
            json.dumps(a, sort_keys=True) for a in baseline
        ]
        checks[f"LLM {label}: deterministic answers"] = (
            same and llm.calls > 0,
            f"{llm.calls} calls, {elapsed:.1f} s",
        )

    # Expiry: 24 hours after the last turn the session is closed (410).
    svc = ctx.stack.service()
    sid = (await svc.create_session())["session_id"]
    real = svc.clock
    svc.clock = lambda: real() + timedelta(hours=25)
    try:
        await svc.get_session(sid)
        checks["expired session is 410"] = (False, "readable")
    except AssistantError as exc:
        checks["expired session is 410"] = (exc.status == 410, exc.code)

    # The database unavailable: the documented 503 ASSISTANT_UNAVAILABLE, codes only.
    checks["database unavailable: 503 ASSISTANT_UNAVAILABLE"] = await _database_down(ctx)

    # Latency: 500 deterministic turns (the conversation set, cycled), server time.
    timed = await _latency(ctx, 500)
    times = [t for t, _ in timed]
    by_kind: dict[str, list[float]] = {}
    for t, kind in timed:
        by_kind.setdefault(kind, []).append(t)
    p95 = statistics.quantiles(times, n=100)[94]
    p99 = statistics.quantiles(times, n=100)[98]
    checks["deterministic latency p95 <= 750 ms"] = (
        p95 <= 750,
        f"p50 {statistics.median(times):.0f} ms, p95 {p95:.0f} ms, p99 {p99:.0f} ms "
        f"over {len(times)} turns",
    )

    passed = all(ok for ok, _ in checks.values())
    md = header("idempotency, failure recovery and latency (E9)", ctx)
    md += "| Check | Result | Detail |\n|---|---|---|\n" + "".join(
        f"| {name} | {'pass' if ok else 'FAIL'} | {detail} |\n"
        for name, (ok, detail) in checks.items()
    )
    md += (
        "\n### Latency by answer kind (server time, no LLM)\n\n"
        "| Kind | Turns | p50 ms | p95 ms |\n|---|---|---|---|\n"
    )
    for kind, ts in sorted(by_kind.items()):
        q = statistics.quantiles(ts, n=20)[18] if len(ts) > 1 else ts[0]
        md += f"| {kind} | {len(ts)} | {statistics.median(ts):.0f} | {q:.0f} |\n"
    md += (
        "\nChecklist answers are the slowest kind: an NRI checklist runs one supplementary "
        "retrieval per catalogue item. Since 2026-11-16 the query vectors of the catalogue "
        "titles are stored (`app/assistant/query_vectors.py`; identical vectors and rankings), "
        "so no embedding model runs per turn. Before that, 18 sequential `embed_query` calls "
        "(about 37 ms each) put p95 at 867–948 ms, above the 750 ms target.\n"
    )
    md += f"\n- Result: **{'PASS' if passed else 'FAIL'}**.\n"
    record = {k: {"ok": v[0], "detail": v[1]} for k, v in checks.items()}
    write_report("reliability", md, {"provenance": ctx.provenance, "checks": record})
    return {"passed": passed}


async def _database_down(ctx: Context) -> tuple[bool, str]:
    from app.main import create_app

    app = create_app(ctx.dev_settings)
    app.state.assistant = ctx.stack.service()
    # Point the store at a port with nothing listening (the request path is unchanged).
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.assistant.store import PostgresAssistantStore

    dead = create_async_engine(
        make_url(ctx.stack.url).set(drivername="postgresql+psycopg", port=1),
        poolclass=NullPool,
        connect_args={"connect_timeout": 2},
    )
    app.state.assistant.store = PostgresAssistantStore(async_sessionmaker(dead))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a7") as client:
        r = await client.post("/api/v2/assistant/sessions")
    await dead.dispose()
    body = r.json()
    code = (body.get("error") or {}).get("code")
    return r.status_code == 503 and code == "ASSISTANT_UNAVAILABLE", f"{r.status_code} {code}"


async def _latency(ctx: Context, n: int) -> list[tuple[float, str]]:
    service = ctx.stack.service()
    times: list[tuple[float, str]] = []
    convs = [
        c
        for c in load_conversations().conversations
        if not any(t.reload or t.restart for t in c.turns)
    ]
    while len(times) < n:
        for c in convs:
            sid = (await service.create_session())["session_id"]
            for t in c.turns:
                body = t.body()
                assert body is not None  # noqa: S101 - reload/restart conversations excluded
                started = time.perf_counter()
                out = await _send(service, sid, body)
                times.append(((time.perf_counter() - started) * 1000, out["answer"]["kind"]))
                if len(times) >= n:
                    return times
    return times
