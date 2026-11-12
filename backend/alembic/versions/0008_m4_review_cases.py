"""M4 S7: open human-review cases (insert-only).

Revision ID: 0008_m4_review_cases
Revises: 0007_m3_checkpointer
Create Date: 2026-11-12

- `review_cases`: one row per (report, requirement, reason), written in the same transaction
  as its report (`PostgresReportStore.save`) and derived from the report's review flags
  (`app.graph.review`). `status` can only be `open`: M4 has no reviewer decision, approval,
  rejection, resolution or dismissal (decision D5; they need authentication later).
- INSERT-ONLY: a trigger rejects every UPDATE. Rows go only by cascade: deleting the report
  or the session (retention phase 2) deletes them.
- `claim_id` is nullable and always NULL in M4 (requirement-level cases).

The downgrade drops the table and the trigger function.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_m4_review_cases"
down_revision: str | Sequence[str] | None = "0007_m3_checkpointer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASONS = (
    "'contradicted', 'conflicting_sources', 'unsupported', 'mandatory_not_established', "
    "'explanation_overstates_source', 'not_verified'"
)

TRIGGER_FUNCTION = """
CREATE FUNCTION review_cases_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'review cases are insert-only (M4): % cannot be updated', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$
"""
TRIGGER = """
CREATE TRIGGER trg_review_cases_insert_only BEFORE UPDATE ON review_cases
FOR EACH ROW EXECUTE FUNCTION review_cases_reject_update()
"""


def upgrade() -> None:
    op.create_table(
        "review_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kyc_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("requirement_id", sa.Text(), nullable=False),
        sa.Column("claim_id", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"reason IN ({REASONS})", name="ck_review_cases_reason"),
        sa.CheckConstraint("status = 'open'", name="ck_review_cases_open_only"),
        sa.UniqueConstraint(
            "report_id", "requirement_id", "reason", name="uq_review_cases_report_req_reason"
        ),
    )
    op.create_index("ix_review_cases_report", "review_cases", ["report_id"])
    op.execute(TRIGGER_FUNCTION)
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_review_cases_insert_only ON review_cases")
    op.execute("DROP FUNCTION IF EXISTS review_cases_reject_update()")
    op.drop_index("ix_review_cases_report", table_name="review_cases")
    op.drop_table("review_cases")
