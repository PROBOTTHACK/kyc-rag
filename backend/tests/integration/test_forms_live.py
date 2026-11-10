"""M2 C1 over the real corpus: deterministic form schemas for all six extractable documents.

Uses the full-corpus throwaway database (tests/integration/conftest.py). The expected
counts are recomputed independently from the stored chunk rows: every chunk is covered
once, field counts equal the non-empty labels of native `Fields:` chunks, required
markers equal labels with exactly one leading `*` and no other `*`, scanned forms have
no structured fields, and two extractions are identical.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Chunk, Document
from app.forms.extract import extract_form, extractable_document_keys
from app.retrieval.qdrant_store import ChunkStore

pytestmark = pytest.mark.integration

KYC_FORMS = {
    "hdfc_non_individual_account_opening",
    "hdfc_nri_account_opening",
    "hdfc_nri_rekyc",
    "hdfc_resident_rekyc_dormant",
    "hdfc_resident_to_nro_conversion",
}
REFERENCE = "hdfc_netbanking_corporate_application"
SCANNED = {"hdfc_non_individual_account_opening", "hdfc_nri_rekyc"}


@pytest.fixture
def sessions(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    return corpus_db  # requesting `corpus_store` guarantees the corpus is ingested


def _expected(rows: list[Chunk]) -> tuple[int, int]:
    """(field count, required count) recomputed from raw rows, independently."""
    fields = required = 0
    for r in rows:
        if r.metadata_.get("content_kind") != "form_fields" or r.text_reliability != "native":
            continue
        for piece in r.content.removeprefix("Fields: ").split(" | "):
            label = piece.strip().lstrip("*").strip()
            if not label:
                continue
            fields += 1
            stripped = piece.strip()
            if stripped.startswith("*") and not stripped.startswith("**") and "*" not in label:
                required += 1
    return fields, required


def test_all_six_documents_extract_with_full_provenance(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        keys = extractable_document_keys(s)
        assert set(keys) == KYC_FORMS | {REFERENCE}
        for key in keys:
            doc = s.scalar(sa.select(Document).where(Document.document_key == key))
            assert doc is not None
            rows = list(
                s.scalars(
                    sa.select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.chunk_index)
                )
            )
            schema = extract_form(s, key)
            # identity and role
            assert schema.document_id == str(doc.id) and schema.document_sha256 == doc.sha256
            assert schema.document_role == (
                "reference_document" if key == REFERENCE else "kyc_form"
            )
            # every stored chunk covered exactly once, in document order
            covered = [cid for sec in schema.sections for cid in sec.chunk_ids]
            assert covered == [str(r.id) for r in rows] and schema.chunk_count == len(rows)
            # counts recomputed from raw rows
            fields, required = _expected(rows)
            assert (schema.field_count, schema.required_as_shown_count) == (fields, required)
            # provenance: pages within the document, chunk ids real
            ids = {str(r.id) for r in rows}
            for sec in schema.sections:
                for f in sec.fields:
                    assert f.chunk_id in ids and 1 <= f.page_number <= (doc.page_count or 0)
                    assert f.mandatory_under_policy is None and f.appears_on_form is True
                for b in sec.text_blocks:
                    assert b.chunk_id in ids and 1 <= b.page_number <= (doc.page_count or 0)
            # determinism
            assert extract_form(s, key).model_dump_json() == schema.model_dump_json()


def test_scanned_forms_are_ocr_text_only(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        for key in SCANNED:
            schema = extract_form(s, key)
            assert schema.field_count == 0
            assert {"no_field_structure", "scanned_document"} <= set(schema.flags)
            blocks = [b for sec in schema.sections for b in sec.text_blocks]
            assert blocks and all(
                b.verification_status in ("ocr_reliable", "ocr_uncertain") for b in blocks
            )
            assert all(b.ocr_confidence is not None for b in blocks)
            assert all("no_field_structure" in sec.flags for sec in schema.sections)


def test_native_forms_and_reference_have_fields(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        for key in (KYC_FORMS - SCANNED) | {REFERENCE}:
            schema = extract_form(s, key)
            assert schema.field_count > 0 and "no_field_structure" not in schema.flags
            assert all(
                f.verification_status == "extracted" for sec in schema.sections for f in sec.fields
            )
        ref = extract_form(s, REFERENCE)
        assert {"reference_document", "context_only"} <= set(ref.flags)
