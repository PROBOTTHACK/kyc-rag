"""ORM models. Tables are added per milestone, always via Alembic migrations (docs/08).

Enum-valued columns are TEXT + CHECK (values from app.schemas.taxonomy) rather than
native Postgres enums, so adding a value is a constraint change, not a type migration.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
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

from app.graph.state import DataClass, Outcome, RunStatus
from app.schemas.taxonomy import (
    AttributionBasis,
    Bank,
    ChunkRole,
    DocumentQuality,
    DocumentType,
    EntityType,
    EvidenceEligibility,
    ExtractionMethod,
    IngestionStage,
    IngestionStatus,
    PageStatus,
    ResidencyStatus,
    SourceStatus,
    TextReliability,
    VersionReviewStatus,
)


class Base(DeclarativeBase):
    pass


class SessionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


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
    # Stage A/B page record: removed-line counts, page label, OCR regions with printed
    # page numbers and confidences (`ProcessedPage.metadata()`, cache flags excluded).
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
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
    """Every chunk of a document: single, parent and child (Postgres = source of truth).

    Mirrors the Qdrant payload (docs/08) for keyword search. Parents are stored here only
    (never embedded). A child must reference a parent of the same document; that FK is
    checked at commit and has no ON DELETE action, so deleting a parent that still has
    children fails instead of orphaning them.
    """

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
    content: Mapped[str] = mapped_column(Text, nullable=False)  # normalised text
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)  # source lines before normalisation
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    chunk_role: Mapped[str] = mapped_column(Text, nullable=False)
    text_reliability: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    can_support_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False)
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
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
        UniqueConstraint("document_id", "id", name="uq_chunks_document_id_id"),
        # Provenance: a chunk's start page must be a recorded page of its document.
        ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            name="fk_chunks_document_page",
            ondelete="CASCADE",
        ),
        # A child's parent exists and belongs to the same document (checked at commit).
        ForeignKeyConstraint(
            ["document_id", "parent_chunk_id"],
            ["chunks.document_id", "chunks.id"],
            name="fk_chunks_parent_same_document",
            deferrable=True,
            initially="DEFERRED",
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
        CheckConstraint(_in("chunk_role", ChunkRole), name="ck_chunks_chunk_role"),
        CheckConstraint(
            _in("text_reliability", TextReliability), name="ck_chunks_text_reliability"
        ),
        CheckConstraint("page_number >= 1", name="ck_chunks_page_number"),
        CheckConstraint("page_end IS NULL OR page_end >= page_number", name="ck_chunks_page_range"),
        CheckConstraint("length(content) > 0", name="ck_chunks_content_nonempty"),
        # Exactly the children have a parent; parents are never embedded.
        CheckConstraint(
            "(chunk_role = 'child') = (parent_chunk_id IS NOT NULL)",
            name="ck_chunks_child_has_parent",
        ),
        CheckConstraint(
            "chunk_role <> 'parent' OR NOT embedding_eligible",
            name="ck_chunks_parent_not_embedded",
        ),
        CheckConstraint(
            "parent_chunk_id IS NULL OR parent_chunk_id <> id", name="ck_chunks_not_own_parent"
        ),
        CheckConstraint(
            "(extraction_method = 'text') = (text_reliability = 'native')",
            name="ck_chunks_reliability_matches_method",
        ),
        # Same rule as ChunkMetadata.can_support_mandatory (docs/15).
        CheckConstraint(
            "can_support_mandatory = (evidence_eligibility = 'primary' "
            "AND text_reliability <> 'ocr_uncertain')",
            name="ck_chunks_can_support_mandatory",
        ),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_parent_chunk_id", "parent_chunk_id"),
        Index("ix_chunks_bank", "bank"),
        Index("ix_chunks_document_type", "document_type"),
        Index("ix_chunks_source_status", "source_status"),
        Index("ix_chunks_evidence_eligibility", "evidence_eligibility"),
        Index("ix_chunks_embedding_eligible", "embedding_eligible"),
        Index("ix_chunks_entity_types", "entity_types", postgresql_using="gin"),
        Index("ix_chunks_residency_statuses", "residency_statuses", postgresql_using="gin"),
    )


class DocumentExclusion(Base):
    """A source line left out of every chunk's content, with the reason
    (`section_heading`, `table_cell_line`, `duplicate_line`, `noise`, ...).

    Low-confidence OCR lines excluded from a particular chunk are stored on that chunk
    (`chunks.metadata.excluded_lines`, with confidence); this table holds the
    document-level exclusions reported by the chunkers.
    """

    __tablename__ = "document_exclusions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)  # order in the document
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            name="fk_document_exclusions_page",
            ondelete="CASCADE",
        ),
        CheckConstraint("seq >= 0", name="ck_document_exclusions_seq"),
        Index("ix_document_exclusions_reason", "reason"),
    )


class FormSchemaRow(Base):
    """A persisted form schema (M2 C2, migration 0003): one row per (document, extraction
    method, extractor version). `schema_json` is the full `app.schemas.forms.FormSchema`.

    Rows are never overwritten: an identical re-extraction writes nothing, and a different
    schema under an existing identity is refused by the application (`app/db/forms.py`).
    The CHECKs mirror migration 0003, including the database-level guard that no field
    carries `mandatory_under_policy` and every field `appears_on_form`.
    """

    __tablename__ = "form_schemas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    form_key: Mapped[str] = mapped_column(Text, nullable=False)
    document_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    document_role: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_eligibility: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_method: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    section_count: Mapped[int] = mapped_column(Integer, nullable=False)
    field_count: Mapped[int] = mapped_column(Integer, nullable=False)
    required_as_shown_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_block_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "extraction_method",
            "extractor_version",
            name="uq_form_schemas_document_method_version",
        ),
        CheckConstraint(
            "document_role IN ('kyc_form', 'reference_document')",
            name="ck_form_schemas_document_role",
        ),
        CheckConstraint(
            _in("evidence_eligibility", EvidenceEligibility),
            name="ck_form_schemas_evidence_eligibility",
        ),
        CheckConstraint(
            # Widened by migration 0005 (M2 G3).
            "extraction_method IN ('m1_chunks', 'gemini_multimodal')",
            name="ck_form_schemas_extraction_method",
        ),
        CheckConstraint("extractor_version >= 1", name="ck_form_schemas_extractor_version"),
        CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$'", name="ck_form_schemas_document_sha256"
        ),
        CheckConstraint("schema_sha256 ~ '^[0-9a-f]{64}$'", name="ck_form_schemas_schema_sha256"),
        CheckConstraint(
            "section_count >= 0 AND field_count >= 0 AND required_as_shown_count >= 0 "
            "AND text_block_count >= 0 AND required_as_shown_count <= field_count",
            name="ck_form_schemas_counts",
        ),
        CheckConstraint(
            "NOT jsonb_path_exists(schema_json, "
            "'$.sections[*].fields[*] ? (@.mandatory_under_policy != null)')",
            name="ck_form_schemas_no_policy_obligation",
        ),
        CheckConstraint(
            "NOT jsonb_path_exists(schema_json, "
            "'$.sections[*].fields[*] ? (@.appears_on_form != true)')",
            name="ck_form_schemas_fields_appear_on_form",
        ),
        CheckConstraint(
            "schema_json ->> 'document_role' = document_role "
            "AND schema_json ->> 'extraction_method' = extraction_method "
            "AND (schema_json ->> 'extractor_version')::int = extractor_version "
            "AND schema_json ->> 'document_sha256' = document_sha256",
            name="ck_form_schemas_json_matches_columns",
        ),
        # Migration 0004: strict guards whose expressions are never NULL (missing, null,
        # mistyped or misplaced JSON keys are definite failures). Same SQL as 0004.
        CheckConstraint(
            "schema_json ->> 'document_role' IS NOT DISTINCT FROM document_role "
            "AND schema_json ->> 'extraction_method' IS NOT DISTINCT FROM extraction_method "
            "AND COALESCE(jsonb_typeof(schema_json -> 'extractor_version') = 'number', false) "
            "AND schema_json ->> 'extractor_version' IS NOT DISTINCT FROM extractor_version::text "
            "AND schema_json ->> 'document_sha256' IS NOT DISTINCT FROM document_sha256",
            name="ck_form_schemas_json_identity",
        ),
        CheckConstraint(
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
            "ELSE false END",
            name="ck_form_schemas_field_structure",
        ),
        Index("ix_form_schemas_form_key", "form_key"),
    )


class IngestionRun(Base):
    """One ingestion attempt for one document: per-stage outcome and the error, if any.

    Written in its own short transactions, outside the document's persistence
    transaction, and with no FK to `documents`: a failed first ingestion, whose
    document row is rolled back, is still recorded here (by `document_key` + `sha256`).
    """

    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)  # "ingest", "reconcile"
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failed_stage: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # {stage: {"status": ok|failed|skipped, "error"?: str, ...counts}}
    stages: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", IngestionStatus), name="ck_ingestion_runs_status"),
        CheckConstraint(
            _in("failed_stage", IngestionStage, nullable=True),
            name="ck_ingestion_runs_failed_stage",
        ),
        CheckConstraint(
            "status <> 'failed' OR (failed_stage IS NOT NULL AND error IS NOT NULL)",
            name="ck_ingestion_runs_failed_has_reason",
        ),
        CheckConstraint(
            "(status = 'processing') = (finished_at IS NULL)", name="ck_ingestion_runs_finished"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_ingestion_runs_sha256"),
        Index("ix_ingestion_runs_document_key", "document_key", "started_at"),
        Index("ix_ingestion_runs_status", "status"),
    )


# --- M3 workflow (migration 0006_m3_workflow; M3_PLAN §5, §9, §11) ---------------------------


class KycSession(Base):
    """A workflow session. Busy-ness comes from its runs, not from `status`."""

    __tablename__ = "kyc_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    data_class: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unspecified'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checkpoints_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", SessionStatus), name="ck_kyc_sessions_status"),
        CheckConstraint(_in("data_class", DataClass), name="ck_kyc_sessions_data_class"),
        CheckConstraint("expires_at > created_at", name="ck_kyc_sessions_expiry"),
    )


class GraphRun(Base):
    """One graph run (execution lifecycle and the Postgres run lease; used from S6)."""

    __tablename__ = "graph_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_sessions.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_events: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    llm_calls: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", RunStatus), name="ck_graph_runs_status"),
        CheckConstraint("retries >= 0", name="ck_graph_runs_retries"),
        Index(
            "uq_graph_runs_active",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'awaiting_clarification')"),
        ),
        Index("ix_graph_runs_session", "session_id", "started_at"),
    )


class ReportRow(Base):
    """One report per run: insert-only (a trigger rejects UPDATE; DELETE is the S8 purge).
    Every report requires human review; no column or value can express approval."""

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_sessions.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    human_review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    evidence_status: Mapped[str | None] = mapped_column(Text)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in("outcome", Outcome), name="ck_reports_outcome"),
        CheckConstraint("human_review_required IS TRUE", name="ck_reports_human_review"),
        CheckConstraint(
            "evidence_status IS NULL OR evidence_status IN "
            "('found', 'partial', 'none', 'retrieval_failed')",
            name="ck_reports_evidence_status",
        ),
        CheckConstraint(
            "report_json->>'outcome' IS NOT DISTINCT FROM outcome "
            "AND report_json->>'report_id' IS NOT DISTINCT FROM id::text "
            "AND (report_json->'human_review_required') = 'true'::jsonb",
            name="ck_reports_json_identity",
        ),
        UniqueConstraint("session_id", "run_id", name="uq_reports_session_run"),
    )


class ReviewCaseRow(Base):
    """An open human-review case (M4 S7, migration 0008): insert-only (a trigger rejects
    UPDATE), deleted only by cascade with its report or session. `status` can only be
    `open`; no column or value can express a decision."""

    __tablename__ = "review_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_sessions.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "reason IN ('contradicted', 'conflicting_sources', 'unsupported', "
            "'mandatory_not_established', 'explanation_overstates_source', 'not_verified')",
            name="ck_review_cases_reason",
        ),
        CheckConstraint("status = 'open'", name="ck_review_cases_open_only"),
        UniqueConstraint(
            "report_id", "requirement_id", "reason", name="uq_review_cases_report_req_reason"
        ),
        Index("ix_review_cases_report", "report_id"),
    )


class ApiIdempotency(Base):
    """Idempotency records for POST /messages, /resume, /retry (used from S6)."""

    __tablename__ = "api_idempotency"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kyc_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_api_idempotency_sha256"),
        CheckConstraint(
            "endpoint IN ('messages', 'resume', 'retry')", name="ck_api_idempotency_endpoint"
        ),
    )
