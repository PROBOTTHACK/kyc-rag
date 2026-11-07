"""Canonical vocabularies shared by the manifest, Postgres rows and Qdrant payloads.

See docs/15-taxonomy.md for definitions and the reasoning behind each value.
Stored values are always the specific members below; group names (e.g. LEGAL_ENTITY)
are expansion sets for querying and are never stored on a document or chunk.
"""

from enum import StrEnum


class Bank(StrEnum):
    """Corpus a document belongs to: the regulator (RBI) or the target bank (HDFC).

    As `bank`, this is project context, not a claimed publisher; the verified publisher
    is `source_authority` (same values, null until verified).
    """

    RBI = "RBI"
    HDFC = "HDFC"


class DocumentType(StrEnum):
    """docs/04 Step 0 categories (docs/14 calls the field `category`)."""

    REGULATORY = "regulatory"
    POLICY = "policy"
    FORM = "form"
    ANNEXURE = "annexure"
    REFERENCE = "reference"


class SourceStatus(StrEnum):
    """Status of a *source document* (docs/14 values plus `unknown`). Never a requirement status."""

    ACTIVE = "active"  # current as far as the corpus shows
    DATED = "dated"  # older basis; may inform but cannot alone support MANDATORY
    SUPERSEDED = "superseded"  # explicitly replaced by a newer document in the corpus
    UNKNOWN = "unknown"  # currency cannot be established from the corpus


class Applicability(StrEnum):
    """Status of a *requirement* (docs/03 `applicability`). Used from M3; never on chunks."""

    MANDATORY = "MANDATORY"
    CONDITIONAL = "CONDITIONAL"
    OPTIONAL = "OPTIONAL"
    UNCLEAR = "UNCLEAR"


class EvidenceEligibility(StrEnum):
    """Whether retrieval may use a document's chunks as compliance evidence (filter, not rank)."""

    PRIMARY = "primary"  # may support MANDATORY on its own
    SUPPORTING = "supporting"  # retrievable evidence; cannot alone support MANDATORY
    CONTEXT_ONLY = "context_only"  # excluded from compliance retrieval unless explicitly requested


class AttributionBasis(StrEnum):
    """How the document's issuer (bank / source_authority) was established."""

    DOCUMENT_TEXT = "document_text"  # issuer named in the extractable text
    DOCUMENT_VISUAL = "document_visual"  # issuer branding visible on a scanned page (not OCR'd)
    FILENAME_ONLY = "filename_only"  # nothing in the document names the issuer


class VersionReviewStatus(StrEnum):
    """Review state of a recorded version relationship. Never set from a hash change:
    a SHA-256 mismatch fails manifest validation instead."""

    NOT_REQUIRED = "not_required"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"


class IngestionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"  # some pages failed; they are recorded, not dropped
    FAILED = "failed"


class DocumentQuality(StrEnum):
    TEXT = "text"
    SCANNED = "scanned"
    MIXED = "mixed"


class ExtractionMethod(StrEnum):
    TEXT = "text"
    OCR = "ocr"


class PageStatus(StrEnum):
    OK = "ok"
    LOW_QUALITY = "low_quality"
    OCR_LOW_CONFIDENCE = "ocr_low_confidence"
    FAILED = "failed"


class EntityType(StrEnum):
    """Legal form / constitution of the customer. Residency is a separate axis."""

    INDIVIDUAL = "INDIVIDUAL"
    SOLE_PROPRIETORSHIP = "SOLE_PROPRIETORSHIP"
    PARTNERSHIP = "PARTNERSHIP"
    LLP = "LLP"
    COMPANY = "COMPANY"
    TRUST = "TRUST"
    HUF = "HUF"
    SOCIETY = "SOCIETY"
    UNINCORPORATED_ASSOCIATION = "UNINCORPORATED_ASSOCIATION"  # AOP / BOI / club
    OTHER_JURIDICAL_PERSON = "OTHER_JURIDICAL_PERSON"  # e.g. local bodies, universities


class EntityGroup(StrEnum):
    NON_INDIVIDUAL = "NON_INDIVIDUAL"
    LEGAL_ENTITY = "LEGAL_ENTITY"


ENTITY_GROUPS: dict[EntityGroup, frozenset[EntityType]] = {
    EntityGroup.NON_INDIVIDUAL: frozenset(EntityType) - {EntityType.INDIVIDUAL},
    # Bodies other than individuals, proprietorships and HUFs. Membership is a query
    # convenience only: it says nothing about which RBI paragraph applies (e.g. LLP).
    EntityGroup.LEGAL_ENTITY: frozenset(
        {
            EntityType.PARTNERSHIP,
            EntityType.LLP,
            EntityType.COMPANY,
            EntityType.TRUST,
            EntityType.SOCIETY,
            EntityType.UNINCORPORATED_ASSOCIATION,
            EntityType.OTHER_JURIDICAL_PERSON,
        }
    ),
}


class ResidencyStatus(StrEnum):
    """Residential status of an individual (FEMA terms as used in the corpus)."""

    RESIDENT = "RESIDENT"
    NRI = "NRI"  # Non-Resident Indian
    PIO = "PIO"  # Person of Indian Origin; the HDFC forms group OCI cardholders here


def expand_entity_terms(terms: list[str]) -> frozenset[EntityType]:
    """Resolve entity types and/or group names to the specific types they denote."""
    out: set[EntityType] = set()
    for term in terms:
        if term in EntityGroup.__members__:
            out |= ENTITY_GROUPS[EntityGroup(term)]
        else:
            out.add(EntityType(term))  # raises ValueError on an unknown term
    return frozenset(out)
