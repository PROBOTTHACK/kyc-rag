"""Workflow maintenance CLI (M3 S8). Run from `backend/`: `python -m app.workflow.cli <command>`.

  purge [--dry-run]   the two-phase retention purge, orphan checkpoint cleanup and the
                      idempotency TTL (make purge / make purge-dry). --dry-run writes
                      nothing (one read-only transaction) and prints what would happen.
  check               read-only integrity report (make check-workflow): orphan checkpoint
                      data in every `langgraph` table, residue left in purged sessions,
                      pending purges, running runs with expired leases.

Output is JSON with counts and ids only. Exit codes: 0 ok; 1 a session could not be
purged (rolled back; the next purge retries it) or `check` found a problem; 2 the
checkpoint schema is not as expected or the database is unreachable (nothing written).
"""

import argparse
import json
import sys

from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.workflow.retention import RetentionError, integrity, purge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workflow.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("purge", help="retention purge (M3_PLAN §10)")
    p.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    sub.add_parser("check", help="read-only workflow integrity check")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_sync_engine(settings)
    sessions = create_sync_sessionmaker(engine)
    try:
        if args.command == "purge":
            report = purge(sessions, settings, dry_run=args.dry_run)
            print(json.dumps(report.as_dict(), indent=2))
            return 1 if report.failed else 0
        with sessions() as db, db.begin():
            result = integrity(db, settings)
        print(json.dumps(result.as_dict(), indent=2))
        return 1 if result.problems else 0
    except RetentionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OperationalError as exc:
        print(f"error: database unavailable ({type(exc.orig).__name__})", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
