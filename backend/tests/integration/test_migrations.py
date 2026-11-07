"""Alembic on real Postgres: fresh DB, M0-state DB, ORM/migration drift, and constraints.

Each test gets its own throwaway database on the docker Postgres (`make up`).
"""

import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.config import get_settings
from app.db.corpus import chunk_row, document_row
from app.db.models import DocumentPage
from app.schemas.corpus import ChunkMetadata, DocumentMetadata
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

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
TABLES = {"documents", "document_pages", "chunks"}
# What M0 left behind: `alembic upgrade head` with no revisions creates only this.
M0_STATE_SQL = (
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
)


@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    base = make_url(get_settings().DATABASE_URL.get_secret_value())
    name = f"kyc_test_{uuid.uuid4().hex[:12]}"
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = base.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()  # alembic env.py reads get_settings()
    try:
        yield url
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def alembic_cfg() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def sync_engine(url: str) -> sa.Engine:
    return sa.create_engine(url, poolclass=sa.pool.NullPool)


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
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0001_m1_corpus"
        )


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


@pytest.fixture
def migrated(temp_db: str) -> sa.Engine:
    command.upgrade(alembic_cfg(), "head")
    return sync_engine(temp_db)


def insert_doc_page_chunk(engine: sa.Engine, doc: DocumentMetadata, page: int = 1) -> None:
    meta = ChunkMetadata.for_document(
        doc,
        chunk_index=0,
        page_number=page,
        content="Synthetic passage.",
        extraction_method=ExtractionMethod.OCR,
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
