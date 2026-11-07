"""M1 corpus: documents, document_pages, chunks.

First revision. M0 shipped no revisions (its database only has an empty
alembic_version table), so this upgrades both a fresh and an M0 database.
Allowed values are written out literally: a migration is a frozen snapshot and must
not change when app.schemas.taxonomy changes later.

Revision ID: 0001_m1_corpus
Revises:
Create Date: 2026-11-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_m1_corpus"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BANK = ("RBI", "HDFC")
DOCUMENT_TYPE = ("regulatory", "policy", "form", "annexure", "reference")
SOURCE_STATUS = ("active", "dated", "superseded", "unknown")
EVIDENCE_ELIGIBILITY = ("primary", "supporting", "context_only")
ATTRIBUTION_BASIS = ("document_text", "document_visual", "filename_only")
VERSION_REVIEW_STATUS = ("not_required", "needs_review", "confirmed")
INGESTION_STATUS = ("pending", "processing", "completed", "partial", "failed")
DOCUMENT_QUALITY = ("text", "scanned", "mixed")
EXTRACTION_METHOD = ("text", "ocr")
PAGE_STATUS = ("ok", "low_quality", "ocr_low_confidence", "failed")
ENTITY_TYPE = (
    "INDIVIDUAL",
    "SOLE_PROPRIETORSHIP",
    "PARTNERSHIP",
    "LLP",
    "COMPANY",
    "TRUST",
    "HUF",
    "SOCIETY",
    "UNINCORPORATED_ASSOCIATION",
    "OTHER_JURIDICAL_PERSON",
)
RESIDENCY_STATUS = ("RESIDENT", "NRI", "PIO")


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    sql = f"{column} IN ({', '.join(repr(v) for v in values)})"
    return f"{column} IS NULL OR {sql}" if nullable else sql


def _array_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} <@ ARRAY[{', '.join(repr(v) for v in values)}]::text[]"


def _ts() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_key", sa.Text(), nullable=False),
        sa.Column("file", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("bank", sa.Text(), nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("source_authority", sa.Text(), nullable=True),
        sa.Column("attribution_basis", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_until", sa.Date(), nullable=True),
        sa.Column("source_status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("evidence_eligibility", sa.Text(), nullable=False),
        sa.Column(
            "supersedes_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "version_review_status", sa.Text(), server_default="not_required", nullable=False
        ),
        sa.Column("entity_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "residency_statuses",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("ingestion_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("quality", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("sha256", name="documents_sha256_key"),
        # M1: one version per logical document; a new version must fail loudly, not overwrite.
        sa.UniqueConstraint("document_key", name="uq_documents_document_key"),
        sa.CheckConstraint(_in("bank", BANK), name="ck_documents_bank"),
        sa.CheckConstraint(_in("document_type", DOCUMENT_TYPE), name="ck_documents_document_type"),
        sa.CheckConstraint(
            _in("source_authority", BANK, nullable=True), name="ck_documents_source_authority"
        ),
        sa.CheckConstraint(
            _in("attribution_basis", ATTRIBUTION_BASIS), name="ck_documents_attribution_basis"
        ),
        sa.CheckConstraint(_in("source_status", SOURCE_STATUS), name="ck_documents_source_status"),
        sa.CheckConstraint(
            _in("evidence_eligibility", EVIDENCE_ELIGIBILITY),
            name="ck_documents_evidence_eligibility",
        ),
        sa.CheckConstraint(
            _in("version_review_status", VERSION_REVIEW_STATUS),
            name="ck_documents_version_review_status",
        ),
        sa.CheckConstraint(
            _in("ingestion_status", INGESTION_STATUS), name="ck_documents_ingestion_status"
        ),
        sa.CheckConstraint(
            _in("quality", DOCUMENT_QUALITY, nullable=True), name="ck_documents_quality"
        ),
        sa.CheckConstraint(
            _array_in("entity_types", ENTITY_TYPE), name="ck_documents_entity_types"
        ),
        sa.CheckConstraint(
            _array_in("residency_statuses", RESIDENCY_STATUS), name="ck_documents_residency"
        ),
        sa.CheckConstraint(
            "source_authority IS NULL OR source_authority = bank",
            name="ck_documents_authority_matches_bank",
        ),
        sa.CheckConstraint(
            "effective_from IS NULL OR effective_until IS NULL "
            "OR effective_from <= effective_until",
            name="ck_documents_effective_range",
        ),
        sa.CheckConstraint("page_count IS NULL OR page_count > 0", name="ck_documents_page_count"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256"),
    )
    op.create_index("ix_documents_bank", "documents", ["bank"])
    op.create_index("ix_documents_document_type", "documents", ["document_type"])
    op.create_index("ix_documents_source_status", "documents", ["source_status"])

    op.create_table(
        "document_pages",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("page_number", sa.Integer(), primary_key=True),
        sa.Column("extraction_method", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("alpha_ratio", sa.Float(), nullable=True),
        sa.Column("image_coverage", sa.Float(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _ts(),
        sa.CheckConstraint("page_number >= 1", name="ck_document_pages_page_number"),
        sa.CheckConstraint(
            _in("extraction_method", EXTRACTION_METHOD, nullable=True),
            name="ck_document_pages_extraction_method",
        ),
        sa.CheckConstraint(_in("status", PAGE_STATUS), name="ck_document_pages_status"),
        sa.CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
            name="ck_document_pages_ocr_confidence",
        ),
        sa.CheckConstraint(
            "image_coverage IS NULL OR (image_coverage >= 0 AND image_coverage <= 1)",
            name="ck_document_pages_image_coverage",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL", name="ck_document_pages_failed_has_error"
        ),
    )

    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("document_key", sa.Text(), nullable=False),
        sa.Column("bank", sa.Text(), nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("source_status", sa.Text(), nullable=False),
        sa.Column("evidence_eligibility", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("entity_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "residency_statuses",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("extraction_method", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column(
            "parent_chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _ts(),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
        sa.ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            name="fk_chunks_document_page",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in("bank", BANK), name="ck_chunks_bank"),
        sa.CheckConstraint(_in("document_type", DOCUMENT_TYPE), name="ck_chunks_document_type"),
        sa.CheckConstraint(_in("source_status", SOURCE_STATUS), name="ck_chunks_source_status"),
        sa.CheckConstraint(
            _in("evidence_eligibility", EVIDENCE_ELIGIBILITY),
            name="ck_chunks_evidence_eligibility",
        ),
        sa.CheckConstraint(
            _in("extraction_method", EXTRACTION_METHOD), name="ck_chunks_extraction_method"
        ),
        sa.CheckConstraint(_array_in("entity_types", ENTITY_TYPE), name="ck_chunks_entity_types"),
        sa.CheckConstraint(
            _array_in("residency_statuses", RESIDENCY_STATUS), name="ck_chunks_residency"
        ),
        sa.CheckConstraint("page_number >= 1", name="ck_chunks_page_number"),
        sa.CheckConstraint(
            "page_end IS NULL OR page_end >= page_number", name="ck_chunks_page_range"
        ),
        sa.CheckConstraint("length(content) > 0", name="ck_chunks_content_nonempty"),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_bank", "chunks", ["bank"])
    op.create_index("ix_chunks_document_type", "chunks", ["document_type"])
    op.create_index("ix_chunks_source_status", "chunks", ["source_status"])
    op.create_index("ix_chunks_evidence_eligibility", "chunks", ["evidence_eligibility"])
    op.create_index("ix_chunks_entity_types", "chunks", ["entity_types"], postgresql_using="gin")
    op.create_index(
        "ix_chunks_residency_statuses", "chunks", ["residency_statuses"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("document_pages")
    op.drop_table("documents")
