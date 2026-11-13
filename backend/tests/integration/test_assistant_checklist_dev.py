"""A4 on the development stack (real Postgres, real dense retriever; read-only).

1. Every catalogue quote occurs verbatim in its stored HDFC chunk, on the claimed document
   and page; OCR-uncertain text is exactly the scanned form's.
2. Every covered combination and the conversion build a checklist with no dropped item;
   every citation is HDFC and comes from the cell's permitted sources; scanned-form
   evidence is never quotable; nothing is policy-supported.
Like the other dev-stack tests it fails (never skips) when the stack is unavailable.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.assistant.catalogue import catalogue
from app.assistant.checklist import AnswerKind, ChecklistService, Classification
from app.assistant.chunks import PostgresChunkSource
from app.assistant.coverage import coverage
from app.assistant.retrieval import AssistantRetriever
from app.config import get_settings
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.retrieval.factory import retriever_from_settings
from tests.unit.test_assistant_checklist import covered_profiles, items

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_sync_engine(get_settings())
    try:
        yield create_sync_sessionmaker(engine)
    finally:
        engine.dispose()


def test_every_catalogue_quote_is_stored_text(sessions: sessionmaker[Session]) -> None:
    cat, cov = catalogue(), coverage()
    evidence = [(i.id, ev) for i in cat.items for ev in i.evidence]
    stored = PostgresChunkSource(sessions).get({ev.chunk_id for _, ev in evidence})
    for item_id, ev in evidence:
        chunk = stored.get(ev.chunk_id)
        assert chunk is not None, f"{item_id}: {ev.chunk_id} not stored"
        assert chunk.bank == "HDFC" and chunk.document_key == ev.document_key, item_id
        assert chunk.page == ev.page, item_id
        assert ev.quote in chunk.content, f"{item_id}: quote not in stored text"
        scanned = cov.documents[ev.document_key].scanned
        assert (chunk.text_reliability == "ocr_uncertain") is scanned, item_id


def test_real_checklists_cite_only_permitted_hdfc_evidence(sessions: sessionmaker[Session]) -> None:
    settings = get_settings()
    retriever = AssistantRetriever(retriever_from_settings(settings, sessions=sessions))
    service = ChecklistService(PostgresChunkSource(sessions), retriever)
    cov = coverage()
    for p in covered_profiles():
        a = service.build(p)
        label = f"{p.customer_category}/{p.account_type}"
        assert a.kind is AnswerKind.CHECKLIST, label
        assert a.diagnostics.dropped == () and not a.diagnostics.retrieval_degraded, label
        permitted = {s.document_key for s in a.sources}
        for item in items(a):
            assert item.classification is not Classification.POLICY_SUPPORTED
            for c in [*item.citations, *item.related_citations]:
                assert c.document_key in permitted, (label, item.id)
                assert not c.document_key.startswith("rbi_")
                assert c.quotable is (not cov.documents[c.document_key].scanned), (label, item.id)
            if item.source_wording is not None:
                assert any(item.source_wording in c.passage for c in item.citations)
