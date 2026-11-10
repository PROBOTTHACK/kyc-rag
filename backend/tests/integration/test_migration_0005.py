"""M2 G3: migration 0005 widens only the extraction-method check and is reversible.

Upgrade 0004 -> 0005 keeps M1 rows and stored m1_chunks schemas byte-identical and lets
a `gemini_multimodal` row in; the downgrade refuses while such a row exists (no silent
data loss), then restores the m1_chunks-only check; the re-upgrade allows it again.
"""

import json
from typing import Any

import pytest
import sqlalchemy as sa

from alembic import command
from app.db.corpus import document_row
from app.db.forms import form_schema_row
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.integration.test_migration_0004 import checks, revision, snapshot
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

pytestmark = pytest.mark.integration


def check_def(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_form_schemas_extraction_method'"
                )
            ).scalar()
        )


def insert_gemini(engine: sa.Engine, base: dict[str, Any]) -> None:
    data = json.loads(json.dumps(base["schema_json"]))
    data["extraction_method"] = "gemini_multimodal"
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO form_schemas (id, document_id, form_key, document_sha256, "
                "document_role, evidence_eligibility, extraction_method, extractor_version, "
                "schema_sha256, schema_json, section_count, field_count, required_as_shown_count, "
                "text_block_count, quality_flags) VALUES (gen_random_uuid(), :doc, :key, :sha, "
                ":role, :elig, 'gemini_multimodal', 1, :ssha, CAST(:j AS jsonb), 1, 1, 1, 0, '{}')"
            ),
            {
                "doc": base["document_id"],
                "key": base["form_key"],
                "sha": base["document_sha256"],
                "role": base["document_role"],
                "elig": base["evidence_eligibility"],
                "ssha": base["schema_sha256"],
                "j": json.dumps(data),
            },
        )


def test_0005_widens_only_the_method_check_and_is_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    engine = sync_engine(temp_db)
    command.upgrade(cfg, "0004_m2_form_schema_guards")
    meta = form_doc()
    row = form_schema_row(schema(meta, [rec(meta, 0, "Fields: *Name", section="S", kind=FIELDS)]))
    cols = ("document_id", "form_key", "document_sha256", "document_role")
    base = {c: getattr(row, c) for c in cols}
    base |= {c: getattr(row, c) for c in ("evidence_eligibility", "schema_sha256", "schema_json")}
    with sa.orm.Session(engine) as s:
        s.add(document_row(meta, quality="text", page_count=1))
        s.flush()
        s.add(row)
        s.commit()
    before, names = snapshot(engine), checks(engine)
    with pytest.raises(sa.exc.IntegrityError, match="ck_form_schemas_extraction_method"):
        insert_gemini(engine, base)

    command.upgrade(cfg, "0005_m2_gemini_method")
    assert revision(engine) == "0005_m2_gemini_method"
    assert checks(engine) == names and snapshot(engine) == before  # same names, rows intact
    assert "gemini_multimodal" in check_def(engine)
    command.check(cfg)  # ORM mirrors the widened check
    insert_gemini(engine, base)

    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(cfg, "0004_m2_form_schema_guards")
    assert revision(engine) == "0005_m2_gemini_method"
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM form_schemas WHERE extraction_method <> 'm1_chunks'"))
    command.downgrade(cfg, "0004_m2_form_schema_guards")
    assert checks(engine) == names and snapshot(engine) == before
    assert "gemini_multimodal" not in check_def(engine)
    with pytest.raises(sa.exc.IntegrityError, match="ck_form_schemas_extraction_method"):
        insert_gemini(engine, base)

    command.upgrade(cfg, "head")
    insert_gemini(engine, base)
