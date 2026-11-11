"""M3 S8: retention reports and CLI exit codes (no database; the purge itself is covered
against Postgres in tests/integration/test_workflow_retention.py)."""

import json
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.workflow import cli
from app.workflow.retention import (
    EXPECTED_TABLES,
    IntegrityReport,
    PurgeReport,
    RetentionError,
    Skip,
    _q,
)


def test_purge_report_failure_only_on_errors() -> None:
    report = PurgeReport(dry_run=False, skipped=[Skip("a", "live_lease"), Skip("b", "locked")])
    assert not report.failed  # safe skips are retried later; not failures
    report.skipped.append(Skip("c", "error:RetentionError"))
    assert report.failed
    data = report.as_dict()
    assert data["skipped"][0] == {"session_id": "a", "reason": "live_lease"}
    assert set(data) == {
        "dry_run",
        "phase1_sessions",
        "phase2_sessions",
        "skipped",
        "orphan_threads",
        "idempotency_pruned",
        "checkpoint_rows_deleted",
    }


def test_integrity_problems() -> None:
    assert not IntegrityReport(pending_phase1=3, stale_leases=1).problems  # informational
    assert IntegrityReport(orphans={"t": {"checkpoints": 1}}).problems
    assert IntegrityReport(residue={"s": 2}).problems
    assert IntegrityReport().as_dict()["ok"] is True


def test_expected_tables_and_quoting() -> None:
    assert {"checkpoints", "checkpoint_blobs", "checkpoint_writes"} == EXPECTED_TABLES
    assert _q("checkpoints") == 'langgraph."checkpoints"'


class Boom:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __call__(self, *a: Any, **k: Any) -> Any:
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [RetentionError("schema"), OperationalError("SELECT 1", {}, ConnectionError("refused"))],
    ids=["schema", "database"],
)
def test_cli_exits_2_when_nothing_could_be_done(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: BaseException
) -> None:
    monkeypatch.setattr(cli, "purge", Boom(exc))
    assert cli.main(["purge", "--dry-run"]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "purge", lambda *a, **k: PurgeReport(dry_run=True))
    assert cli.main(["purge", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    failed = PurgeReport(dry_run=False, skipped=[Skip("s", "error:db")])
    monkeypatch.setattr(cli, "purge", lambda *a, **k: failed)
    assert cli.main(["purge"]) == 1
