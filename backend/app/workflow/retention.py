"""Workflow retention: the two-phase purge, orphan cleanup and an integrity check (M3 S8;
M3_PLAN §10). Run by the CLI (`make purge`, `make purge-dry`, `make check-workflow`);
there is no periodic worker.

- **Phase 1, at `expires_at`:** the session becomes `expired` (every write is `410`), its
  non-terminal runs become `abandoned`, its idempotency rows and ALL of its LangGraph
  checkpoint data are deleted, and `checkpoints_purged_at` is set. Reports, `graph_runs`
  and the SSE replay stay readable.
- **Phase 2, at `expires_at + REPORT_RETENTION_D`:** the checkpoint data is deleted and
  verified gone FIRST; only then is the session row deleted, which cascades to its runs,
  reports and idempotency rows (FKs from migration 0006).
- **Orphans:** checkpoint data whose thread has no `graph_runs` row is deleted.
- **Idempotency TTL:** records older than `IDEMPOTENCY_TTL_H` are pruned (S8-D3).
- **Review cases (M4 S7):** they live and die with their report: readable after phase 1,
  deleted by the phase-2 cascade. `check` verifies that every stored report's cases are
  exactly those its review flags imply, and that every case is still `open`.

Safety (owner review of the S8 plan):
- The plan is advisory. Every destructive step re-reads and LOCKS the current state in
  the same transaction that deletes: `kyc_sessions ... FOR UPDATE` (blocks a new run being
  inserted for the session: the FK takes a KEY SHARE lock) and `graph_runs ... FOR UPDATE`
  (blocks lease renewal, claim, node events and finish on those rows). A session with a
  `running` run whose lease is still valid is skipped (`live_lease`), so checkpoint data is
  never deleted under a live writer. A session whose state changed since planning is
  skipped or handled by its CURRENT phase; every skip carries a reason.
- Checkpoint data is deleted with plain SQL inside that same transaction (the same three
  DELETEs as `AsyncPostgresSaver.adelete_thread`, langgraph-checkpoint-postgres 3.2.0),
  so the lock, the deletion, the verification and the session change commit together or
  not at all. The tables are discovered from the live `langgraph` schema: every table
  with a `thread_id` column (checkpoints, checkpoint_blobs, checkpoint_writes), so a
  library upgrade that adds one is covered; the expected three must be present.
- A failure (lock timeout, failed verification, any error) rolls the session back
  untouched, is reported, and the next purge retries it. Running twice is a no-op.
- A writer whose lease had already expired but that is somehow still alive could write
  checkpoint rows AFTER phase 1. `check` reports such residue and the next purge removes
  it (phase 1 re-selects expired sessions with residue).
Logs and output carry counts and ids only, never content.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

log = logging.getLogger(__name__)
CHECKPOINT_SCHEMA = "langgraph"
EXPECTED_TABLES = frozenset({"checkpoints", "checkpoint_blobs", "checkpoint_writes"})
LOCK_TIMEOUT = "5s"


class RetentionError(RuntimeError):
    """The checkpoint schema is not what the purge was written for (nothing deleted)."""


@dataclass
class Skip:
    session_id: str
    reason: str


@dataclass
class PurgeReport:
    dry_run: bool
    phase1: list[str] = field(default_factory=list)  # session ids
    phase2: list[str] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    orphan_threads: list[str] = field(default_factory=list)
    idempotency_pruned: int = 0
    checkpoint_rows: dict[str, int] = field(default_factory=dict)  # per table (deleted)

    @property
    def failed(self) -> bool:
        return any(s.reason.startswith("error:") for s in self.skipped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "phase1_sessions": self.phase1,
            "phase2_sessions": self.phase2,
            "skipped": [{"session_id": s.session_id, "reason": s.reason} for s in self.skipped],
            "orphan_threads": self.orphan_threads,
            "idempotency_pruned": self.idempotency_pruned,
            "checkpoint_rows_deleted": self.checkpoint_rows,
        }


@dataclass
class IntegrityReport:
    orphans: dict[str, dict[str, int]] = field(default_factory=dict)  # thread -> table -> rows
    residue: dict[str, int] = field(default_factory=dict)  # purged session -> leftover rows
    pending_phase1: int = 0
    pending_phase2: int = 0
    stale_leases: int = 0
    review_cases: int = 0
    review_case_problems: list[str] = field(default_factory=list)  # codes with ids only

    @property
    def problems(self) -> bool:
        return bool(self.orphans or self.residue or self.review_case_problems)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.problems,
            "orphan_threads": self.orphans,
            "purged_sessions_with_checkpoint_residue": self.residue,
            "pending_phase1_sessions": self.pending_phase1,
            "pending_phase2_sessions": self.pending_phase2,
            "running_runs_with_expired_lease": self.stale_leases,
            "review_cases": self.review_cases,
            "review_case_problems": self.review_case_problems,
        }


# --- the live checkpoint schema ----------------------------------------------------------------


def checkpoint_tables(db: Session) -> list[str]:
    """Every `langgraph` table holding per-thread data (a `thread_id` column)."""
    tables = sorted(
        str(t)
        for t in db.execute(
            sa.text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND column_name = 'thread_id'"
            ),
            {"schema": CHECKPOINT_SCHEMA},
        ).scalars()
    )
    missing = EXPECTED_TABLES - set(tables)
    if missing:
        raise RetentionError(f"{CHECKPOINT_SCHEMA} tables missing: {sorted(missing)}")
    return tables


def _q(table: str) -> str:
    return f'{CHECKPOINT_SCHEMA}."{table}"'  # names come from information_schema only


def _thread_rows(db: Session, tables: list[str], threads: list[str]) -> dict[str, int]:
    if not threads:
        return {t: 0 for t in tables}
    return {
        t: int(
            db.execute(
                sa.text(f"SELECT count(*) FROM {_q(t)} WHERE thread_id = ANY(:th)"),  # noqa: S608
                {"th": threads},
            ).scalar_one()
        )
        for t in tables
    }


def _delete_threads(db: Session, tables: list[str], threads: list[str]) -> dict[str, int]:
    """Delete, then verify in the same transaction (raises: the caller rolls back)."""
    deleted: dict[str, int] = {}
    if threads:
        for t in tables:
            result = db.execute(
                sa.text(f"DELETE FROM {_q(t)} WHERE thread_id = ANY(:th)"),  # noqa: S608
                {"th": threads},
            )
            deleted[t] = int(result.rowcount)  # type: ignore[attr-defined]
    left = _thread_rows(db, tables, threads)
    if any(left.values()):
        raise RetentionError("checkpoint data still present after delete")
    return deleted


def _residue_sql(tables: list[str], session_ref: str) -> str:
    """SQL predicate: the session's threads still have checkpoint data."""
    parts = [
        f"EXISTS (SELECT 1 FROM {_q(t)} c JOIN graph_runs r ON r.thread_id = c.thread_id "  # noqa: S608
        f"WHERE r.session_id = {session_ref})"
        for t in tables
    ]
    return "(" + " OR ".join(parts) + ")"


