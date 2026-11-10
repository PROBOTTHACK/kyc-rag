"""M2 C2 follow-up: migration 0004 adds exactly two constraints and is reversible.

Upgrade 0003 -> 0004 keeps every M1 row and every stored form schema byte-identical and
adds only `ck_form_schemas_json_identity` and `ck_form_schemas_field_structure`; the
downgrade removes only those two (0003's constraints, the table and its rows stay); the
re-upgrade restores them. Uses its own throwaway database.
"""

import json
from typing import Any

import pytest
import sqlalchemy as sa

from alembic import command
from app.db.corpus import document_row
from app.db.forms import form_schema_row
from tests.integration.conftest import alembic_cfg, sync_engine
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

pytestmark = pytest.mark.integration
NEW = {"ck_form_schemas_json_identity", "ck_form_schemas_field_structure"}
FROM_0003 = {
    "ck_form_schemas_document_role",
    "ck_form_schemas_evidence_eligibility",
    "ck_form_schemas_extraction_method",
    "ck_form_schemas_extractor_version",
    "ck_form_schemas_document_sha256",
    "ck_form_schemas_schema_sha256",
    "ck_form_schemas_counts",
    "ck_form_schemas_no_policy_obligation",
    "ck_form_schemas_fields_appear_on_form",
    "ck_form_schemas_json_matches_columns",
}
TABLES = (
    "documents",
    "document_pages",
    "chunks",
    "document_exclusions",
    "ingestion_runs",
    "form_schemas",
)


def checks(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'form_schemas'::regclass "
                    "AND contype = 'c'"
                )
            ).scalars()
        )


def snapshot(engine: sa.Engine) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with engine.connect() as conn:
        for t in TABLES:  # fixed table names, never input
            query = sa.text(f'SELECT * FROM "{t}"')  # noqa: S608
            out[t] = sorted(tuple(str(v) for v in r) for r in conn.execute(query))
    return out


def revision(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar())


def insert_missing_appears(engine: sa.Engine, base: dict[str, Any]) -> None:
    """A row whose first field lacks `appears_on_form` (accepted by 0003, not by 0004)."""
    data = json.loads(json.dumps(base["schema_json"]))
    data["extractor_version"] = 7
    data["sections"][0]["fields"][0].pop("appears_on_form")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO form_schemas (id, document_id, form_key, document_sha256, "
                "document_role, evidence_eligibility, extraction_method, extractor_version, "
                "schema_sha256, schema_json, section_count, field_count, required_as_shown_count, "
                "text_block_count, quality_flags) VALUES (gen_random_uuid(), :doc, :key, :sha, "
                ":role, :elig, :method, 7, :ssha, CAST(:j AS jsonb), 1, 2, 1, 0, '{}')"
            ),
            {
                "doc": base["document_id"],
                "key": base["form_key"],
                "sha": base["document_sha256"],
                "role": base["document_role"],
                "elig": base["evidence_eligibility"],
                "method": base["extraction_method"],
                "ssha": base["schema_sha256"],
                "j": json.dumps(data),
            },
        )


def test_0004_adds_only_two_constraints_and_is_reversible(temp_db: str) -> None:
    cfg = alembic_cfg()
    engine = sync_engine(temp_db)
    command.upgrade(cfg, "0003_m2_form_schemas")
    meta = form_doc()
    row = form_schema_row(
        schema(meta, [rec(meta, 0, "Fields: *Name | Surname", section="S", kind=FIELDS)])
    )
    base = {
        c: getattr(row, c)
        for c in (
            "document_id",
            "form_key",
            "document_sha256",
            "document_role",
            "evidence_eligibility",
            "extraction_method",
            "schema_sha256",
            "schema_json",
        )
    }
    with sa.orm.Session(engine) as s:
        s.add(document_row(meta, quality="text", page_count=1))
        s.flush()
        s.add(row)
        s.commit()
    before = snapshot(engine)
    assert checks(engine) == FROM_0003

    command.upgrade(cfg, "0004_m2_form_schema_guards")
    assert revision(engine) == "0004_m2_form_schema_guards"
    assert checks(engine) == FROM_0003 | NEW  # exactly two added
    assert snapshot(engine) == before  # M1 rows and the stored schema untouched
    command.check(cfg)  # ORM and migrations agree at head
    with pytest.raises(sa.exc.IntegrityError, match="ck_form_schemas_field_structure"):
        insert_missing_appears(engine, base)

    command.downgrade(cfg, "0003_m2_form_schemas")
    assert revision(engine) == "0003_m2_form_schemas"
    assert checks(engine) == FROM_0003  # only the two removed; 0003 intact
    assert snapshot(engine) == before  # table and rows preserved
    insert_missing_appears(engine, base)  # the 0003-only gap, demonstrating the removal
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM form_schemas WHERE extractor_version = 7"))
    assert snapshot(engine) == before

    command.upgrade(cfg, "head")
    assert checks(engine) == FROM_0003 | NEW and snapshot(engine) == before
    with pytest.raises(sa.exc.IntegrityError, match="ck_form_schemas_field_structure"):
        insert_missing_appears(engine, base)
