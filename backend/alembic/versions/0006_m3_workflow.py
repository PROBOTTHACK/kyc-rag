"""M3 S5: workflow tables (sessions, graph runs, reports, idempotency).

Additive (M3_PLAN §11; decision S5-D4: all four tables now; `graph_runs` and
`api_idempotency` are used from S6):

- `kyc_sessions`: status (active | expired), data_class (synthetic_demo | unspecified).
- `graph_runs`: run lifecycle (running | awaiting_clarification | completed | failed |
  abandoned) and the Postgres run lease; a partial unique index allows one active run per
  session (decision D6).
- `reports`: one per run (UNIQUE(session_id, run_id)); outcome restricted to the five
  report outcomes (no value can express approval or rejection); `human_review_required`
  must be true; the JSON's outcome / report_id / human_review_required must equal the
  columns. INSERT-ONLY: a trigger rejects every UPDATE (DELETE stays possible for the S8
  retention purge).
- `api_idempotency`: (session_id, key) with the screened-request hash.

Allowed values are snapshotted here (docs/15: migrations own their value lists).
The downgrade drops the four tables and the trigger function.

Revision ID: 0006_m3_workflow
Revises: 0005_m2_gemini_method
Create Date: 2026-11-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_m3_workflow"
down_revision: str | Sequence[str] | None = "0005_m2_gemini_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = (
    "'BLOCKED', 'INSUFFICIENT_INFORMATION', 'INSUFFICIENT_EVIDENCE', 'DEGRADED', "
    "'CHECKLIST_REVIEW_REQUIRED'"
)
RUN_STATUSES = "'running', 'awaiting_clarification', 'completed', 'failed', 'abandoned'"

TRIGGER_FUNCTION = """
CREATE FUNCTION reports_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'reports are insert-only (M3): % cannot be updated', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$
"""
TRIGGER = """
CREATE TRIGGER trg_reports_insert_only BEFORE UPDATE ON reports
FOR EACH ROW EXECUTE FUNCTION reports_reject_update()
"""


def upgrade() -> None:
    op.create_table(
        "kyc_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("data_class", sa.Text(), nullable=False, server_default=sa.text("'unspecified'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkpoints_purged_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active', 'expired')", name="ck_kyc_sessions_status"),
        sa.CheckConstraint(
            "data_class IN ('synthetic_demo', 'unspecified')", name="ck_kyc_sessions_data_class"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_kyc_sessions_expiry"),
    )
    op.create_table(
        "graph_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kyc_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "node_events",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "llm_calls", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("retries", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"status IN ({RUN_STATUSES})", name="ck_graph_runs_status"),
        sa.CheckConstraint("retries >= 0", name="ck_graph_runs_retries"),
    )
    op.create_index(
        "uq_graph_runs_active",
        "graph_runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'awaiting_clarification')"),
    )
    op.create_index("ix_graph_runs_session", "graph_runs", ["session_id", "started_at"])
    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kyc_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "human_review_required", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("evidence_status", sa.Text()),
        sa.Column("report_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name="ck_reports_outcome"),
        sa.CheckConstraint("human_review_required IS TRUE", name="ck_reports_human_review"),
        sa.CheckConstraint(
            "evidence_status IS NULL OR evidence_status IN "
            "('found', 'partial', 'none', 'retrieval_failed')",
            name="ck_reports_evidence_status",
        ),
        sa.CheckConstraint(
            "report_json->>'outcome' IS NOT DISTINCT FROM outcome "
            "AND report_json->>'report_id' IS NOT DISTINCT FROM id::text "
            "AND (report_json->'human_review_required') = 'true'::jsonb",
            name="ck_reports_json_identity",
        ),
        sa.UniqueConstraint("session_id", "run_id", name="uq_reports_session_run"),
    )
    op.execute(TRIGGER_FUNCTION)
    op.execute(TRIGGER)
    op.create_table(
        "api_idempotency",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kyc_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_api_idempotency_sha256"),
        sa.CheckConstraint(
            "endpoint IN ('messages', 'resume', 'retry')", name="ck_api_idempotency_endpoint"
        ),
    )


def downgrade() -> None:
    op.drop_table("api_idempotency")
    op.execute("DROP TRIGGER IF EXISTS trg_reports_insert_only ON reports")
    op.execute("DROP FUNCTION IF EXISTS reports_reject_update()")
    op.drop_table("reports")
    op.drop_index("ix_graph_runs_session", table_name="graph_runs")
    op.drop_index("uq_graph_runs_active", table_name="graph_runs")
    op.drop_table("graph_runs")
    op.drop_table("kyc_sessions")
