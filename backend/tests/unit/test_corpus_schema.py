"""Document/chunk metadata schemas, eligibility, and cross-store consistency.

All values are synthetic; nothing here is taken from the real corpus.
"""

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.db.corpus import (
    CHUNK_PAYLOAD_RENAMES,
    DOCUMENT_LEVEL_PAYLOAD_FIELDS,
    chunk_row,
    document_row,
)
from app.db.models import Chunk, Document
from app.schemas.corpus import (
    QDRANT_KEYWORD_INDEXES,
    ChunkMetadata,
    DocumentMetadata,
    chunk_id,
    derive_evidence_eligibility,
    document_row_id,
)
from app.schemas.taxonomy import (
    DocumentType,
    EntityType,
    EvidenceEligibility,
    ExtractionMethod,
    SourceStatus,
)

SHA = "a" * 64


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "document_key": "synthetic_doc",
        "file": "regulatory/synthetic.pdf",
        "sha256": SHA,
        "bank": "RBI",
        "document_type": "regulatory",
        "source_authority": "RBI",
        "attribution_basis": "document_text",
        "title": "Synthetic Directions",
        "source_status": "active",
        "entity_types": ["INDIVIDUAL", "COMPANY"],
    }
    base.update(overrides)
    return base


def doc(**overrides: Any) -> DocumentMetadata:
    return DocumentMetadata.model_validate(entry(**overrides))


# --- eligibility -------------------------------------------------------------


@pytest.mark.parametrize(
    ("document_type", "status", "expected"),
    [
        ("regulatory", "active", "primary"),
        ("policy", "active", "primary"),
        ("regulatory", "dated", "supporting"),
        ("regulatory", "superseded", "supporting"),
        ("regulatory", "unknown", "supporting"),
        ("policy", "dated", "supporting"),
        ("form", "active", "supporting"),
        ("annexure", "active", "supporting"),
        ("reference", "active", "context_only"),
        ("reference", "dated", "context_only"),
    ],
)
def test_evidence_eligibility_matrix(document_type: str, status: str, expected: str) -> None:
    got = derive_evidence_eligibility(DocumentType(document_type), SourceStatus(status))
    assert got is EvidenceEligibility(expected)


def test_eligibility_is_derived_not_settable() -> None:
    assert doc().evidence_eligibility is EvidenceEligibility.PRIMARY
    with pytest.raises(ValidationError):
        DocumentMetadata.model_validate(entry(evidence_eligibility="primary"))


# --- document validation -----------------------------------------------------


