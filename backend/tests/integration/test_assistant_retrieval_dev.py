"""A2 on the development stack (real model, real Postgres and Qdrant; read-only).

1. Every chunk the coverage map and the evaluation set cite exists in Postgres, is HDFC,
   and has the document key and page the map claims (evidence or nothing).
2. Through the real retriever, in every mode, even queries aimed at RBI material return
   only HDFC chunks of the scope's documents, with full source metadata.
Like the other dev-stack tests it fails (never skips) when the stack is unavailable.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.assistant.coverage import coverage
from app.assistant.evaluation import load_cases
from app.assistant.retrieval import AssistantRetriever
from app.assistant.taxonomy import AccountType, CustomerCategory, EvidenceScope, SourceRole
from app.config import get_settings
from app.db.models import Chunk, Document
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.retrieval.factory import retriever_from_settings
from app.retrieval.retrieve import RetrievalMode, Retriever

pytestmark = pytest.mark.integration

C, A = CustomerCategory, AccountType
RBI_AIMED = [
    "RBI Master Direction on KYC customer due diligence",
    "Reserve Bank of India officially valid documents list",
    "video based customer identification process V-CIP",
    "periodic updation of KYC for high risk customers RBI",
]


@pytest.fixture(scope="module")
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_sync_engine(get_settings())
    try:
        yield create_sync_sessionmaker(engine)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def base(sessions: sessionmaker[Session]) -> Retriever:
    return retriever_from_settings(get_settings(), sessions=sessions)


def stored(sessions: sessionmaker[Session], ids: set[str]) -> dict[str, tuple[str, str, int]]:
    with sessions() as s:
        rows = s.execute(
            sa.select(Chunk.id, Document.document_key, Document.bank, Chunk.page_number)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.id.in_(ids))
        ).all()
    return {str(cid): (key, bank, page) for cid, key, bank, page in rows}


def test_every_cited_chunk_resolves_to_the_claimed_hdfc_document_and_page(
    sessions: sessionmaker[Session],
) -> None:
    cov = coverage()
    evidence = list(cov.all_evidence())
    found = stored(sessions, {e.chunk_id for e in evidence})
    for e in evidence:
        assert e.chunk_id in found, f"coverage evidence {e.chunk_id} not in Postgres"
        key, bank, page = found[e.chunk_id]
        assert bank == "HDFC" and key == e.document_key, e.chunk_id
        assert page == e.page, (e.chunk_id, page, e.page)

    for e in evidence:
        if e.schema_ref is None:
            continue
        ref = e.schema_ref
        with sessions() as s:
            fields = s.execute(
                sa.text(
                    "SELECT jsonb_path_query(schema_json, 'strict $.**.fields[*]') "
                    "FROM form_schemas WHERE form_key = :k "
                    "AND extraction_method = :m AND extractor_version = :v"
                ),
                {"k": ref.form_key, "m": ref.extraction_method, "v": ref.extractor_version},
            ).scalars()
            match = [
                f for f in fields if f.get("label") == ref.label and f.get("chunk_id") == e.chunk_id
            ]
        assert match, f"schema field {ref.label!r} for {e.chunk_id} not stored"
        assert match[0]["page_number"] == e.page
        assert (match[0]["verification_status"] == "corroborated") is e.corroborated

    cases = load_cases()
    gold = {g for c in cases for g in c.gold}
    found = stored(sessions, gold)
    for c in cases:
        if not c.gold:
            continue
        scope = EvidenceScope(c.scope)
        allowed = set(cov.documents_for(scope, c.category, c.account_type))
        for g in c.gold:
            assert g in found, f"{c.id}: gold {g} not in Postgres"
            key, bank, _ = found[g]
            assert bank == "HDFC" and key in allowed, (c.id, g, key)


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_real_retrieval_returns_only_in_scope_hdfc_chunks(
    base: Retriever, mode: RetrievalMode
) -> None:
    retriever = AssistantRetriever(base, mode=mode)
    hdfc_keys = set(coverage().documents)
    for q in RBI_AIMED:
        runs = [
            (retriever.opening(q, C.NRI_INDIVIDUAL, A.SAVINGS), {SourceRole.OPENING_FORM}),
            (retriever.opening(q, C.LLP, A.CURRENT), {SourceRole.OPENING_FORM}),
            (retriever.conversion(q), {SourceRole.CONVERSION_FORM}),
            (retriever.context(q, C.NRI_INDIVIDUAL), {SourceRole.CONTEXT}),
            (retriever.legacy(q), {SourceRole.LEGACY_REFERENCE}),
        ]
        for r, roles in runs:
            assert r.searched and r.hits, (mode, q, r.scope)
            for h in r.hits:
                assert h.document_key in r.document_keys and h.document_key in hdfc_keys
                assert not h.document_key.startswith("rbi_")
                assert h.source_role in roles
                assert h.page >= 1 and h.extraction_method and h.text_reliability
        resident = retriever.opening(q, C.RESIDENT_INDIVIDUAL, A.SAVINGS)
        assert not resident.searched and resident.hits == ()
