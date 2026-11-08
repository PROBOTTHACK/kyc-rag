"""M1 Stage E: Postgres persistence of the full chunk set + ingestion run tracking.

Additive to 0001 (which is not edited):
- `document_pages.metadata` jsonb: the Stage A/B page record (removed lines, page label,
  OCR regions with printed page numbers and confidences).
- `chunks`: `chunk_role`, `text_reliability`, `embedding_eligible`,
  `can_support_mandatory` become real columns (backfilled from `chunks.metadata`, then
  removed from it), plus `raw_text` and `updated_at`. CHECKs tie role, parent and
  eligibility together.
- The parent self-reference is replaced: the old `parent_chunk_id -> chunks.id`
  (ON DELETE SET NULL, which could orphan children silently) becomes
  `(document_id, parent_chunk_id) -> chunks(document_id, id)`, DEFERRABLE INITIALLY
  DEFERRED with no delete action, so a parent must exist in the same document and
  cannot be deleted while children still reference it.
- `document_exclusions`: document-level lines left out of chunk content, with reason.
- `ingestion_runs`: one row per ingestion attempt with per-stage outcome; no FK to
  `documents`, so a rolled-back first ingestion is still recorded.

Allowed values are written out literally (migrations are frozen snapshots).

Revision ID: 0002_m1_persistence
Revises: 0001_m1_corpus
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_m1_persistence"
down_revision: str | Sequence[str] | None = "0001_m1_corpus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHUNK_ROLE = ("single", "parent", "child")
TEXT_RELIABILITY = ("native", "ocr_reliable", "ocr_uncertain")
INGESTION_STATUS = ("pending", "processing", "completed", "partial", "failed")
INGESTION_STAGE = ("extraction", "ocr", "chunking", "persistence", "embedding", "indexing")


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    sql = f"{column} IN ({', '.join(repr(v) for v in values)})"
    return f"{column} IS NULL OR {sql}" if nullable else sql


def upgrade() -> None:
    # --- document_pages -------------------------------------------------------------
    op.add_column(
        "document_pages",
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )

    # --- chunks: promoted columns, backfilled from metadata -------------------------
    op.add_column("chunks", sa.Column("raw_text", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("chunk_role", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("text_reliability", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("embedding_eligible", sa.Boolean(), nullable=True))
    op.add_column("chunks", sa.Column("can_support_mandatory", sa.Boolean(), nullable=True))
    op.add_column(
        "chunks",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Rows written before Stage E (none in practice) carry these values in metadata.
    # Missing values fall back to the ChunkMetadata defaults.
    op.execute("""
        UPDATE chunks SET
          raw_text = metadata->>'raw_text',
          chunk_role = COALESCE(metadata->>'chunk_role',
                                CASE WHEN parent_chunk_id IS NULL THEN 'single' ELSE 'child' END),
          text_reliability = COALESCE(metadata->>'text_reliability',
                                      CASE WHEN extraction_method = 'text' THEN 'native'
                                           ELSE 'ocr_uncertain' END),
          embedding_eligible = COALESCE((metadata->>'embedding_eligible')::boolean,
                                        COALESCE(metadata->>'chunk_role', '') <> 'parent')
        """)
    op.execute(
        "UPDATE chunks SET can_support_mandatory = "
        "(evidence_eligibility = 'primary' AND text_reliability <> 'ocr_uncertain')"
    )
    op.execute(
        "UPDATE chunks SET metadata = metadata - ARRAY['chunk_role', 'text_reliability', "
        "'embedding_eligible', 'can_support_mandatory', 'raw_text']::text[]"
    )
    for col in ("chunk_role", "text_reliability", "embedding_eligible", "can_support_mandatory"):
        op.alter_column("chunks", col, nullable=False)

    # --- chunks: parent reference ---------------------------------------------------
    op.drop_constraint("chunks_parent_chunk_id_fkey", "chunks", type_="foreignkey")
    op.create_unique_constraint("uq_chunks_document_id_id", "chunks", ["document_id", "id"])
    op.create_foreign_key(
        "fk_chunks_parent_same_document",
        "chunks",
        "chunks",
        ["document_id", "parent_chunk_id"],
        ["document_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint("ck_chunks_chunk_role", "chunks", _in("chunk_role", CHUNK_ROLE))
    op.create_check_constraint(
        "ck_chunks_text_reliability", "chunks", _in("text_reliability", TEXT_RELIABILITY)
    )
    op.create_check_constraint(
        "ck_chunks_child_has_parent",
        "chunks",
        "(chunk_role = 'child') = (parent_chunk_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_chunks_parent_not_embedded",
        "chunks",
        "chunk_role <> 'parent' OR NOT embedding_eligible",
    )
    op.create_check_constraint(
        "ck_chunks_not_own_parent", "chunks", "parent_chunk_id IS NULL OR parent_chunk_id <> id"
    )
    op.create_check_constraint(
        "ck_chunks_reliability_matches_method",
        "chunks",
        "(extraction_method = 'text') = (text_reliability = 'native')",
    )
    op.create_check_constraint(
        "ck_chunks_can_support_mandatory",
        "chunks",
        "can_support_mandatory = (evidence_eligibility = 'primary' "
        "AND text_reliability <> 'ocr_uncertain')",
    )
    op.create_index("ix_chunks_parent_chunk_id", "chunks", ["parent_chunk_id"])
    op.create_index("ix_chunks_embedding_eligible", "chunks", ["embedding_eligible"])

    # --- document_exclusions ----------------------------------------------------------
    op.create_table(
        "document_exclusions",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            name="fk_document_exclusions_page",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_document_exclusions_seq"),
    )
    op.create_index("ix_document_exclusions_reason", "document_exclusions", ["reason"])

    # --- ingestion_runs ------------------------------------------------------------------
    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failed_stage", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "stages", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("status", INGESTION_STATUS), name="ck_ingestion_runs_status"),
        sa.CheckConstraint(
            _in("failed_stage", INGESTION_STAGE, nullable=True),
            name="ck_ingestion_runs_failed_stage",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (failed_stage IS NOT NULL AND error IS NOT NULL)",
            name="ck_ingestion_runs_failed_has_reason",
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (finished_at IS NULL)", name="ck_ingestion_runs_finished"
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_ingestion_runs_sha256"),
    )
    op.create_index(
        "ix_ingestion_runs_document_key", "ingestion_runs", ["document_key", "started_at"]
    )
    op.create_index("ix_ingestion_runs_status", "ingestion_runs", ["status"])


def downgrade() -> None:
    op.drop_table("ingestion_runs")
    op.drop_table("document_exclusions")
    op.drop_index("ix_chunks_embedding_eligible", "chunks")
    op.drop_index("ix_chunks_parent_chunk_id", "chunks")
    for name in (
        "ck_chunks_can_support_mandatory",
        "ck_chunks_reliability_matches_method",
        "ck_chunks_not_own_parent",
        "ck_chunks_parent_not_embedded",
        "ck_chunks_child_has_parent",
        "ck_chunks_text_reliability",
        "ck_chunks_chunk_role",
    ):
        op.drop_constraint(name, "chunks", type_="check")
    op.drop_constraint("fk_chunks_parent_same_document", "chunks", type_="foreignkey")
    op.drop_constraint("uq_chunks_document_id_id", "chunks", type_="unique")
    op.create_foreign_key(
        "chunks_parent_chunk_id_fkey",
        "chunks",
        "chunks",
        ["parent_chunk_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Fold the promoted columns back into metadata so no data is lost.
    op.execute("""
        UPDATE chunks SET metadata = metadata || jsonb_strip_nulls(jsonb_build_object(
          'raw_text', raw_text,
          'chunk_role', chunk_role,
          'text_reliability', text_reliability,
          'embedding_eligible', embedding_eligible,
          'can_support_mandatory', can_support_mandatory))
        """)
    for col in (
        "updated_at",
        "can_support_mandatory",
        "embedding_eligible",
        "text_reliability",
        "chunk_role",
        "raw_text",
    ):
        op.drop_column("chunks", col)
    op.drop_column("document_pages", "metadata")
