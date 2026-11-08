"""Alembic on real Postgres: fresh DB, M0-state DB, ORM/migration drift, and constraints.

Each test gets its own throwaway database on the docker Postgres (`make up`).
"""

import json
import re
import uuid

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.corpus import chunk_row, document_row
from app.db.models import DocumentPage
from app.schemas.corpus import ChunkMetadata, DocumentMetadata
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
from tests.integration.conftest import alembic_cfg, sync_engine

pytestmark = pytest.mark.integration

TABLES = {"documents", "document_pages", "chunks", "document_exclusions", "ingestion_runs"}
# What M0 left behind: `alembic upgrade head` with no revisions creates only this.
M0_STATE_SQL = (
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
)


def tables(url: str) -> set[str]:
    return set(sa.inspect(sync_engine(url)).get_table_names())


def test_fresh_database_upgrade_downgrade_cycle(temp_db: str) -> None:
    cfg = alembic_cfg()
    command.upgrade(cfg, "head")
    assert tables(temp_db) >= TABLES
    command.check(cfg)  # raises if ORM models and migrations disagree
    command.downgrade(cfg, "base")
    assert not TABLES & tables(temp_db)
    command.upgrade(cfg, "head")
    assert tables(temp_db) >= TABLES


def test_upgrade_from_m0_state(temp_db: str) -> None:
    with sync_engine(temp_db).begin() as conn:
        conn.execute(sa.text(M0_STATE_SQL))
    assert tables(temp_db) == {"alembic_version"}
    command.upgrade(alembic_cfg(), "head")
    assert tables(temp_db) >= TABLES
    with sync_engine(temp_db).connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == ScriptDirectory.from_config(alembic_cfg()).get_current_head()


# --- constraints -------------------------------------------------------------


def synthetic_doc(**overrides: object) -> DocumentMetadata:
    data: dict[str, object] = {
        "document_key": "synthetic_doc",
        "file": "regulatory/synthetic.pdf",
        "sha256": uuid.uuid4().hex * 2,
        "bank": "RBI",
        "document_type": "regulatory",
        "source_authority": "RBI",
        "attribution_basis": "document_text",
        "title": "Synthetic Directions",
        "source_status": "active",
        "entity_types": [t.value for t in EntityType],
        "residency_statuses": ["RESIDENT", "NRI", "PIO"],
    }
    data.update(overrides)
    return DocumentMetadata.model_validate(data)


def insert_doc_page_chunk(engine: sa.Engine, doc: DocumentMetadata, page: int = 1) -> None:
    meta = ChunkMetadata.for_document(
        doc,
        chunk_index=0,
        page_number=page,
        content="Synthetic passage.",
        extraction_method=ExtractionMethod.OCR,
        text_reliability=TextReliability.OCR_UNCERTAIN,  # OCR text is never `native`
        ocr_confidence=0.42,
    )
    with Session(engine) as s, s.begin():
        s.add(document_row(doc, page_count=1, quality="scanned"))
        s.flush()
        s.add(
            DocumentPage(
                document_id=doc.row_id,
                page_number=1,
                extraction_method="ocr",
                status="ocr_low_confidence",
                char_count=18,
                image_coverage=1.0,
                ocr_confidence=0.42,
            )
        )
        s.flush()
        s.add(chunk_row(meta))


ENUM_CHECKS: dict[str, type] = {
    "ck_documents_bank": Bank,
    "ck_documents_document_type": DocumentType,
    "ck_documents_source_authority": Bank,
    "ck_documents_attribution_basis": AttributionBasis,
    "ck_documents_source_status": SourceStatus,
    "ck_documents_evidence_eligibility": EvidenceEligibility,
    "ck_documents_version_review_status": VersionReviewStatus,
    "ck_documents_ingestion_status": IngestionStatus,
    "ck_documents_quality": DocumentQuality,
    "ck_documents_entity_types": EntityType,
    "ck_documents_residency": ResidencyStatus,
    "ck_document_pages_extraction_method": ExtractionMethod,
    "ck_document_pages_status": PageStatus,
    "ck_chunks_bank": Bank,
    "ck_chunks_document_type": DocumentType,
    "ck_chunks_source_status": SourceStatus,
    "ck_chunks_evidence_eligibility": EvidenceEligibility,
    "ck_chunks_extraction_method": ExtractionMethod,
    "ck_chunks_entity_types": EntityType,
    "ck_chunks_residency": ResidencyStatus,
    "ck_chunks_chunk_role": ChunkRole,
    "ck_chunks_text_reliability": TextReliability,
    "ck_ingestion_runs_status": IngestionStatus,
    "ck_ingestion_runs_failed_stage": IngestionStage,
}


