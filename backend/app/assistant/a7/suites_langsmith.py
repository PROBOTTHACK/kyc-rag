"""A7 LangSmith evaluation (owner requirement at the A7 approval: "LangSmith observability and
evaluation"; run once the owner connected LangSmith, 2026-11-16).

The committed A7 conversation set (§2.3, synthetic) becomes a LangSmith dataset, and the real
assistant (development stack, throwaway conversation database, deterministic: no LLM) is run
over it as a LangSmith experiment with deterministic evaluators:
- `turn_accuracy`: the conversation's checked turns that meet every scripted expectation;
- `conversation_pass`: 1 when all of them do;
plus summary evaluators for the overall rates. The numbers are the same checks as the
`conversations` suite, now also visible in LangSmith.

What is uploaded: conversation ids, categories, the scripted customer turns (synthetic,
committed in data/eval) and, per conversation, answer kinds and failure codes. Never stored
source passages, prompts, checklists or keys. Without `LANGSMITH_API_KEY` nothing is sent and
the report says so.
"""

from __future__ import annotations

from typing import Any

from app.assistant.a7.core import Context, header, suite, write_report
from app.assistant.a7.datasets import Expect, load_conversations
from app.assistant.a7.driver import describe, run_turns

DATASET = "account-opening-assistant-a7-conversations"


@suite("langsmith_eval")
async def langsmith_eval(ctx: Context) -> dict[str, Any]:
    settings = ctx.dev_settings
    key = settings.LANGSMITH_API_KEY.get_secret_value() if settings.LANGSMITH_API_KEY else ""
    data = load_conversations()
    md = header("LangSmith evaluation", ctx)
    if not key:
        md += "- Not run: no `LANGSMITH_API_KEY` configured. Nothing was uploaded.\n"
        write_report("langsmith_eval", md, {"provenance": ctx.provenance, "run": False})
        return {"passed": True}

    from langsmith import Client, aevaluate

    client = Client(api_key=key, timeout_ms=10_000)
    convs = {c.id: c for c in data.conversations}
    name = f"{DATASET}-{data.version}"
    try:
        exists = client.has_dataset(dataset_name=name)
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        reason = (
            "the configured key is a tracing-only key (403: datasets are not allowed); a "
            "LangSmith personal access token or service key with dataset permissions is needed"
            if "403" in str(exc)
            else f"LangSmith unavailable ({type(exc).__name__})"
        )
        md += f"- **Not run:** {reason}. No dataset or experiment was created.\n"
        write_report("langsmith_eval", md, {"provenance": ctx.provenance, "run": False,
                                            "reason": reason})  # fmt: skip
        return {"passed": True}
    if exists:
        dataset = client.read_dataset(dataset_name=name)
        existing = {
            (e.inputs or {}).get("conversation_id")
            for e in client.list_examples(dataset_id=dataset.id)
        }
    else:
        dataset = client.create_dataset(
            name,
            description=(
                "A7 scripted conversations (data/eval/a7_conversations.yaml, "
                f"version {data.version}). Synthetic only. Account Opening Assistant, an "
                "independent prototype; not affiliated with or endorsed by HDFC Bank or the RBI."
            ),
        )
        existing = set()
    missing = [c for c in data.conversations if c.id not in existing]
    if missing:
        client.create_examples(
            dataset_id=dataset.id,
            examples=[
                {
                    "inputs": {
                        "conversation_id": c.id,
                        "category": c.category,
                        "turns": [describe(t) for t in c.turns],
                    },
                    "outputs": {"checked_turns": sum(1 for t in c.turns if t.expect != Expect())},
                    "metadata": {"category": c.category, "description": c.description},
                }
                for c in missing
            ],
        )  # fmt: skip

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        conv = convs[inputs["conversation_id"]]
        run = await run_turns(ctx.stack.service(), conv.turns, restart=ctx.stack.service)
        checked = [t for t in run.turns if t.checked]
        return {
            "passed": run.passed,
            "turns": len(run.turns),
            "checked_turns": len(checked),
            "failed_turns": [
                {"turn": t.index, "failures": t.failures} for t in checked if t.failures
            ],
            "answer_kinds": [t.answer.get("kind") for t in run.turns],
        }

    # Evaluators take `outputs` by name (LangSmith passes arguments by parameter name).
    def turn_accuracy(outputs: dict[str, Any]) -> dict[str, Any]:
        checked = outputs["checked_turns"]
        ok = checked - len(outputs["failed_turns"])
        return {"key": "turn_accuracy", "score": ok / checked if checked else 1.0}

    def conversation_pass(outputs: dict[str, Any]) -> dict[str, Any]:
        return {"key": "conversation_pass", "score": 1 if outputs["passed"] else 0}

    def overall(outputs: list[dict[str, Any]]) -> dict[str, Any]:
        checked = sum(o["checked_turns"] for o in outputs)
        failed = sum(len(o["failed_turns"]) for o in outputs)
        passed = sum(1 for o in outputs if o["passed"])
        return {
            "results": [
                {"key": "overall_turn_accuracy", "score": (checked - failed) / max(checked, 1)},
                {"key": "conversations_passed", "score": passed / max(len(outputs), 1)},
            ]
        }

    p = ctx.provenance
    results = await aevaluate(
        target,
        data=name,
        evaluators=[turn_accuracy, conversation_pass],
        summary_evaluators=[overall],  # type: ignore[list-item]
        experiment_prefix="a7-deterministic",
        description="The real assistant, deterministic (no LLM), on the development corpus.",
        metadata={
            "commit": p["commit"], "dirty": p["dirty"], "coverage": p["coverage_version"],
            "catalogue": p["catalogue_version"], "llm": "none (deterministic)",
        },  # fmt: skip
        max_concurrency=1,
        client=client,
    )
    rows = [r async for r in results]
    outputs = [r["run"].outputs or {} for r in rows]
    checked = sum(o.get("checked_turns", 0) for o in outputs)
    failed = sum(len(o.get("failed_turns", [])) for o in outputs)
    passed_convs = sum(1 for o in outputs if o.get("passed"))
    client.flush()
    experiment = results.experiment_name
    project = client.read_project(project_name=experiment)
    url = getattr(project, "url", None) or ""
    ok = len(rows) == len(data.conversations) and failed == 0
    md += (
        f"The {len(data.conversations)} scripted conversations (`a7_conversations.yaml`, version "
        f"`{data.version}`) uploaded as the LangSmith dataset `{name}` and run as the experiment "
        f"`{experiment}` with deterministic evaluators (`turn_accuracy`, `conversation_pass`, "
        "and the summary `overall_turn_accuracy` / `conversations_passed`).\n\n"
        "| Check | Result |\n|---|---|\n"
        f"| Examples evaluated | {len(rows)} of {len(data.conversations)} |\n"
        f"| Conversations passing every expectation | {passed_convs} of {len(rows)} |\n"
        f"| Checked turns meeting every expectation | {checked - failed} of {checked} |\n"
        f"| Experiment | `{experiment}`{f' ({url})' if url else ''} |\n\n"
        "Uploaded: conversation ids, categories, the scripted synthetic turns, answer kinds and "
        "failure codes. Never source passages, prompts, checklists or keys.\n\n"
        f"- Result: **{'PASS' if ok else 'FAIL'}**.\n"
    )
    write_report("langsmith_eval", md, {
        "provenance": p, "dataset": name, "experiment": experiment, "examples": len(rows),
        "checked_turns": checked, "failed_turns": failed, "passed_conversations": passed_convs,
    })  # fmt: skip
    return {"passed": ok}
