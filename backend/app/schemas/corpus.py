"""Document and chunk metadata: the one schema shared by manifest, Postgres and Qdrant.

`DocumentMetadata` is exactly one manifest entry. `ChunkMetadata` is the Qdrant payload;
its document-level fields are always copied from a `DocumentMetadata` (`for_document`),
so the three stores cannot drift. Unknown values stay None (CLAUDE.md principle 2).
"""

import uuid
from datetime import date
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.schemas.taxonomy import (
    AttributionBasis,
    Bank,
    DocumentType,
    EntityType,
    EvidenceEligibility,
    ExtractionMethod,
    ResidencyStatus,
    SourceStatus,
    VersionReviewStatus,
)

# Fixed namespace so ids are reproducible across machines and re-runs.
ID_NAMESPACE = uuid.UUID("8f0c6a52-3d51-4f0e-9d7a-6f1f2b9c4e11")

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DocumentKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(_[a-z0-9]+)*$", max_length=80)]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Qdrant payload fields that get a keyword index (docs/08 list + status/eligibility/residency).
QDRANT_KEYWORD_INDEXES: tuple[str, ...] = (
    "document_id",
    "document_key",
    "bank",
    "document_type",
    "source_status",
    "evidence_eligibility",
    "entity_types",
    "residency_statuses",
)


def derive_evidence_eligibility(
    document_type: DocumentType, source_status: SourceStatus
) -> EvidenceEligibility:
    """Deterministic retrieval eligibility. Only active regulatory/policy text is PRIMARY."""
    if document_type is DocumentType.REFERENCE:
        return EvidenceEligibility.CONTEXT_ONLY
    if source_status is SourceStatus.ACTIVE and document_type in (
        DocumentType.REGULATORY,
        DocumentType.POLICY,
    ):
        return EvidenceEligibility.PRIMARY
    # Forms show what a form requests, not a current obligation; annexures and any
    # dated/superseded/unknown source are likewise usable evidence, never sufficient alone.
    return EvidenceEligibility.SUPPORTING


def document_row_id(sha256: str) -> uuid.UUID:
    """Row id of one file's content. Logical identity is `document_key`, not this."""
    return uuid.uuid5(ID_NAMESPACE, f"document:{sha256}")


def chunk_id(sha256: str, chunk_index: int) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, f"chunk:{sha256}:{chunk_index}")


def _unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate values: {values}")
    return values


