"""M2 C2 follow-up (migration 0004): adversarial raw inserts into `form_schemas`.

Writes bypass the application on purpose (raw SQL), so only the database guards stand
between a malformed schema and the table. Every invalid case must fail with a definite
CHECK violation of the named 0004 constraint; valid shapes must be accepted. Uses a
module-scoped, migrated, otherwise empty throwaway database with one synthetic document.
"""

import itertools
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db.corpus import document_row
from app.db.forms import form_schema_row
from app.db.models import FormSchemaRow
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

pytestmark = pytest.mark.integration
STRUCTURE = "ck_form_schemas_field_structure"
IDENTITY = "ck_form_schemas_json_identity"
_versions = itertools.count(10)


@pytest.fixture(scope="module")
def db(corpus_db: sessionmaker[Session]) -> tuple[sessionmaker[Session], dict[str, Any]]:
    """The migrated (empty) throwaway database plus one document and a valid row template."""
    meta = form_doc("hdfc_guard_test_form")
    valid = schema(
        meta,
        [
            rec(meta, 0, "Heading", section="A) DETAILS"),
            rec(meta, 1, "Fields: *Name | Surname", section="A) DETAILS", kind=FIELDS),
        ],
    )
    with corpus_db() as s:
        s.add(document_row(meta, quality="text", page_count=1))
        s.commit()
    template = form_schema_row(valid)
    cols = {
        c.columns[0].name: getattr(template, c.key) for c in sa.inspect(FormSchemaRow).column_attrs
    }
    cols.pop("created_at")
    return corpus_db, cols


def insert(
    db: tuple[sessionmaker[Session], dict[str, Any]],
    mutate_json: Callable[[dict[str, Any]], Any] | None = None,
    **columns: Any,
) -> None:
    """Insert a copy of the valid row with a fresh version, after `mutate_json`. A mutator
    mutates in place and returns None; only a whole-document replacement returns a value."""
    sessions, base = db
    version = next(_versions)
    data = json.loads(json.dumps(base["schema_json"]))
    data["extractor_version"] = version
    if mutate_json is not None:
        replaced = mutate_json(data)
        data = data if replaced is None else replaced
    values = (
        base | {"id": uuid.uuid4(), "extractor_version": version, "schema_json": data} | columns
    )
    with sessions() as s:
        s.execute(sa.insert(FormSchemaRow).values(**values))
        s.commit()


def field(data: dict[str, Any]) -> dict[str, Any]:
    f: dict[str, Any] = data["sections"][0]["fields"][0]
    return f


def drop(key: str) -> Callable[[dict[str, Any]], None]:
    """Remove `key` from the first field (returns None: an in-place mutation)."""

    def mutate(d: dict[str, Any]) -> None:
        field(d).pop(key)

    return mutate


def drop_top(key: str) -> Callable[[dict[str, Any]], None]:
    """Remove a top-level key (returns None: an in-place mutation)."""

    def mutate(d: dict[str, Any]) -> None:
        d.pop(key)

    return mutate


def put(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda d: field(d).__setitem__(key, value)


def first_section(mut: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], None]:
    return lambda d: d["sections"][0].__setitem__("fields", mut(d["sections"][0]["fields"]))


STRUCTURE_CASES: dict[str, Callable[[dict[str, Any]], Any]] = {
    # mandatory_under_policy must exist and be JSON null
    "mandatory_under_policy key missing": drop("mandatory_under_policy"),
    "mandatory_under_policy = true": put("mandatory_under_policy", True),
    "mandatory_under_policy = false": put("mandatory_under_policy", False),
    "mandatory_under_policy = 'required' (string)": put("mandatory_under_policy", "required"),
    "mandatory_under_policy = {} (object)": put("mandatory_under_policy", {}),
    # appears_on_form must exist, be a boolean and be true
    "appears_on_form key missing": drop("appears_on_form"),
    "appears_on_form = false": put("appears_on_form", False),
    "appears_on_form = null": put("appears_on_form", None),
    "appears_on_form = 'true' (string)": put("appears_on_form", "true"),
    "appears_on_form = 1 (number)": put("appears_on_form", 1),
    # fields must be objects in a `fields` array
    "field is a string": first_section(lambda fs: ["*Name", *fs[1:]]),
    "field is null": first_section(lambda fs: [None, *fs[1:]]),
    "field nested in an extra array": first_section(lambda fs: [[fs[0]], *fs[1:]]),
    "bad field nested in an extra array": first_section(
        lambda fs: [[{k: v for k, v in fs[0].items() if k != "appears_on_form"}]]
    ),
    "second field bad, first valid": first_section(
        lambda fs: [fs[0], {**fs[1], "appears_on_form": "true"}]
    ),
    "fields is an object": first_section(lambda fs: fs[0]),
    "fields is null": first_section(lambda fs: None),
    "fields under a misplaced key": lambda d: d["sections"][0].__setitem__(
        "fieldz", d["sections"][0].pop("fields")
    ),
    # sections must be an array of objects
    "section is a string": lambda d: d.__setitem__("sections", ["A) DETAILS"]),
    "sections is an object": lambda d: d.__setitem__("sections", d["sections"][0]),
    "sections key missing": drop_top("sections"),
    "sections = null": lambda d: d.__setitem__("sections", None),
    "schema_json is an array": lambda d: [d],
}


