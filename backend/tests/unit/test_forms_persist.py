"""M2 C2: form-schema persistence helpers (no database; live tests in
tests/integration/test_forms_persist_live.py)."""

import json

import pytest

from app.config import get_settings
from app.db.forms import canonical_json, form_schema_id, form_schema_row, schema_sha256
from app.ingestion import cli
from app.schemas.forms import FormSchema
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema


def sample() -> FormSchema:
    d = form_doc()
    return schema(
        d,
        [
            rec(d, 0, "Heading", section="A) DETAILS"),
            rec(d, 1, "Fields: *Name | Surname | Email *ID", section="A) DETAILS", kind=FIELDS),
        ],
    )


def test_identity_is_derived_from_hash_method_and_version() -> None:
    a = form_schema_id("a" * 64, "m1_chunks", 1)
    assert a == form_schema_id("a" * 64, "m1_chunks", 1)  # deterministic
    assert a != form_schema_id("a" * 64, "m1_chunks", 2)  # new extractor version
    assert a != form_schema_id("b" * 64, "m1_chunks", 1)  # new file


def test_canonical_hash_is_stable_and_content_sensitive() -> None:
    s = sample()
    assert canonical_json(s) == canonical_json(FormSchema.model_validate(s.model_dump()))
    assert json.loads(canonical_json(s)) == s.model_dump(mode="json")
    changed = s.model_copy(update={"title": "Other"})
    assert schema_sha256(s) != schema_sha256(changed)
    assert len(schema_sha256(s)) == 64


def test_row_preserves_provenance_quality_and_distinctions() -> None:
    s = sample()
    row = form_schema_row(s)
    assert row.id == form_schema_id(s.document_sha256, "m1_chunks", 1)
    assert (row.form_key, row.document_sha256, row.document_role) == (
        s.form_key,
        s.document_sha256,
        "kyc_form",
    )
    assert (row.section_count, row.field_count, row.required_as_shown_count) == (1, 3, 1)
    assert row.quality_flags == s.flags and "ambiguous_required_markers" in row.quality_flags
    back = FormSchema.model_validate(row.schema_json)
    assert back == s  # the full schema round-trips: pages, chunk ids, positions, statuses
    field = back.sections[0].fields[2]
    assert (field.printed_label, field.required_as_shown, field.marker_issue) == (
        "Email *ID",
        None,
        "asterisk not at the start of the label",
    )
    assert all(
        f.appears_on_form and f.mandatory_under_policy is None for f in back.sections[0].fields
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["forms", "extract"], ("extract", [])),
        (["forms", "extract", "--document", "a", "--document", "b"], ("extract", ["a", "b"])),
        (["forms", "list"], ("list", None)),
        (["forms", "show", "k", "--json", "--version", "2"], ("show", None)),
    ],
)
def test_forms_cli_parses(argv: list[str], expected: tuple[str, list[str] | None]) -> None:
    args = cli.build_parser(get_settings()).parse_args(argv)
    assert args.forms_command == expected[0]
    if expected[1] is not None:
        assert args.document == expected[1]
    if expected[0] == "show":
        assert (args.key, args.json, args.version) == ("k", True, 2)


def test_forms_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser(get_settings()).parse_args(["forms"])
