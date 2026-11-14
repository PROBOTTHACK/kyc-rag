"""A5: assistant sessions, turns and idempotency keys (additive; existing tables untouched).

Revision ID: 0009_a5_assistant
Revises: 0008_m4_review_cases
Create Date: 2026-11-14

- `assistant_sessions`: the A3 dialogue state (enum codes only) and a sliding expiry.
- `assistant_turns`: one row per turn. INSERT-ONLY: a trigger rejects every UPDATE; rows go
  only by cascade with their session. The message is stored only after screening (NULL when
  screening refused it; only the categories are kept).
- `assistant_idempotency`: (session_id, key) -> the turn a replay returns.
- `assistant_turns.answer` is JSON, not JSONB: JSONB reorders keys, and a replayed answer
  must be byte-identical to the first response.

The downgrade drops the three tables and the trigger function.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_a5_assistant"
down_revision: str | Sequence[str] | None = "0008_m4_review_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_FUNCTION = """
CREATE FUNCTION assistant_turns_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'assistant turns are insert-only (A5): % cannot be updated', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$
"""
TRIGGER = """
CREATE TRIGGER trg_assistant_turns_insert_only BEFORE UPDATE ON assistant_turns
FOR EACH ROW EXECUTE FUNCTION assistant_turns_reject_update()
"""


def _timestamp(name: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "assistant_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("turn_count >= 0", name="ck_assistant_sessions_turn_count"),
    )
    op.create_table(
        "assistant_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assistant_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_no", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column(
            "screen_categories",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("answer", postgresql.JSON(), nullable=False),  # JSON keeps the bytes
        _timestamp("created_at"),
        sa.CheckConstraint("turn_no >= 0", name="ck_assistant_turns_turn_no"),
        sa.UniqueConstraint("session_id", "turn_no", name="uq_assistant_turns_session_turn"),
    )
    op.create_table(
        "assistant_idempotency",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assistant_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assistant_turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_assistant_idempotency_sha256"
        ),
        sa.CheckConstraint("length(key) BETWEEN 1 AND 200", name="ck_assistant_idempotency_key"),
    )
    op.execute(TRIGGER_FUNCTION)
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_assistant_turns_insert_only ON assistant_turns")
    op.execute("DROP FUNCTION IF EXISTS assistant_turns_reject_update()")
    op.drop_table("assistant_idempotency")
    op.drop_table("assistant_turns")
    op.drop_table("assistant_sessions")