def orphan_threads(db: Session, tables: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in tables:
        rows = db.execute(
            sa.text(
                f"SELECT c.thread_id, count(*) FROM {_q(t)} c "  # noqa: S608
                "WHERE NOT EXISTS (SELECT 1 FROM graph_runs r WHERE r.thread_id = c.thread_id) "
                "GROUP BY c.thread_id"
            )
        )
        for thread, n in rows:
            out.setdefault(str(thread), {})[t] = int(n)
    return out


# --- planning (read-only) ----------------------------------------------------------------------


def _candidates(db: Session, tables: list[str], retention_d: int) -> tuple[list[str], list[str]]:
    params = {"days": retention_d}
    phase2 = [
        str(r)
        for r in db.execute(
            sa.text(
                "SELECT id FROM kyc_sessions "
                "WHERE expires_at + make_interval(days => :days) <= now() ORDER BY expires_at, id"
            ),
            params,
        ).scalars()
    ]
    phase1 = [
        str(r)
        for r in db.execute(
            sa.text(
                "SELECT s.id FROM kyc_sessions s WHERE s.expires_at <= now() "  # noqa: S608
                "AND s.expires_at + make_interval(days => :days) > now() "
                f"AND (s.checkpoints_purged_at IS NULL OR {_residue_sql(tables, 's.id')}) "
                "ORDER BY s.expires_at, s.id"
            ),
            params,
        ).scalars()
    ]
    return phase1, phase2


def _stale_idempotency(db: Session, ttl_h: float) -> int:
    return int(
        db.execute(
            sa.text(
                "SELECT count(*) FROM api_idempotency "
                "WHERE created_at < now() - make_interval(secs => :s)"
            ),
            {"s": ttl_h * 3600},
        ).scalar_one()
    )


def integrity(db: Session, settings: Settings) -> IntegrityReport:
    """Read-only (`make check-workflow`)."""
    tables = checkpoint_tables(db)
    phase1, phase2 = _candidates(db, tables, settings.REPORT_RETENTION_D)
    residue: dict[str, int] = {}
    purged = db.execute(
        sa.text("SELECT id FROM kyc_sessions WHERE checkpoints_purged_at IS NOT NULL")
    ).scalars()
    for sid in purged:
        threads = _session_threads(db, sid)
        n = sum(_thread_rows(db, tables, threads).values())
        if n:
            residue[str(sid)] = n
    stale = int(
        db.execute(
            sa.text(
                "SELECT count(*) FROM graph_runs WHERE status = 'running' "
                "AND lease_expires_at < now()"
            )
        ).scalar_one()
    )
    count, case_problems = review_case_integrity(db)
    return IntegrityReport(
        orphans=orphan_threads(db, tables),
        residue=residue,
        pending_phase1=len(phase1),
        pending_phase2=len(phase2),
        stale_leases=stale,
        review_cases=count,
        review_case_problems=case_problems,
    )


_CASE_FIELDS = ("session_id", "run_id", "requirement_id", "claim_id", "reason", "status")


def review_case_integrity(db: Session) -> tuple[int, list[str]]:
    """Read-only (M4 S7): stored cases must equal the cases each stored report implies
    (`app.graph.review`), field for field, and every case must be `open`."""
    from app.db.workflow import expected_review_cases

    stored: dict[str, dict[str, dict[str, Any]]] = {}
    count = 0
    for row in db.execute(
        sa.text(
            "SELECT id, report_id, session_id, run_id, requirement_id, claim_id, reason, status "
            "FROM review_cases"
        )
    ).mappings():
        count += 1
        stored.setdefault(str(row["report_id"]), {})[str(row["id"])] = {
            k: (str(row[k]) if k == "session_id" else row[k]) for k in _CASE_FIELDS
        }
    problems: list[str] = []
    for case_id in sorted(cid for cases in stored.values() for cid, c in cases.items()
                          if c["status"] != "open"):  # fmt: skip
        problems.append(f"review_case_not_open:{case_id}")
    reports = db.execute(sa.text("SELECT id, report_json FROM reports")).all()
    for report_id, report_json in sorted(reports, key=lambda r: str(r[0])):
        rid = str(report_id)
        have = stored.pop(rid, {})
        try:
            want = {c.id: c for c in expected_review_cases(report_json)}
        except ValueError:
            problems.append(f"report_invalid:{rid}")
            continue
        for case_id in sorted(set(want) - set(have)):
            problems.append(f"review_case_missing:{rid}:{case_id}")
        for case_id in sorted(set(have) - set(want)):
            problems.append(f"review_case_unexpected:{rid}:{case_id}")
        for case_id in sorted(set(have) & set(want)):
            w = want[case_id]
            expected = {"session_id": w.session_id, "run_id": w.run_id,
                        "requirement_id": w.requirement_id, "claim_id": w.claim_id,
                        "reason": w.reason, "status": w.status}  # fmt: skip
            if have[case_id] != expected:
                problems.append(f"review_case_mismatch:{rid}:{case_id}")
    for rid in sorted(stored):  # impossible with the FK; reported if it ever happens
        problems.append(f"review_cases_without_report:{rid}")
    return count, problems


def _session_threads(db: Session, session_id: Any) -> list[str]:
    return [
        str(t)
        for t in db.execute(
            sa.text("SELECT DISTINCT thread_id FROM graph_runs WHERE session_id = :id"),
            {"id": session_id},
        ).scalars()
    ]


# --- execution ---------------------------------------------------------------------------------


def _purge_session(
    db: Session, tables: list[str], session_id: str, retention_d: int
) -> tuple[str, dict[str, int]]:
    """One session, one transaction (the caller commits or rolls back). Returns the phase
    executed (`phase1` / `phase2`) and the checkpoint rows deleted, or raises
    `_SkipSession` with the reason."""
    db.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    row = db.execute(
        sa.text(
            "SELECT expires_at <= now() AS expired, "
            "expires_at + make_interval(days => :days) <= now() AS retention_over "
            "FROM kyc_sessions WHERE id = :id FOR UPDATE"
        ),
        {"id": session_id, "days": retention_d},
    ).first()
    if row is None:
        raise _SkipSession("gone")
    if not row.expired:
        raise _SkipSession("not_expired")
    runs = db.execute(
        sa.text(
            "SELECT id, thread_id, status, coalesce(lease_expires_at > now(), false) AS live "
            "FROM graph_runs WHERE session_id = :id ORDER BY started_at, id FOR UPDATE"
        ),
        {"id": session_id},
    ).all()
    if any(r.status == "running" and r.live for r in runs):
        raise _SkipSession("live_lease")
    threads = sorted({str(r.thread_id) for r in runs})
    deleted = _delete_threads(db, tables, threads)  # verified, same transaction
    if row.retention_over:  # only after the checkpoint data is verified gone
        db.execute(sa.text("DELETE FROM kyc_sessions WHERE id = :id"), {"id": session_id})
        return "phase2", deleted
    db.execute(
        sa.text(
            "UPDATE graph_runs SET status = 'abandoned', owner_id = NULL, "
            "lease_expires_at = NULL, finished_at = coalesce(finished_at, now()) "
            "WHERE session_id = :id AND status IN ('running', 'awaiting_clarification')"
        ),
        {"id": session_id},
    )
    db.execute(sa.text("DELETE FROM api_idempotency WHERE session_id = :id"), {"id": session_id})
    db.execute(
        sa.text(
            "UPDATE kyc_sessions SET status = 'expired', checkpoints_purged_at = now() "
            "WHERE id = :id"
        ),
        {"id": session_id},
    )
    return "phase1", deleted


class _SkipSession(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _add(total: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        total[k] = total.get(k, 0) + v


def _purge_orphans(db: Session, tables: list[str], threads: list[str]) -> dict[str, int]:
    """Delete only threads that are STILL orphans (re-checked in the DELETE)."""
    deleted: dict[str, int] = {}
    for t in tables:
        result = db.execute(
            sa.text(
                f"DELETE FROM {_q(t)} c WHERE c.thread_id = ANY(:th) "  # noqa: S608
                "AND NOT EXISTS (SELECT 1 FROM graph_runs r WHERE r.thread_id = c.thread_id)"
            ),
            {"th": threads},
        )
        deleted[t] = int(result.rowcount)  # type: ignore[attr-defined]
    left = orphan_threads(db, tables)
    if any(th in left for th in threads):
        raise RetentionError("orphan checkpoint data still present after delete")
    return deleted


def purge(sessions: sessionmaker[Session], settings: Settings, *, dry_run: bool) -> PurgeReport:
    report = PurgeReport(dry_run=dry_run)
    with sessions() as db, db.begin():
        db.execute(sa.text("SET TRANSACTION READ ONLY"))
        tables = checkpoint_tables(db)
        phase1, phase2 = _candidates(db, tables, settings.REPORT_RETENTION_D)
        orphans = sorted(orphan_threads(db, tables))
        stale_idem = _stale_idempotency(db, settings.IDEMPOTENCY_TTL_H)
    if dry_run:
        report.phase1, report.phase2 = phase1, phase2
        report.orphan_threads, report.idempotency_pruned = orphans, stale_idem
        return report

    for sid in phase2 + phase1:
        try:
            with sessions() as db, db.begin():
                done, deleted = _purge_session(db, tables, sid, settings.REPORT_RETENTION_D)
            _add(report.checkpoint_rows, deleted)
            (report.phase2 if done == "phase2" else report.phase1).append(sid)
        except _SkipSession as skip:
            report.skipped.append(Skip(sid, skip.reason))
        except DBAPIError as exc:
            reason = "locked" if "lock" in type(exc.orig).__name__.lower() else "error:db"
            report.skipped.append(Skip(sid, reason))
            log.warning("purge skipped session %s: %s", sid, type(exc.orig).__name__)
        except Exception as exc:  # noqa: BLE001 - rolled back; retried by the next purge
            report.skipped.append(Skip(sid, f"error:{type(exc).__name__}"))
            log.warning("purge failed for session %s: %s", sid, type(exc).__name__)

    if orphans:
        try:
            with sessions() as db, db.begin():
                _add(report.checkpoint_rows, _purge_orphans(db, tables, orphans))
            report.orphan_threads = orphans
        except Exception as exc:  # noqa: BLE001 - rolled back; retried by the next purge
            report.skipped.append(Skip("orphans", f"error:{type(exc).__name__}"))

    with sessions() as db, db.begin():
        result = db.execute(
            sa.text(
                "DELETE FROM api_idempotency WHERE created_at < now() - make_interval(secs => :s)"
            ),
            {"s": settings.IDEMPOTENCY_TTL_H * 3600},
        )
        report.idempotency_pruned = int(result.rowcount)  # type: ignore[attr-defined]
    log.info(
        "purge: phase1=%d phase2=%d skipped=%d orphans=%d idempotency=%d",
        len(report.phase1),
        len(report.phase2),
        len(report.skipped),
        len(report.orphan_threads),
        report.idempotency_pruned,
    )
    return report
