"""M3 S6: migration 0007 puts the LangGraph checkpointer tables in the `langgraph` schema.

Up / down (refused while a checkpoint exists) / up on a throwaway database; nothing lands
in `public`; the ORM drift check stays clean (it compares the default schema only).
"""

import pytest
import sqlalchemy as sa

from alembic import command
from tests.integration.conftest import alembic_cfg, sync_engine

pytestmark = pytest.mark.integration
CHECKPOINT_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


def tables(engine: sa.Engine, schema: str) -> set[str]:
    return set(sa.inspect(engine).get_table_names(schema=schema))


def schemas(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_schema_names())


def test_0007_creates_the_checkpointer_schema_and_is_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    engine = sync_engine(temp_db)
    command.upgrade(cfg, "0006_m3_workflow")
    assert "langgraph" not in schemas(engine)
    public_before = tables(engine, "public")

    command.upgrade(cfg, "0007_m3_checkpointer")
    assert tables(engine, "langgraph") == CHECKPOINT_TABLES
    assert tables(engine, "public") == public_before  # nothing leaked into public
    command.check(cfg)

    with engine.begin() as conn:  # a stored checkpoint makes the downgrade refuse
        conn.execute(
            sa.text(
                "INSERT INTO langgraph.checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "checkpoint, metadata) VALUES ('t', '', 'c', '{}'::jsonb, '{}'::jsonb)"
            )
        )
    with pytest.raises(RuntimeError, match="refusing to downgrade 0007"):
        command.downgrade(cfg, "0006_m3_workflow")
    assert "langgraph" in schemas(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM langgraph.checkpoints"))

    command.downgrade(cfg, "0006_m3_workflow")
    assert "langgraph" not in schemas(engine)
    command.upgrade(cfg, "head")
    assert tables(engine, "langgraph") == CHECKPOINT_TABLES