def test_db_allowed_values_equal_python_enums(migrated: sa.Engine) -> None:
    """Manifest (Pydantic enums) and Postgres CHECKs must allow exactly the same values."""
    with migrated.connect() as conn:
        defs = dict(
            conn.execute(
                sa.text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = ANY(:names)"
                ),
                {"names": list(ENUM_CHECKS)},
            ).all()
        )
    assert set(defs) == set(ENUM_CHECKS)
    for name, enum in ENUM_CHECKS.items():
        allowed = set(re.findall(r"'([^']+)'::text", defs[name]))
        assert allowed == {m.value for m in enum}, name


def test_every_current_enum_value_is_accepted(migrated: sa.Engine) -> None:
    for dt in DocumentType:
        for st in SourceStatus:
            if dt is DocumentType.REGULATORY:
                bank, authority = "RBI", "RBI"
            else:
                bank, authority = "HDFC", "HDFC"
            doc = synthetic_doc(
                document_key=f"d_{dt.value}_{st.value}",
                document_type=dt.value,
                source_status=st.value,
                status_reason=None if st is SourceStatus.ACTIVE else "synthetic",
                bank=bank,
                source_authority=authority,
            )
            insert_doc_page_chunk(migrated, doc)
    with migrated.connect() as conn:
        n = conn.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one()
    assert n == len(DocumentType) * len(SourceStatus)


def test_chunk_round_trip_keeps_provenance(migrated: sa.Engine) -> None:
    doc = synthetic_doc()
    insert_doc_page_chunk(migrated, doc)
    with migrated.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT c.bank, c.source_status, c.document_type, c.evidence_eligibility, "
                "c.page_number, c.extraction_method, d.sha256, p.ocr_confidence "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "JOIN document_pages p ON p.document_id = c.document_id "
                "AND p.page_number = c.page_number"
            )
        ).one()
    assert tuple(row) == ("RBI", "active", "regulatory", "primary", 1, "ocr", doc.sha256, 0.42)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE documents SET bank = 'SBI'",
        "UPDATE documents SET source_status = 'current'",
        "UPDATE documents SET evidence_eligibility = 'maybe'",
        "UPDATE documents SET source_authority = 'HDFC'",  # must match bank
        "UPDATE documents SET entity_types = ARRAY['LEGAL_ENTITY']",
        "UPDATE documents SET residency_statuses = ARRAY['NRO']",
        "UPDATE documents SET sha256 = 'not-a-hash'",
        "UPDATE documents SET effective_from = '2026-02-01', effective_until = '2026-01-01'",
        "UPDATE document_pages SET status = 'failed'",  # failed page without an error
        "UPDATE document_pages SET ocr_confidence = 1.5",
        "UPDATE chunks SET page_number = 2",  # no such page recorded for the document
        "UPDATE chunks SET page_end = 0",
        "UPDATE chunks SET entity_types = ARRAY['NRI']",
        "UPDATE chunks SET content = ''",
        "UPDATE chunks SET source_status = 'archived'",
    ],
)
def test_constraints_reject(migrated: sa.Engine, sql: str) -> None:
    insert_doc_page_chunk(migrated, synthetic_doc())
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(sa.text(sql))


def test_duplicate_file_content_rejected(migrated: sa.Engine) -> None:
    doc = synthetic_doc()
    insert_doc_page_chunk(migrated, doc)
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO documents (id, document_key, file, sha256, bank, document_type, "
                "attribution_basis, title, source_status, evidence_eligibility, entity_types) "
                "VALUES (gen_random_uuid(), 'other_key', 'x.pdf', :sha, 'RBI', 'regulatory', "
                "'document_text', 't', 'active', 'primary', ARRAY['INDIVIDUAL'])"
            ),
            {"sha": doc.sha256},
        )


