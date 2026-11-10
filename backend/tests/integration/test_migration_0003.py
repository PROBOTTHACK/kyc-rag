"""M2 C2: migration 0003 (`form_schemas`) is additive and reversible without touching M1.

Each test uses its own throwaway database (`temp_db`). The M1 rows written before the
upgrade must be byte-identical after upgrade, downgrade and re-upgrade.
"""

from typing import Any

import pytest
import sqlalchemy as sa

from alembic import command
from app.db.corpus import document_row
from app.db.forms import form_schema_row
from app.db.models import Document
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

pytestmark = pytest.mark.integration
M1_TABLES = ("documents", "document_pages", "chunks", "document_exclusions", "ingestion_runs")


def snapshot(engine: sa.Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            t: sorted(
                tuple(str(v) for v in row)
                for row in conn.execute(sa.text(f'SELECT * FROM "{t}"'))  # noqa: S608 - fixed names
            )
            for t in M1_TABLES
        }


def tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_0003_is_additive_and_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    engine = sync_engine(temp_db)
    command.upgrade(cfg, "0002_m1_persistence")
    assert "form_schemas" not in tables(engine)
    meta = form_doc()
    with sa.orm.Session(engine) as s:  # an M1 document row written before 0003
        s.add(document_row(meta, quality="text", page_count=1))
        s.commit()
    before = snapshot(engine)

    command.upgrade(cfg, "0003_m2_form_schemas")
    assert "form_schemas" in tables(engine) and snapshot(engine) == before
    with sa.orm.Session(engine) as s:
        doc = s.get(Document, meta.row_id)
        assert doc is not None
        s.add(
            form_schema_row(schema(meta, [rec(meta, 0, "Fields: *Name", section="S", kind=FIELDS)]))
        )
        s.commit()

    command.downgrade(cfg, "0002_m1_persistence")
    assert "form_schemas" not in tables(engine) and snapshot(engine) == before
    command.upgrade(cfg, "head")
    assert "form_schemas" in tables(engine) and snapshot(engine) == before
    command.check(cfg)  # ORM and migrations agree (at head: 0003 is no longer the head)
    with engine.connect() as conn:  # the downgrade dropped the schemas; nothing resurrected
        assert conn.execute(sa.text("SELECT count(*) FROM form_schemas")).scalar() == 0


def test_unique_identity_and_cascade(temp_db: str) -> None:
    command.upgrade(alembic_cfg(), "head")
    engine = sync_engine(temp_db)
    meta = form_doc()
    with sa.orm.Session(engine) as s:
        s.add(document_row(meta, quality="text", page_count=1))
        s.commit()
        row = form_schema_row(schema(meta, [rec(meta, 0, "Fields: A", section="S", kind=FIELDS)]))
        s.add(row)
        s.commit()
        dup = form_schema_row(schema(meta, [rec(meta, 0, "Fields: B", section="S", kind=FIELDS)]))
        dup.id = sa.func.gen_random_uuid()  # same (document, method, version), new id
        s.add(dup)
        with pytest.raises(sa.exc.IntegrityError, match="uq_form_schemas_document_method_version"):
            s.commit()
        s.rollback()
        s.execute(sa.delete(Document).where(Document.id == meta.row_id))
        s.commit()
        assert s.execute(sa.text("SELECT count(*) FROM form_schemas")).scalar() == 0