def test_unknown_metadata_defaults_to_none() -> None:
    d = doc()
    assert (d.version, d.issue_date, d.effective_from, d.effective_until) == (None,) * 4


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "A" * 64},
        {"sha256": "abc"},
        {"file": "/abs/x.pdf"},
        {"file": "C:/x.pdf"},
        {"file": "regulatory\\x.pdf"},
        {"file": "../x.pdf"},
        {"file": "regulatory/x.txt"},
        {"document_key": "Bad-Key"},
        {"bank": "SBI"},
        {"entity_types": []},
        {"entity_types": ["LEGAL_ENTITY"]},  # groups are never stored
        {"entity_types": ["NRI"]},  # residency is a separate axis
        {"entity_types": ["COMPANY", "COMPANY"]},
        {"residency_statuses": ["NRI", "NRI"]},
        {"bank": "HDFC", "source_authority": "HDFC"},  # regulatory must be RBI
        {"source_authority": "HDFC"},  # authority must match bank
        {"attribution_basis": "filename_only"},  # authority not provable from a filename
        {"source_status": "dated"},  # status_reason required
        {"effective_from": "2026-01-02", "effective_until": "2026-01-01"},
        {"supersedes": ["synthetic_doc"]},
        {"surprise_field": 1},
    ],
)
def test_document_rejects(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentMetadata.model_validate(entry(**overrides))


def test_filename_only_attribution_allowed_without_authority() -> None:
    d = doc(
        bank="HDFC",
        document_type="policy",
        source_authority=None,
        attribution_basis="filename_only",
        source_status="dated",
        status_reason="older basis",
    )
    assert d.source_authority is None
    assert d.evidence_eligibility is EvidenceEligibility.SUPPORTING


# --- ids ---------------------------------------------------------------------


def test_ids_are_deterministic_and_content_based() -> None:
    assert document_row_id(SHA) == document_row_id(SHA)
    assert document_row_id(SHA) != document_row_id("b" * 64)
    assert chunk_id(SHA, 0) == chunk_id(SHA, 0)
    assert chunk_id(SHA, 0) != chunk_id(SHA, 1)
    assert doc().row_id == doc(document_key="renamed_doc").row_id  # identity != content


# --- chunks ------------------------------------------------------------------


def make_chunk(d: DocumentMetadata, **kw: Any) -> ChunkMetadata:
    args: dict[str, Any] = {
        "chunk_index": 0,
        "page_number": 3,
        "content": "Synthetic passage.",
        "extraction_method": ExtractionMethod.TEXT,
    }
    args.update(kw)
    return ChunkMetadata.for_document(d, **args)


def test_chunk_copies_document_level_fields() -> None:
    d = doc(version="v1", effective_from=date(2026, 1, 1))
    c = make_chunk(d, section="Chapter I")
    assert c.document_id == str(d.row_id)
    assert (c.bank, c.document_type, c.source_status) == (
        d.bank,
        d.document_type,
        d.source_status,
    )
    assert c.evidence_eligibility is d.evidence_eligibility
    assert (c.document_name, c.version, c.effective_from) == (d.title, "v1", date(2026, 1, 1))
    assert c.entity_types == d.entity_types


def test_chunk_may_narrow_but_not_widen_entity_types() -> None:
    d = doc()
    assert make_chunk(d, entity_types=[EntityType.COMPANY]).entity_types == [EntityType.COMPANY]
    with pytest.raises(ValueError):
        make_chunk(d, entity_types=[EntityType.LLP])


@pytest.mark.parametrize("kw", [{"page_number": 0}, {"page_end": 2}, {"content": "  "}])
def test_chunk_rejects(kw: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        make_chunk(doc(), **kw)


def test_qdrant_payload_is_json_and_indexable() -> None:
    payload = make_chunk(doc(effective_from=date(2026, 1, 1))).to_qdrant_payload()
    json.dumps(payload)  # must not raise
    assert payload["bank"] == "RBI"
    assert payload["source_status"] == "active"
    assert payload["document_type"] == "regulatory"
    assert payload["evidence_eligibility"] == "primary"
    assert payload["effective_from"] == "2026-01-01"
    assert set(QDRANT_KEYWORD_INDEXES) <= payload.keys()


# --- manifest / Postgres / Qdrant consistency --------------------------------


def test_every_payload_field_has_a_postgres_home() -> None:
    chunk_cols = set(Chunk.__table__.columns.keys())
    doc_cols = set(Document.__table__.columns.keys())
    for key in ChunkMetadata.model_fields:
        if key in DOCUMENT_LEVEL_PAYLOAD_FIELDS:
            assert DOCUMENT_LEVEL_PAYLOAD_FIELDS[key] in doc_cols, key
        else:
            assert CHUNK_PAYLOAD_RENAMES.get(key, key) in chunk_cols, key


# Manifest fields kept in documents.metadata (JSONB) instead of their own column.
DOCUMENT_METADATA_JSON_FIELDS = {"supersedes", "coverage_notes", "notes"}


def test_every_manifest_field_has_a_postgres_home() -> None:
    doc_cols = set(Document.__table__.columns.keys())
    fields = set(DocumentMetadata.model_fields) | set(DocumentMetadata.model_computed_fields)
    for key in fields - DOCUMENT_METADATA_JSON_FIELDS:
        assert key in doc_cols, key
    drow = document_row(doc())
    assert set(drow.metadata_) == DOCUMENT_METADATA_JSON_FIELDS


def test_shared_fields_use_the_same_types_in_manifest_and_payload() -> None:
    for key in ("bank", "document_type", "source_status", "evidence_eligibility", "entity_types"):
        doc_ann = (
            DocumentMetadata.model_fields[key].annotation
            if key in DocumentMetadata.model_fields
            else DocumentMetadata.model_computed_fields[key].return_type
        )
        assert ChunkMetadata.model_fields[key].annotation == doc_ann, key


def test_rows_mirror_metadata() -> None:
    d = doc(residency_statuses=["RESIDENT"], coverage_notes=["gap"])
    c = make_chunk(d)
    drow, crow = document_row(d), chunk_row(c)
    payload = c.to_qdrant_payload()

    assert drow.id == d.row_id and drow.sha256 == d.sha256
    assert drow.evidence_eligibility == d.evidence_eligibility.value
    assert drow.metadata_["coverage_notes"] == ["gap"]
    for key, col in DOCUMENT_LEVEL_PAYLOAD_FIELDS.items():
        value = getattr(drow, col)
        assert (value.isoformat() if isinstance(value, date) else value) == payload[key], key
    for key in ChunkMetadata.model_fields:
        if key in DOCUMENT_LEVEL_PAYLOAD_FIELDS:
            continue
        value = getattr(crow, CHUNK_PAYLOAD_RENAMES.get(key, key))
        assert (str(value) if key.endswith("_id") and value else value) == payload[key], key
    # Shared document-level columns agree across both tables.
    for col in ("bank", "document_type", "source_status", "evidence_eligibility", "document_key"):
        assert getattr(drow, col) == getattr(crow, col), col