def test_second_version_of_same_document_key_is_rejected(migrated: sa.Engine) -> None:
    # M1 keeps one version per document_key: a new file under an existing key must fail
    # loudly, not overwrite the stored version or coexist unlinked.
    first = synthetic_doc()
    insert_doc_page_chunk(migrated, first)
    second = synthetic_doc()  # same document_key, different sha256
    assert second.sha256 != first.sha256
    with pytest.raises(IntegrityError), Session(migrated) as s, s.begin():
        s.add(document_row(second))
    with migrated.connect() as conn:
        rows = conn.execute(sa.text("SELECT sha256 FROM documents")).scalars().all()
    assert rows == [first.sha256]


def test_same_content_reingest_maps_to_same_ids(migrated: sa.Engine) -> None:
    doc = synthetic_doc()
    insert_doc_page_chunk(migrated, doc)
    with Session(migrated) as s, s.begin():
        s.merge(document_row(doc))  # idempotent upsert by content-derived id
    with migrated.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM documents")).scalar_one() == 1


def test_failed_page_is_recorded_with_reason(migrated: sa.Engine) -> None:
    doc = synthetic_doc()
    insert_doc_page_chunk(migrated, doc)
    with Session(migrated) as s, s.begin():
        s.add(
            DocumentPage(
                document_id=doc.row_id,
                page_number=2,
                status=PageStatus.FAILED.value,
                error="OCR produced no text",
            )
        )
    with migrated.connect() as conn:
        status = conn.execute(
            sa.text("SELECT status, error FROM document_pages WHERE page_number = 2")
        ).one()
    assert tuple(status) == ("failed", "OCR produced no text")


# --- 0002: data preservation and parent/child constraints ---------------------------

OLD_STYLE_PARENT_METADATA = {
    "paragraph": "7",
    "chunk_role": "parent",
    "text_reliability": "native",
    "embedding_eligible": False,
    "can_support_mandatory": True,
    "raw_text": "7. Full\nparagraph.",
}


def test_0002_backfills_promoted_columns_and_downgrade_restores_them(temp_db: str) -> None:
    cfg = alembic_cfg()
    command.upgrade(cfg, "0001_m1_corpus")
    engine = sync_engine(temp_db)
    doc = synthetic_doc()
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO documents (id, document_key, file, sha256, bank, document_type, "
                "attribution_basis, title, source_status, evidence_eligibility, entity_types) "
                "VALUES (:id, 'k', 'f.pdf', :sha, 'RBI', 'regulatory', 'document_text', 't', "
                "'active', 'primary', ARRAY['INDIVIDUAL'])"
            ),
            {"id": doc.row_id, "sha": doc.sha256},
        )
        conn.execute(sa.text(
            "INSERT INTO document_pages (document_id, page_number, status) "
            "VALUES (:id, 1, 'ok')"),  # fmt: skip
            {"id": doc.row_id},
        )
        insert = sa.text(
            "INSERT INTO chunks (id, document_id, chunk_index, document_key, bank, "
            "document_type, source_status, evidence_eligibility, page_number, entity_types, "
            "extraction_method, content, content_sha256, parent_chunk_id, metadata) VALUES "
            "(:id, :doc, :idx, 'k', 'RBI', 'regulatory', 'active', 'primary', 1, "
            "ARRAY['INDIVIDUAL'], 'text', :content, 'x', :parent, CAST(:meta AS jsonb))"
        )
        conn.execute(
            insert,
            {"id": parent_id, "doc": doc.row_id, "idx": 0, "content": "7. Full paragraph.",
             "parent": None, "meta": json.dumps(OLD_STYLE_PARENT_METADATA)},
        )  # fmt: skip
        conn.execute(
            insert,
            {"id": child_id, "doc": doc.row_id, "idx": 1, "content": "Full paragraph.",
             "parent": parent_id, "meta": json.dumps({"paragraph": "7"})},
        )  # fmt: skip

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT chunk_index, chunk_role, text_reliability, embedding_eligible, "
                "can_support_mandatory, raw_text, metadata FROM chunks ORDER BY chunk_index"
            )
        ).all()
    assert rows[0][:6] == (0, "parent", "native", False, True, "7. Full\nparagraph.")
    assert rows[0][6] == {"paragraph": "7"}  # promoted keys left the JSON
    # Missing values fall back to the ChunkMetadata defaults (a child of a parent).
    assert rows[1][:6] == (1, "child", "native", True, True, None)

    command.downgrade(cfg, "0001_m1_corpus")
    with engine.connect() as conn:
        meta = conn.execute(sa.text("SELECT metadata FROM chunks WHERE chunk_index = 0")).scalar()
    assert meta == OLD_STYLE_PARENT_METADATA