@pytest.mark.parametrize("case", list(STRUCTURE_CASES))
def test_malformed_fields_and_sections_are_definite_violations(
    db: tuple[sessionmaker[Session], dict[str, Any]], case: str
) -> None:
    with pytest.raises(sa.exc.IntegrityError, match=STRUCTURE) as err:
        insert(db, STRUCTURE_CASES[case])
    assert "violates check constraint" in str(err.value)


IDENTITY_CASES: dict[str, tuple[Callable[[dict[str, Any]], Any], dict[str, Any]]] = {
    "document_role key missing": (drop_top("document_role"), {}),
    "document_role = null": (lambda d: d.__setitem__("document_role", None), {}),
    "document_role mismatch": (lambda d: d.__setitem__("document_role", "reference_document"), {}),
    "extraction_method key missing": (drop_top("extraction_method"), {}),
    "extraction_method = null": (lambda d: d.__setitem__("extraction_method", None), {}),
    "extractor_version key missing": (drop_top("extractor_version"), {}),
    "extractor_version = null": (lambda d: d.__setitem__("extractor_version", None), {}),
    "extractor_version as a numeric string": (
        lambda d: d.__setitem__("extractor_version", str(d["extractor_version"])),
        {},
    ),
    "extractor_version non-numeric string": (
        lambda d: d.__setitem__("extractor_version", "v1"),
        {},
    ),
    "extractor_version differs from column": (
        lambda d: d.__setitem__("extractor_version", d["extractor_version"] + 1),
        {},
    ),
    "document_sha256 key missing": (drop_top("document_sha256"), {}),
    "document_sha256 = null": (lambda d: d.__setitem__("document_sha256", None), {}),
    "document_sha256 mismatch": (lambda d: d.__setitem__("document_sha256", "f" * 64), {}),
}


@pytest.mark.parametrize("case", list(IDENTITY_CASES))
def test_row_and_json_identity_cannot_disagree(
    db: tuple[sessionmaker[Session], dict[str, Any]], case: str
) -> None:
    mutate, columns = IDENTITY_CASES[case]
    with pytest.raises(sa.exc.IntegrityError, match=IDENTITY) as err:
        insert(db, mutate, **columns)
    assert "violates check constraint" in str(err.value)


@pytest.mark.parametrize(
    "case",
    ["unchanged valid schema", "section with an empty fields array", "no sections at all"],
)
def test_valid_shapes_are_accepted(
    db: tuple[sessionmaker[Session], dict[str, Any]], case: str
) -> None:
    mutate: dict[str, Callable[[dict[str, Any]], Any] | None] = {
        "unchanged valid schema": None,
        "section with an empty fields array": lambda d: d["sections"][0].__setitem__("fields", []),
        "no sections at all": lambda d: d.__setitem__("sections", []),
    }
    insert(db, mutate[case])  # no exception: definite TRUE


def test_guards_never_evaluate_to_null(db: tuple[sessionmaker[Session], dict[str, Any]]) -> None:
    """Evaluate both constraint expressions directly on every adversarial document: the
    result must be FALSE (never NULL, which a CHECK would accept)."""
    sessions, base = db
    exprs = {}
    for c in FormSchemaRow.__table__.constraints:
        if getattr(c, "name", None) in (STRUCTURE, IDENTITY):
            exprs[c.name] = str(c.sqltext)  # type: ignore[attr-defined]
    docs: list[tuple[str, Any]] = []
    for name, mut in STRUCTURE_CASES.items():
        d = json.loads(json.dumps(base["schema_json"]))
        r = mut(d)
        docs.append((name, d if r is None else r))
    for name, (mut, _) in IDENTITY_CASES.items():
        d = json.loads(json.dumps(base["schema_json"]))
        mut(d)
        docs.append((name, d))
    with sessions() as s:
        for name, d in docs:
            row = s.execute(
                sa.text(
                    # The interpolated SQL is our own constraint text, never input.
                    f"SELECT ({exprs[STRUCTURE]}) AS structure, "  # noqa: S608
                    f"({exprs[IDENTITY]}) AS identity "
                    "FROM (SELECT CAST(:j AS jsonb) AS schema_json, :role AS document_role, "
                    ":method AS extraction_method, :version AS extractor_version, "
                    ":sha AS document_sha256) t"
                ),
                {
                    "j": json.dumps(d),
                    "role": base["document_role"],
                    "method": base["extraction_method"],
                    "version": base["extractor_version"],
                    "sha": base["document_sha256"],
                },
            ).one()
            assert row.structure is not None and row.identity is not None, name
            assert row.structure is False or row.identity is False, name
