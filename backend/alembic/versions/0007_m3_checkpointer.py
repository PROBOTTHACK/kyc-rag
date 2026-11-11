"""M3 S6: LangGraph Postgres checkpointer tables in a dedicated `langgraph` schema.

Decision D3: the library's own `PostgresSaver.setup()` creates its tables
(`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`), so
`make migrate` stays the single schema path while the table layout stays the library's.
`setup()` needs its own autocommit connection, so it runs on a separate psycopg
connection whose `search_path` is `langgraph` (the library uses unqualified names).
That step is idempotent; the schema is not part of the ORM, and `check-schema` compares
only the default schema.

The downgrade refuses while any checkpoint exists (resumable workflow state would be
lost), like 0005; otherwise it drops the schema.

Revision ID: 0007_m3_checkpointer
Revises: 0006_m3_workflow
Create Date: 2026-11-11

"""

from collections.abc import Sequence

import psycopg
import sqlalchemy as sa
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from alembic import op
from app.config import get_settings

revision: str = "0007_m3_checkpointer"
down_revision: str | Sequence[str] | None = "0006_m3_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "langgraph"


def libpq_url() -> str:
    """The same database Alembic migrates, as a plain libpq URL (secret never logged)."""
    url = make_url(get_settings().DATABASE_URL.get_secret_value())
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def upgrade() -> None:
    with psycopg.connect(libpq_url(), autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    with psycopg.connect(
        libpq_url(),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={SCHEMA}",
    ) as conn:
        PostgresSaver(conn).setup()  # type: ignore[arg-type]


def downgrade() -> None:
    bind = op.get_bind()
    exists = bind.scalar(
        sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"{SCHEMA}.checkpoints"}
    )
    if exists:
        rows = bind.scalar(sa.text(f"SELECT count(*) FROM {SCHEMA}.checkpoints"))  # noqa: S608
        if rows:
            raise RuntimeError(
                f"refusing to downgrade 0007: {rows} checkpoint row(s) hold resumable workflow "
                "state; purge them deliberately first"
            )
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
