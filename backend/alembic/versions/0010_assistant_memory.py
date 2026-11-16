"""Assistant memory: a rolling conversation summary on each session (additive).

Revision ID: 0010_assistant_memory
Revises: 0009_a5_assistant
Create Date: 2026-11-14

Two nullable columns on `assistant_sessions`:
- `summary`: the rolling summary of turns older than the recent window (JSONB; structured:
  confirmed / corrected / unconfirmed facts, open question, other scenarios, earlier
  questions). Built from the dialogue state and stored turns, never from a blocked message.
- `summary_through_turn`: the last turn the summary covers.

Existing rows get NULL (no summary yet); it is created on the next turn that needs it. Both
are deleted with their session row, so a deleted conversation leaves no summary behind.
The downgrade drops the two columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_assistant_memory"
down_revision: str | Sequence[str] | None = "0009_a5_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assistant_sessions", sa.Column("summary", postgresql.JSONB()))
    op.add_column("assistant_sessions", sa.Column("summary_through_turn", sa.Integer()))


def downgrade() -> None:
    op.drop_column("assistant_sessions", "summary_through_turn")
    op.drop_column("assistant_sessions", "summary")
