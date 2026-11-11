"""The M3 LLP demo through the running API (M3 S8). Run from `backend/` with `make api`
already running: `python -m app.workflow.demo [--live] [--preflight-only]`.

1. Preflight (presence checks only: no setting value is ever printed): the API answers
   `/health`; PostgreSQL is migrated to head and holds the ingested corpus. With
   `--live` it also requires the owner's Groq live gate (`GROQ_LIVE_ENABLED`,
   `GROQ_DATA_TERMS_VERIFIED_ON`, `GROQ_API_KEY`, `PRIMARY_LLM_MODEL`) and
   `LLM_CACHE_ENABLED=false`, so the run is genuinely live.
2. The run: a `synthetic_demo` session; the SSE stream opened first and printed; the
   approved synthetic question (`m3-demo-01` from the committed allowlist, never typed
   text); the clarification answered with enum values only; the report fetched.
3. The verdict: `app.workflow.acceptance.check_report` (citations looked up in Postgres),
   the actual outcome as returned (a degraded or incomplete outcome is reported, never
   retried into a better one) and the LLM call metadata recorded for the run (provider,
   model, latency, tokens; never prompts). With `--live`, at least one Groq call must be
   recorded, otherwise the API was not running with the live gate open.
The full report and metadata go to the gitignored `data/processed/m3_demo/`.
Exit codes: 0 accepted; 1 not accepted or the run failed; 2 preflight failed.

Data sent anywhere: only what the API itself sends (the screened synthetic question, the
enum answer and public corpus text to Groq when live). Gemini is never called (D4).
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

from app.config import REPO_ROOT, Settings, get_settings
from app.db.migrations import current_revision, head_revision
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.workflow.acceptance import check_report, db_chunk_lookup

DEMO_ID = "m3-demo-01"
# Enum answers for the clarification fields the demo question may need (synthetic).
ANSWERS = {"entity_type": "LLP", "account_type": "CURRENT_ACCOUNT"}
TERMINAL = {"completed", "failed", "abandoned"}
OUT_DIR = REPO_ROOT / "data" / "processed" / "m3_demo"


def demo_query(settings: Settings, demo_id: str = DEMO_ID) -> str:
    """The approved synthetic question from the committed allowlist (never free text)."""
    for line in Path(settings.LLM_CACHE_ALLOWLIST).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("id") == demo_id:
            if row.get("synthetic") is not True:
                raise ValueError(f"{demo_id} is not marked synthetic")
            return str(row["query"])
    raise ValueError(f"{demo_id} is not in the synthetic allowlist")


# --- preflight ---------------------------------------------------------------------------------


def preflight(settings: Settings, base_url: str, *, live: bool) -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []
    try:
        health = httpx.get(f"{base_url}/api/v1/health", timeout=5)
        checks.append(("api reachable (make api)", health.status_code in (200, 503)))
    except httpx.HTTPError:
        checks.append(("api reachable (make api)", False))
    engine = create_sync_engine(settings)
    try:
        checks.append(("postgres migrated to head", current_revision(engine) == head_revision()))
        with engine.connect() as conn:
            chunks = conn.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one()
        checks.append(("corpus ingested (chunks > 0)", int(chunks) > 0))
    except sa.exc.SQLAlchemyError:
        checks.append(("postgres reachable", False))
    finally:
        engine.dispose()
    try:
        demo_query(settings)
        checks.append((f"synthetic question {DEMO_ID} in the allowlist", True))
    except (OSError, ValueError):
        checks.append((f"synthetic question {DEMO_ID} in the allowlist", False))
    if live:
        checks += [
            ("GROQ_LIVE_ENABLED is true", settings.GROQ_LIVE_ENABLED),
            (
                "GROQ_DATA_TERMS_VERIFIED_ON is set",
                settings.GROQ_DATA_TERMS_VERIFIED_ON is not None,
            ),
            ("GROQ_API_KEY is present", settings.GROQ_API_KEY is not None),
            ("PRIMARY_LLM_MODEL is set", bool(settings.PRIMARY_LLM_MODEL)),
            ("LLM_CACHE_ENABLED is false (a genuinely live run)", not settings.LLM_CACHE_ENABLED),
        ]
    return checks


# --- the run -----------------------------------------------------------------------------------


async def _stream(client: httpx.AsyncClient, url: str, lines: list[str]) -> None:
    # `send(stream=True)`, not `client.stream(...)`: the S6 AST guard reserves graph-
    # submission method names (astream/stream/...) for the workflow service.
    r = await client.send(client.build_request("GET", url, timeout=None), stream=True)
    try:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                label = ev["node"] if ev["kind"] == "node" else f"run {ev['status']}"
                seq = f"#{ev['seq']}" if ev["seq"] is not None else "  "
                print(f"  [event] {seq:>4} {label} ({ev['status']})")
                lines.append(line)
    finally:
        await r.aclose()


async def _wait(client: httpx.AsyncClient, sid: str, want: set[str], limit_s: float) -> Any:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        view = (await client.get(f"/api/v1/sessions/{sid}")).json()
        wf = view.get("workflow") or {}
        if wf.get("status") in want:
            return wf
        await asyncio.sleep(0.5)
    raise TimeoutError(f"workflow did not reach {sorted(want)}")


async def run_demo(base_url: str, query: str, limit_s: float) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        r = await client.post("/api/v1/sessions", json={"data_class": "synthetic_demo"})
        r.raise_for_status()
        sid = r.json()["session_id"]
        print(f"session {sid} (synthetic_demo)")
        events: list[str] = []
        stream = asyncio.create_task(_stream(client, f"/api/v1/sessions/{sid}/events", events))
        await asyncio.sleep(0.5)  # the stream is open before the first message

        def key() -> dict[str, str]:
            return {"Idempotency-Key": str(uuid.uuid4())}

        print(f"question ({DEMO_ID}): {query}")
        (
            await client.post(
                f"/api/v1/sessions/{sid}/messages", json={"message": query}, headers=key()
            )
        ).raise_for_status()
        wf = await _wait(client, sid, TERMINAL | {"awaiting_clarification"}, limit_s)
        rounds = 0
        while wf["status"] == "awaiting_clarification" and rounds < 2:
            rounds += 1
            fields = [q["field"] for q in wf["clarification"]["questions"]]
            print(f"clarification round {rounds}: asked {fields}")
            unknown = [f for f in fields if f not in ANSWERS]
            if unknown:
                raise RuntimeError(f"no synthetic enum answer for {unknown}")
            answers = {f: ANSWERS[f] for f in fields}
            print(f"  answering {answers}")
            body = {"checkpoint_id": wf["clarification"]["checkpoint_id"], "answers": answers}
            r = await client.post(f"/api/v1/sessions/{sid}/resume", json=body, headers=key())
            r.raise_for_status()
            wf = await _wait(client, sid, TERMINAL | {"awaiting_clarification"}, limit_s)
        try:
            await asyncio.wait_for(stream, 15)
        except TimeoutError:
            stream.cancel()
        report = None
        if wf.get("report_id"):
            report = (await client.get(f"/api/v1/reports/{wf['report_id']}")).json()
        return {"session_id": sid, "workflow": wf, "report": report, "events": len(events)}


def _llm_calls(settings: Settings, run_id: str) -> list[dict[str, Any]]:
    engine = create_sync_engine(settings)
    try:
        with engine.connect() as conn:
            calls = conn.execute(
                sa.text("SELECT llm_calls FROM graph_runs WHERE id = :id"), {"id": run_id}
            ).scalar_one_or_none()
        return list(calls or [])
    finally:
        engine.dispose()


def summarize(report: dict[str, Any]) -> None:
    print(
        f"outcome: {report['outcome']} (human review required: {report['human_review_required']})"
    )
    print(f"evidence status: {report.get('evidence_status')}")
    for item in report.get("checklist") or []:
        cites = ", ".join(
            f"{c['document_key']} p.{c['page']} chunk {c['evidence_id'][:8]}"
            for c in item["citations"]
        )
        print(
            f"  - {item['name']} [{item['applicability']}; proposed "
            f"{item['proposed_applicability']}] <- {cites}"
        )
    for u in report.get("unresolved_items") or []:
        print(f"  unresolved: {u}")
    for d in report.get("degradations") or []:
        print(f"  degradation: {d}")
    print(f"dropped requirements: {report.get('dropped_requirements')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workflow.demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--live", action="store_true", help="require the Groq live gate")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    settings = get_settings()

    print(f"preflight ({'live Groq' if args.live else 'whatever LLM the API runs with'}):")
    checks = preflight(settings, args.base_url, live=args.live)
    for name, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not all(ok for _, ok in checks):
        return 2
    if args.preflight_only:
        return 0

    result = asyncio.run(run_demo(args.base_url, demo_query(settings), args.timeout))
    wf, report = result["workflow"], result["report"]
    calls = _llm_calls(settings, wf["run_id"])
    print(f"run {wf['run_id']}: {wf['status']}; {result['events']} events streamed")
    print(f"llm calls recorded: {len(calls)}")
    for c in calls:
        print(
            f"  {c['node']}: {c['provider']} {c['model_id']} {c['result']} "
            f"{c['latency_ms']} ms tokens {c.get('input_tokens')}/{c.get('output_tokens')} "
            f"cache_hit={c.get('cache_hit')}"
        )
    verdict: dict[str, Any] = {"accepted": False, "problems": ["no_report"]}
    if report is not None:
        summarize(report)
        with create_sync_sessionmaker(create_sync_engine(settings))() as db:
            acc = check_report(report, db_chunk_lookup(db))
        verdict = {
            "accepted": acc.ok,
            "problems": acc.problems,
            "items": acc.items,
            "mandatory": acc.mandatory,
            "citations": acc.citations,
            "unresolved": acc.unresolved,
        }
    live_ok = not args.live or any(c["provider"] == "groq" for c in calls)
    if not live_ok:
        verdict["problems"] = [*verdict["problems"], "no_live_groq_call_recorded"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}{'-live' if args.live else ''}.json"
    out.write_text(
        json.dumps({**result, "llm_calls": calls, "verdict": verdict}, indent=2), encoding="utf-8"
    )
    print(f"acceptance: {'PASS' if verdict['accepted'] and live_ok else 'FAIL'} {verdict}")
    print(f"saved: {out.relative_to(REPO_ROOT)}")
    return 0 if verdict["accepted"] and live_ok and wf["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