def _family(engine: sa.Engine) -> tuple[DocumentMetadata, str, str]:
    """A synthetic document with one parent (index 0) and one child (index 1)."""
    doc = synthetic_doc()
    parent = ChunkMetadata.for_document(
        doc, chunk_index=0, page_number=1, content="7. Whole paragraph.",
        extraction_method=ExtractionMethod.TEXT, chunk_role=ChunkRole.PARENT,
        embedding_eligible=False,
    )  # fmt: skip
    child = ChunkMetadata.for_document(
        doc, chunk_index=1, page_number=1, content="Whole paragraph.",
        extraction_method=ExtractionMethod.TEXT, chunk_role=ChunkRole.CHILD,
        parent_chunk_id=parent.chunk_id,
    )  # fmt: skip
    with Session(engine) as s, s.begin():
        s.add(document_row(doc, page_count=1))
        s.flush()
        s.add(DocumentPage(document_id=doc.row_id, page_number=1, status="ok"))
        s.flush()
        s.add_all([chunk_row(child), chunk_row(parent)])  # order is irrelevant: deferred FK
    return doc, parent.chunk_id, child.chunk_id


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM chunks WHERE chunk_index = 0",  # parent with a live child
        "UPDATE chunks SET parent_chunk_id = NULL WHERE chunk_index = 1",  # child, no parent
        "UPDATE chunks SET parent_chunk_id = gen_random_uuid() WHERE chunk_index = 1",
        "UPDATE chunks SET parent_chunk_id = id WHERE chunk_index = 1",
        "UPDATE chunks SET embedding_eligible = true WHERE chunk_index = 0",  # parent
        "UPDATE chunks SET can_support_mandatory = false WHERE chunk_index = 1",
        "UPDATE chunks SET chunk_role = 'sibling'",
        "UPDATE chunks SET text_reliability = 'ocr_reliable' WHERE chunk_index = 1",  # text
    ],
)
def test_parent_child_constraints_reject(migrated: sa.Engine, sql: str) -> None:
    _family(migrated)
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(sa.text(sql))


def test_child_cannot_point_into_another_document(migrated: sa.Engine) -> None:
    _, parent_id, _ = _family(migrated)
    other = synthetic_doc(document_key="other_doc")
    single = ChunkMetadata.for_document(
        other, chunk_index=0, page_number=1, content="Other.",
        extraction_method=ExtractionMethod.TEXT,
    )  # fmt: skip
    row = chunk_row(single)
    row.chunk_role, row.parent_chunk_id = "child", uuid.UUID(parent_id)
    with pytest.raises(IntegrityError), Session(migrated) as s, s.begin():
        s.add(document_row(other, page_count=1))
        s.flush()
        s.add(DocumentPage(document_id=other.row_id, page_number=1, status="ok"))
        s.flush()
        s.add(row)


def test_deleting_a_whole_family_together_is_allowed(migrated: sa.Engine) -> None:
    _family(migrated)
    with migrated.begin() as conn:
        conn.execute(sa.text("DELETE FROM chunks WHERE chunk_index = 0"))
        conn.execute(sa.text("DELETE FROM chunks WHERE chunk_index = 1"))
    with migrated.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one() == 0


def test_failed_run_needs_stage_and_error(migrated: sa.Engine) -> None:
    doc = synthetic_doc()
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ingestion_runs (id, document_key, sha256, document_id, command, "
                "status, finished_at) VALUES (gen_random_uuid(), 'k', :sha, :id, 'ingest', "
                "'failed', now())"
            ),
            {"sha": doc.sha256, "id": doc.row_id},
        )
