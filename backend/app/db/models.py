"""ORM models. Tables are added per milestone, always via Alembic migrations (docs/08).

Enum-valued columns are TEXT + CHECK (values from app.schemas.taxonomy) rather than
native Postgres enums, so adding a value is a constraint change, not a type migration.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.schemas.taxonomy import (
    AttributionBasis,
    Bank,
    DocumentQuality,
    DocumentType,
    EntityType,
    EvidenceEligibility,
    ExtractionMethod,
    IngestionStatus,
    PageStatus,
    ResidencyStatus,
    SourceStatus,
    VersionReviewStatus,
)


class Base(DeclarativeBase):
    pass


def _in(column: str, enum: type[StrEnum], *, nullable: bool = False) -> str:
    values = ", ".join(f"'{m.value}'" for m in enum)
    sql = f"{column} IN ({values})"
    return f"{column} IS NULL OR {sql}" if nullable else sql


def _array_in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{m.value}'" for m in enum)
    return f"{column} <@ ARRAY[{values}]::text[]"


class Document(Base):
    """One source document: logical `document_key` + exact file content (`sha256`).

    M1 stores a single version per `document_key` (UNIQUE); multi-version history needs
    a later migration that relaxes this together with explicit supersedes links.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_key: Mapped[str] = mapped_column(Text, nullable=False)
    file: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    bank: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_authority: Mapped[str | None] = mapped_column(Text)
    attribution_basis: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_until: Mapped[date | None] = mapped_column(Date)
    source_status: Mapped[str] = mapped_column(Text, nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    evidence_eligibility: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    version_review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=VersionReviewStatus.NOT_REQUIRED.value
    )
    entity_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    residency_statuses: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    ingestion_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=IngestionStatus.PENDING.value
    )
    quality: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in("bank", Bank), name="ck_documents_bank"),
        CheckConstraint(_in("document_type", DocumentType), name="ck_documents_document_type"),
        CheckConstraint(
            _in("source_authority", Bank, nullable=True), name="ck_documents_source_authority"
        ),
        CheckConstraint(
            _in("attribution_basis", AttributionBasis), name="ck_documents_attribution_basis"
        ),
        CheckConstraint(_in("source_status", SourceStatus), name="ck_documents_source_status"),
        CheckConstraint(
            _in("evidence_eligibility", EvidenceEligibility),
            name="ck_documents_evidence_eligibility",
        ),
        CheckConstraint(
            _in("version_review_status", VersionReviewStatus),
            name="ck_documents_version_review_status",
        ),
        CheckConstraint(
            _in("ingestion_status", IngestionStatus), name="ck_documents_ingestion_status"
        ),
        CheckConstraint(
            _in("quality", DocumentQuality, nullable=True), name="ck_documents_quality"
        ),
        CheckConstraint(_array_in("entity_types", EntityType), name="ck_documents_entity_types"),
        CheckConstraint(
            _array_in("residency_statuses", ResidencyStatus), name="ck_documents_residency"
        ),
        CheckConstraint(
            "source_authority IS NULL OR source_authority = bank",
            name="ck_documents_authority_matches_bank",
        ),
        CheckConstraint(
            "effective_from IS NULL OR effective_until IS NULL "
            "OR effective_from <= effective_until",
            name="ck_documents_effective_range",
        ),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="ck_documents_page_count"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256"),
        # M1 limitation: one version per logical document. A second version of a key fails
        # loudly here instead of overwriting or coexisting unlinked (docs/15).
        UniqueConstraint("document_key", name="uq_documents_document_key"),
        Index("ix_documents_bank", "bank"),
        Index("ix_documents_document_type", "document_type"),
        Index("ix_documents_source_status", "source_status"),
    )


class DocumentPage(Base):
    """Per-page extraction record. Every page gets a row, including failed ones."""

    __tablename__ = "document_pages"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    extraction_method: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    alpha_ratio: Mapped[float | None] = mapped_column(Float)
    image_coverage: Mapped[float | None] = mapped_column(Float)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("page_number >= 1", name="ck_document_pages_page_number"),
        CheckConstraint(
            _in("extraction_method", ExtractionMethod, nullable=True),
            name="ck_document_pages_extraction_method",
        ),
        CheckConstraint(_in("status", PageStatus), name="ck_document_pages_status"),
        CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
            name="ck_document_pages_ocr_confidence",
        ),
        CheckConstraint(
            "image_coverage IS NULL OR (image_coverage >= 0 AND image_coverage <= 1)",
            name="ck_document_pages_image_coverage",
        ),
        # A failed page must say why.
        CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL", name="ck_document_pages_failed_has_error"
        ),
    )


class Chunk(Base):
    """Retrievable passage. Mirrors the Qdrant payload (docs/08) for keyword search."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    document_key: Mapped[str] = mapped_column(Text, nullable=False)
    bank: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_status: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_eligibility: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    entity_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    residency_statuses: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    extraction_method: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
        # Provenance: a chunk's start page must be a recorded page of its document.
        ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            name="fk_chunks_document_page",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("bank", Bank), name="ck_chunks_bank"),
        CheckConstraint(_in("document_type", DocumentType), name="ck_chunks_document_type"),
        CheckConstraint(_in("source_status", SourceStatus), name="ck_chunks_source_status"),
        CheckConstraint(
            _in("evidence_eligibility", EvidenceEligibility),
            name="ck_chunks_evidence_eligibility",
        ),
        CheckConstraint(
            _in("extraction_method", ExtractionMethod), name="ck_chunks_extraction_method"
        ),
        CheckConstraint(_array_in("entity_types", EntityType), name="ck_chunks_entity_types"),
        CheckConstraint(
            _array_in("residency_statuses", ResidencyStatus), name="ck_chunks_residency"
        ),
        CheckConstraint("page_number >= 1", name="ck_chunks_page_number"),
        CheckConstraint("page_end IS NULL OR page_end >= page_number", name="ck_chunks_page_range"),
        CheckConstraint("length(content) > 0", name="ck_chunks_content_nonempty"),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_bank", "bank"),
        Index("ix_chunks_document_type", "document_type"),
        Index("ix_chunks_source_status", "source_status"),
        Index("ix_chunks_evidence_eligibility", "evidence_eligibility"),
        Index("ix_chunks_entity_types", "entity_types", postgresql_using="gin"),
        Index("ix_chunks_residency_statuses", "residency_statuses", postgresql_using="gin"),
    )
