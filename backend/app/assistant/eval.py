"""HDFC-only retrieval evaluation CLI (A2; `make assistant-eval`).

    python -m app.assistant.eval [--mode dense|bm25|hybrid|all] [--k 5]
                                 [--report FILE] [--record FILE]

Runs the HDFC-only retrieval evaluation (A2) against the stored corpus. Read-only: it
writes nothing to Postgres or Qdrant. `all` compares the three existing modes; the
default strategy is not changed by this command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.assistant.evaluation import K, load_cases, render, run
from app.assistant.retrieval import AssistantRetriever
from app.config import get_settings
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.retrieval.factory import retriever_from_settings
from app.retrieval.retrieve import RetrievalMode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.assistant.eval")
    parser.add_argument("--mode", default="dense", choices=["dense", "bm25", "hybrid", "all"])
    parser.add_argument("--k", type=int, default=K)
    parser.add_argument("--report", type=Path, help="write the Markdown report here")
    parser.add_argument("--record", type=Path, help="write the per-case JSON record here")
    args = parser.parse_args(argv)

    settings = get_settings()
    sessions = create_sync_sessionmaker(create_sync_engine(settings))
    base = retriever_from_settings(settings, sessions=sessions)
    modes = list(RetrievalMode) if args.mode == "all" else [RetrievalMode(args.mode)]
    cases = load_cases()
    sections, records, summary = [], [], []
    for mode in modes:
        report = run(AssistantRetriever(base, mode=mode), cases, args.k)
        sections.append(render(report))
        records.append(report.as_record())
        summary.append(f"{mode.value}: Recall@{args.k} {report.overall.recall:.3f} "
                       f"over {report.overall.n} evidence cases")  # fmt: skip
    text = "\n\n".join(sections)
    print(text)
    print("\n" + "\n".join(summary))
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    if args.record:
        args.record.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
