"""M2 G3: allow `gemini_multimodal` as a form-schema extraction method.

Replaces only `ck_form_schemas_extraction_method` (0003 and 0004 files are not edited):
`extraction_method IN ('m1_chunks')` becomes `IN ('m1_chunks', 'gemini_multimodal')`.
Every other guard (no policy obligation, field structure, JSON identity) applies to the
new method unchanged. Existing rows are untouched.

The downgrade restores the m1_chunks-only check. It refuses (rather than deleting data)
while any gemini_multimodal row exists: remove those rows deliberately first.

Revision ID: 0005_m2_gemini_method
Revises: 0004_m2_form_schema_guards
Create Date: 2026-11-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_m2_gemini_method"
down_revision: str | Sequence[str] | None = "0004_m2_form_schema_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_form_schemas_extraction_method"
METHODS = "extraction_method IN ('m1_chunks', 'gemini_multimodal')"
M1_ONLY = "extraction_method IN ('m1_chunks')"


def upgrade() -> None:
    op.drop_constraint(NAME, "form_schemas", type_="check")
    op.create_check_constraint(NAME, "form_schemas", METHODS)


def downgrade() -> None:
    gemini_rows = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM form_schemas WHERE extraction_method <> 'm1_chunks'")
    )
    if gemini_rows:
        raise RuntimeError(
            f"refusing to downgrade: {gemini_rows} non-m1_chunks form schema row(s) exist; "
            "delete them deliberately before downgrading"
        )
    op.drop_constraint(NAME, "form_schemas", type_="check")
    op.create_check_constraint(NAME, "form_schemas", M1_ONLY)
