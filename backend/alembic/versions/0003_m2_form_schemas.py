"""M2 C2: persisted, versioned form schemas (deterministic extraction from M1 chunks).

Additive to 0001/0002 (which are not edited; no existing table is changed):
- `form_schemas`: one row per (document, extraction method, extractor version). The
  `schema_json` holds the full `FormSchema` (sections, fields, text blocks, provenance,
  quality flags); summary columns support listing and checks.
- Identity and versioning: `id` is derived from (document SHA-256, method, version);
  UNIQUE(document_id, extraction_method, extractor_version). A new PDF (new SHA-256 ->
  new document id) or a new extractor version is a NEW row; existing rows are never
  overwritten (the application refuses a different schema under an existing identity).
- Policy guard in the database: no persisted field may carry `mandatory_under_policy`,
  and every persisted field must be `appears_on_form = true`.

Allowed values are written out literally (migrations are frozen snapshots).

Revision ID: 0003_m2_form_schemas
Revises: 0002_m1_persistence
Create Date: 2026-11-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_m2_form_schemas"
down_revision: str | Sequence[str] | None = "0002_m1_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form_schemas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("form_key", sa.Text(), nullable=False),
        sa.Column("document_sha256", sa.Text(), nullable=False),
        sa.Column("document_role", sa.Text(), nullable=False),
        sa.Column("evidence_eligibility", sa.Text(), nullable=False),
        sa.Column("extraction_method", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Integer(), nullable=False),
        sa.Column("schema_sha256", sa.Text(), nullable=False),
        sa.Column("schema_json", postgresql.JSONB(), nullable=False),
        sa.Column("section_count", sa.Integer(), nullable=False),
        sa.Column("field_count", sa.Integer(), nullable=False),
        sa.Column("required_as_shown_count", sa.Integer(), nullable=False),
        sa.Column("text_block_count", sa.Integer(), nullable=False),
        sa.Column(
            "quality_flags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "document_id",
            "extraction_method",
            "extractor_version",
            name="uq_form_schemas_document_method_version",
        ),
        sa.CheckConstraint(
            "document_role IN ('kyc_form', 'reference_document')",
            name="ck_form_schemas_document_role",
        ),
        sa.CheckConstraint(
            "evidence_eligibility IN ('primary', 'supporting', 'context_only')",
            name="ck_form_schemas_evidence_eligibility",
        ),
        sa.CheckConstraint(
            "extraction_method IN ('m1_chunks')", name="ck_form_schemas_extraction_method"
        ),
        sa.CheckConstraint("extractor_version >= 1", name="ck_form_schemas_extractor_version"),
        sa.CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$'", name="ck_form_schemas_document_sha256"
        ),
        sa.CheckConstraint(
            "schema_sha256 ~ '^[0-9a-f]{64}$'", name="ck_form_schemas_schema_sha256"
        ),
        sa.CheckConstraint(
            "section_count >= 0 AND field_count >= 0 AND required_as_shown_count >= 0 "
            "AND text_block_count >= 0 AND required_as_shown_count <= field_count",
            name="ck_form_schemas_counts",
        ),
        sa.CheckConstraint(
            "NOT jsonb_path_exists(schema_json, "
            "'$.sections[*].fields[*] ? (@.mandatory_under_policy != null)')",
            name="ck_form_schemas_no_policy_obligation",
        ),
        sa.CheckConstraint(
            "NOT jsonb_path_exists(schema_json, "
            "'$.sections[*].fields[*] ? (@.appears_on_form != true)')",
            name="ck_form_schemas_fields_appear_on_form",
        ),
        sa.CheckConstraint(
            "schema_json ->> 'document_role' = document_role "
            "AND schema_json ->> 'extraction_method' = extraction_method "
            "AND (schema_json ->> 'extractor_version')::int = extractor_version "
            "AND schema_json ->> 'document_sha256' = document_sha256",
            name="ck_form_schemas_json_matches_columns",
        ),
    )
    op.create_index("ix_form_schemas_form_key", "form_schemas", ["form_key"])


def downgrade() -> None:
    op.drop_index("ix_form_schemas_form_key", table_name="form_schemas")
    op.drop_table("form_schemas")
