"""M2 C2 follow-up: strict JSON guards on `form_schemas` (0003 is not edited).

0003's guards rejected wrong VALUES but could be bypassed when JSON keys were missing,
NULL, wrongly typed or misplaced: such expressions evaluate to NULL (or skip the field),
and a CHECK accepts NULL. This revision adds two constraints whose expressions are
always a definite TRUE or FALSE:

- `ck_form_schemas_json_identity`: the JSON's document role, extraction method and
  document SHA-256 must equal their columns (`IS NOT DISTINCT FROM`, so a missing or null
  key is a mismatch, not NULL), and `extractor_version` must be a JSON number equal to
  the column.
- `ck_form_schemas_field_structure`: `sections` must be an array of objects, each with a
  `fields` array; every field must be an object in which `appears_on_form` exists, is a
  boolean and is true, and `mandatory_under_policy` exists and is JSON null. The field
  test runs in lax mode (catches missing or mistyped keys) and in strict mode (catches
  fields nested in extra arrays, which lax mode unwraps). Nested CASEs ensure the strict
  paths are evaluated only on a valid shape, so the expression never raises.

PostgreSQL checks CHECK constraints in name order, so these run before 0003's. The
downgrade drops only these two constraints; the table, its rows and 0003 are unchanged.

Revision ID: 0004_m2_form_schema_guards
Revises: 0003_m2_form_schemas
Create Date: 2026-11-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_m2_form_schema_guards"
down_revision: str | Sequence[str] | None = "0003_m2_form_schemas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTITY = (
    "schema_json ->> 'document_role' IS NOT DISTINCT FROM document_role "
    "AND schema_json ->> 'extraction_method' IS NOT DISTINCT FROM extraction_method "
    "AND COALESCE(jsonb_typeof(schema_json -> 'extractor_version') = 'number', false) "
    "AND schema_json ->> 'extractor_version' IS NOT DISTINCT FROM extractor_version::text "
    "AND schema_json ->> 'document_sha256' IS NOT DISTINCT FROM document_sha256"
)
FIELD_STRUCTURE = (
    "CASE WHEN COALESCE(jsonb_typeof(schema_json -> 'sections') = 'array', false) THEN "
    "CASE WHEN NOT jsonb_path_exists(schema_json, "
    '\'lax $.sections[*] ? (@.type() != "object" || !exists(@.fields) '
    '|| @.fields.type() != "array")\') THEN '
    "NOT jsonb_path_exists(schema_json, "
    '\'lax $.sections[*].fields[*] ? (@.type() != "object" '
    '|| !exists(@.appears_on_form) || @.appears_on_form.type() != "boolean" '
    "|| @.appears_on_form != true "
    '|| !exists(@.mandatory_under_policy) || @.mandatory_under_policy.type() != "null")\') '
    "AND NOT jsonb_path_exists(schema_json, "
    "'strict $.sections[*].fields[*] ? (@.type() != \"object\")') "
    "ELSE false END "
    "ELSE false END"
)


def upgrade() -> None:
    op.create_check_constraint("ck_form_schemas_json_identity", "form_schemas", IDENTITY)
    op.create_check_constraint("ck_form_schemas_field_structure", "form_schemas", FIELD_STRUCTURE)


def downgrade() -> None:
    op.drop_constraint("ck_form_schemas_field_structure", "form_schemas", type_="check")
    op.drop_constraint("ck_form_schemas_json_identity", "form_schemas", type_="check")
