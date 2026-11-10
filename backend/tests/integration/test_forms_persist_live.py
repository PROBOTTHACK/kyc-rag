"""M2 C2 on real Postgres: persisted, versioned form schemas over the full corpus.

Uses the full-corpus throwaway database (tests/integration/conftest.py). Covers the first
extraction (6 inserted), idempotent repeats (unchanged, no duplicates, no rewrite), a new
extractor version (new row, old kept), a conflict (refused, never overwritten), full
provenance round-trip, the reference/KYC-form distinction, the database guards against
policy obligations, and CLI exit codes.
"""

import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.forms import (
    FormSchemaConflict,
    extract_and_save,
    form_schema_row,
    load_form_schema,
    save_form_schema,
)
from app.db.models import Chunk, Document, FormSchemaRow
from app.forms.extract import extract_form, extractable_document_keys
from app.ingestion import cli
from app.retrieval.qdrant_store import ChunkStore

pytestmark = pytest.mark.integration
REFERENCE = "hdfc_netbanking_corporate_application"
SCANNED = "hdfc_nri_rekyc"
NATIVE = "hdfc_nri_account_opening"


@pytest.fixture(scope="module")
def stored(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    """The corpus with all form schemas extracted once (via the CLI)."""
    assert cli._forms_extract(get_settings(), [], sessions=corpus_db) == 0
    return corpus_db


def rows(sessions: sessionmaker[Session]) -> list[FormSchemaRow]:
    with sessions() as s:
        return list(
            s.scalars(
                sa.select(FormSchemaRow).order_by(
                    FormSchemaRow.form_key, FormSchemaRow.extractor_version
                )
            )
        )


def test_first_extraction_stores_all_six(stored: sessionmaker[Session]) -> None:
    got = rows(stored)
    with stored() as s:
        keys = extractable_document_keys(s)
    assert [r.form_key for r in got] == keys and len(got) == 6
    roles = {r.form_key: (r.document_role, r.evidence_eligibility) for r in got}
    assert roles[REFERENCE] == ("reference_document", "context_only")
    assert {k for k, v in roles.items() if v[0] == "kyc_form"} == set(keys) - {REFERENCE}
    assert all(r.extractor_version == 1 and r.extraction_method == "m1_chunks" for r in got)


def test_repeat_extraction_is_idempotent(
    stored: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    before = [(r.id, r.schema_sha256, r.created_at) for r in rows(stored)]
    capsys.readouterr()
    assert cli._forms_extract(get_settings(), [], sessions=stored) == 0
    out = capsys.readouterr().out
    assert out.count(" unchanged ") == 6 and "inserted" not in out
    assert [(r.id, r.schema_sha256, r.created_at) for r in rows(stored)] == before


def test_stored_schema_round_trips_with_full_provenance(stored: sessionmaker[Session]) -> None:
    with stored() as s:
        for key in (NATIVE, SCANNED, REFERENCE):
            loaded = load_form_schema(s, key)
            fresh = extract_form(s, key)
            assert loaded == fresh  # every page, region, chunk id, position, flag, status
            doc = s.scalar(sa.select(Document).where(Document.document_key == key))
            assert doc is not None
            assert (loaded.document_id, loaded.document_sha256) == (str(doc.id), doc.sha256)
        native = load_form_schema(s, NATIVE)
        assert native is not None
        f = next(f for sec in native.sections for f in sec.fields if f.required_as_shown)
        assert f.printed_label.startswith("*") and f.appears_on_form is True
        assert f.mandatory_under_policy is None and f.chunk_id and f.page_number >= 1
        scanned = load_form_schema(s, SCANNED)
        assert scanned is not None and scanned.field_count == 0
        blocks = [b for sec in scanned.sections for b in sec.text_blocks]
        assert all(
            b.ocr_confidence is not None and b.verification_status.startswith("ocr_")
            for b in blocks
        )
        assert "no_field_structure" in scanned.flags


def test_new_extractor_version_is_a_new_row_and_the_old_one_is_kept(
    stored: sessionmaker[Session],
) -> None:
    with stored() as s:
        v1 = extract_form(s, NATIVE)
        v2 = v1.model_copy(update={"extractor_version": 2})
        out = save_form_schema(s, v2)
        s.commit()
    try:
        assert out.status == "inserted"
        versions = [r.extractor_version for r in rows(stored) if r.form_key == NATIVE]
        assert versions == [1, 2]
        with stored() as s:
            latest = load_form_schema(s, NATIVE)
            first = load_form_schema(s, NATIVE, extractor_version=1)
        assert latest is not None and latest.extractor_version == 2
        assert first == v1
    finally:
        with stored() as s:
            s.execute(
                sa.delete(FormSchemaRow).where(
                    FormSchemaRow.form_key == NATIVE, FormSchemaRow.extractor_version == 2
                )
            )
            s.commit()


def test_changed_chunks_under_the_same_version_are_refused_not_overwritten(
    stored: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    before = {r.form_key: r.schema_sha256 for r in rows(stored)}
    with stored() as s:
        doc = s.scalar(sa.select(Document).where(Document.document_key == NATIVE))
        assert doc is not None
        chunk = s.scalars(
            sa.select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.chunk_index)
        ).first()
        assert chunk is not None
        cid, original = chunk.id, chunk.content
        chunk.content = original + " (edited)"
        s.commit()
    try:
        with stored() as s:
            with pytest.raises(FormSchemaConflict, match="refusing to overwrite"):
                extract_and_save(s, NATIVE)
            s.rollback()
        capsys.readouterr()
        assert cli._forms_extract(get_settings(), [NATIVE], sessions=stored) == 1
        assert "CONFLICT" in capsys.readouterr().out
        assert {r.form_key: r.schema_sha256 for r in rows(stored)} == before  # untouched
    finally:
        with stored() as s:
            row = s.get(Chunk, cid)
            assert row is not None
            row.content = original
            s.commit()
    with stored() as s:
        assert extract_and_save(s, NATIVE).status == "unchanged"  # restored exactly


def _raw_insert(sessions: sessionmaker[Session], schema_json: dict[str, Any], **cols: Any) -> None:
    with sessions() as s:
        base = form_schema_row(extract_form(s, NATIVE))
        values = {
            c.columns[0].name: getattr(base, c.key) for c in sa.inspect(FormSchemaRow).column_attrs
        }
        values |= {"id": uuid.uuid4(), "extractor_version": 99, "schema_json": schema_json} | cols
        values.pop("created_at", None)
        s.execute(sa.insert(FormSchemaRow).values(**values))
        s.commit()


@pytest.mark.parametrize(
    ("mutate", "constraint"),
    [
        (
            lambda j: j["sections"][1]["fields"][0].__setitem__("mandatory_under_policy", True),
            "ck_form_schemas_no_policy_obligation",
        ),
        (
            lambda j: j["sections"][1]["fields"][0].__setitem__("appears_on_form", False),
            "ck_form_schemas_fields_appear_on_form",
        ),
        (
            lambda j: j.__setitem__("document_role", "reference_document"),
            "ck_form_schemas_json_matches_columns",
        ),
    ],
)
def test_database_rejects_policy_obligations_and_mismatches(
    stored: sessionmaker[Session], mutate: Any, constraint: str
) -> None:
    with stored() as s:
        schema = extract_form(s, NATIVE)
    data = json.loads(schema.model_dump_json())
    data["extractor_version"] = 99
    assert data["sections"][1]["fields"], "fixture expects fields in section 1"
    mutate(data)
    with pytest.raises(sa.exc.IntegrityError, match=constraint):
        _raw_insert(stored, data)


def test_schema_for_an_unknown_document_is_refused(stored: sessionmaker[Session]) -> None:
    with stored() as s:
        schema = extract_form(s, NATIVE)
        with pytest.raises(ValueError, match="does not match a stored document"):
            save_form_schema(s, schema.model_copy(update={"document_sha256": "0" * 64}))


def test_cli_exit_codes_and_show(
    stored: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert cli._forms_extract(get_settings(), ["rbi_kyc_directions_2025"], sessions=stored) == 2
    assert "not extractable" in capsys.readouterr().out
    assert (
        cli._forms_show(get_settings(), "nope", as_json=False, version=None, sessions=stored) == 2
    )
    assert (
        cli._forms_show(get_settings(), NATIVE, as_json=False, version=None, sessions=stored) == 0
    )
    out = capsys.readouterr().out
    assert "not policy obligations" in out and "* *Mother's Maiden Name" in out
    assert cli._forms_list(get_settings(), sessions=stored) == 0
    assert REFERENCE in capsys.readouterr().out