class DocumentMetadata(BaseModel):
    """One source document version (= one manifest entry = one `documents` row)."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    document_key: DocumentKey
    file: NonEmptyText  # POSIX path relative to the corpus dir
    sha256: Sha256
    bank: Bank  # corpus / target-bank context, NOT the publisher
    document_type: DocumentType
    source_authority: Bank | None = None  # verified publisher only; else None
    attribution_basis: AttributionBasis  # how source_authority was established
    title: NonEmptyText  # as printed on the document
    version: str | None = None  # as printed (edition text or form print code), never inferred
    issue_date: date | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    source_status: SourceStatus
    status_reason: str | None = None
    supersedes: list[DocumentKey] = Field(default_factory=list)
    version_review_status: VersionReviewStatus = VersionReviewStatus.NOT_REQUIRED
    entity_types: list[EntityType] = Field(min_length=1)
    residency_statuses: list[ResidencyStatus] = Field(default_factory=list)  # [] = not specified
    coverage_notes: list[str] = Field(default_factory=list)
    notes: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_eligibility(self) -> EvidenceEligibility:
        return derive_evidence_eligibility(self.document_type, self.source_status)

    @field_validator("file")
    @classmethod
    def _relative_pdf_path(cls, v: str) -> str:
        if "\\" in v or v.startswith("/") or ".." in v.split("/") or ":" in v:
            raise ValueError("file must be a POSIX path relative to the corpus dir")
        if not v.lower().endswith(".pdf"):
            raise ValueError("file must be a .pdf")
        return v

    @field_validator("entity_types", "residency_statuses", "supersedes")
    @classmethod
    def _no_duplicates(cls, v: list[str]) -> list[str]:
        return _unique(v)

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        if (
            self.effective_from
            and self.effective_until
            and self.effective_from > self.effective_until
        ):
            raise ValueError("effective_from is after effective_until")
        if self.source_authority is not None and self.source_authority is not self.bank:
            raise ValueError("source_authority, when known, must match bank")
        if (
            self.source_authority is not None
            and self.attribution_basis is AttributionBasis.FILENAME_ONLY
        ):
            raise ValueError("source_authority cannot be set from the filename alone")
        if self.document_type is DocumentType.REGULATORY and self.bank is not Bank.RBI:
            raise ValueError("regulatory documents must come from RBI (docs/14)")
        if self.source_status is not SourceStatus.ACTIVE and not self.status_reason:
            raise ValueError(f"status_reason is required when source_status={self.source_status}")
        if self.document_key in self.supersedes:
            raise ValueError("a document cannot supersede itself")
        return self

    @property
    def row_id(self) -> uuid.UUID:
        return document_row_id(self.sha256)


class ChunkMetadata(BaseModel):
    """Chunk metadata = Qdrant payload. Document-level fields mirror `DocumentMetadata`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    document_id: str  # = documents.id (row UUID)
    document_key: DocumentKey
    document_name: NonEmptyText
    file: NonEmptyText
    bank: Bank
    document_type: DocumentType
    source_status: SourceStatus
    evidence_eligibility: EvidenceEligibility
    source_authority: Bank | None
    version: str | None
    effective_from: date | None
    effective_until: date | None
    entity_types: list[EntityType]
    residency_statuses: list[ResidencyStatus]
    section: str | None
    page_number: int = Field(ge=1)  # physical PDF page, 1-based
    page_end: int | None = None
    chunk_index: int = Field(ge=0)
    parent_chunk_id: str | None = None
    extraction_method: ExtractionMethod
    content: NonEmptyText

    @model_validator(mode="after")
    def _page_range(self) -> Self:
        if self.page_end is not None and self.page_end < self.page_number:
            raise ValueError("page_end is before page_number")
        return self

    @classmethod
    def for_document(
        cls,
        doc: DocumentMetadata,
        *,
        chunk_index: int,
        page_number: int,
        content: str,
        extraction_method: ExtractionMethod,
        section: str | None = None,
        page_end: int | None = None,
        parent_chunk_id: str | None = None,
        entity_types: list[EntityType] | None = None,
    ) -> "ChunkMetadata":
        """Build a chunk whose document-level fields are copied from `doc`.

        `entity_types` may narrow the document's set (e.g. a company-only paragraph)
        but never add a type the document does not cover.
        """
        types = list(doc.entity_types) if entity_types is None else entity_types
        if not set(types) <= set(doc.entity_types):
            raise ValueError("chunk entity_types must be a subset of the document's")
        return cls(
            chunk_id=str(chunk_id(doc.sha256, chunk_index)),
            document_id=str(doc.row_id),
            document_key=doc.document_key,
            document_name=doc.title,
            file=doc.file,
            bank=doc.bank,
            document_type=doc.document_type,
            source_status=doc.source_status,
            evidence_eligibility=doc.evidence_eligibility,
            source_authority=doc.source_authority,
            version=doc.version,
            effective_from=doc.effective_from,
            effective_until=doc.effective_until,
            entity_types=types,
            residency_statuses=list(doc.residency_statuses),
            section=section,
            page_number=page_number,
            page_end=page_end,
            chunk_index=chunk_index,
            parent_chunk_id=parent_chunk_id,
            extraction_method=extraction_method,
            content=content,
        )

    def to_qdrant_payload(self) -> dict[str, object]:
        # JSON mode: enums -> values, dates -> ISO strings (Qdrant payloads are JSON).
        return self.model_dump(mode="json")
