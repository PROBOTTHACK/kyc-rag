"""A7 evaluation runner (docs/assistant/A7_PLAN.md).

    python -m app.assistant.a7.runner SUITE [SUITE ...]     (run in backend/; `make a7-eval`)

Every suite runs on the development stack: conversations in a throwaway PostgreSQL
database, evidence read-only from the development corpus, no live LLM (see `live.py` for
the opt-in live run). Each suite writes `docs/assistant/a7/<suite>.md` and a JSON record
under `data/processed/a7/`, and returns whether its hard gates passed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Any

from app.assistant.a7.core import SUITES, Context, header, suite, write_report
from app.assistant.a7.datasets import load_conversations
from app.assistant.a7.driver import run_turns
from app.assistant.a7.probes import CapturingChecklist
from app.assistant.a7.stack import Stack, development_checklist, throwaway_database
from app.config import get_settings
from app.core.loop import selector_loop_factory

# --- suites --------------------------------------------------------------------------------------


@suite("smoke")
async def smoke(ctx: Context) -> dict[str, Any]:
    """One conversation end to end on the real stack (checks the harness itself)."""
    conv = next(c for c in load_conversations().conversations if c.id == "sep-01")
    started = time.perf_counter()
    run = await run_turns(ctx.stack.service(), conv.turns, restart=ctx.stack.service)
    ok = run.passed and ctx.checklist.built != []
    md = header("harness smoke test", ctx) + (
        f"- conversation `{conv.id}`: {'pass' if run.passed else 'FAIL'}\n"
        f"- checklists built from the stored corpus: {len(ctx.checklist.built)}\n"
        f"- elapsed: {time.perf_counter() - started:.1f} s\n"
    )
    write_report("smoke", md, {"provenance": ctx.provenance, "passed": ok})
    return {"passed": ok}


# --- entry point ---------------------------------------------------------------------------------


async def _run(
    names: list[str], url: str, checklist: CapturingChecklist, dev: Any
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    stack = Stack(url, checklist)
    ctx = Context(stack, checklist, dev)
    try:
        for name in names:
            results[name] = await SUITES[name](ctx)
            print(f"{name}: {'PASS' if results[name]['passed'] else 'FAIL'}")
    finally:
        await stack.close()
    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    # Import the later stages' suites so they register (kept in their own modules).
    import importlib.util

    for module in (
        "suites_evidence",
        "suites_conversation",
        "suites_privacy",
        "suites_observability",
        "suites_langsmith",
    ):
        name = f"app.assistant.a7.{module}"
        if importlib.util.find_spec(name) is not None:  # later stages add their modules
            importlib.import_module(name)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="+", choices=[*SUITES, "all"])
    args = parser.parse_args(argv)
    # `all` never uploads: the LangSmith evaluation runs only when named (`langsmith_eval`).
    names = (
        [n for n in SUITES if n not in ("smoke", "langsmith_eval")]
        if "all" in args.suites
        else args.suites
    )
    dev = get_settings()  # the development settings, before DATABASE_URL is switched
    checklist = CapturingChecklist(development_checklist(dev))
    # The database is created and migrated outside the evaluation loop (alembic runs its own).
    with (
        throwaway_database() as url,
        asyncio.Runner(loop_factory=selector_loop_factory) as runner,
    ):
        results = runner.run(_run(names, url, checklist, dev))
    return 0 if all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
