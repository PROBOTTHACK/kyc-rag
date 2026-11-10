"""M2 A1 on real services: BM25 keyword retrieval over the full corpus in Postgres.

A throwaway database is migrated and the whole corpus is ingested (Stages A-C from the
real PDFs, Postgres, and a throwaway collection on the docker Qdrant; vectors come from
the deterministic `FakeEmbedder`, since only payloads and filters matter here). The
development database and collection are never touched. Covers:

- corpus count: BM25 corpus = Postgres eligible chunks = Qdrant points (429);
- payload parity: the Postgres-built payload of every chunk equals its Qdrant payload
  (minus the `index` bookkeeping), so BM25 and dense hits cite identically;
- filter parity: `SearchFilters.matches` selects exactly the point ids Qdrant's
  `build_filter` selects, over the full cartesian product of representative values of
  every filter (2 x 2 x 4 x 4 x 4 = 256 combinations, so every pairwise and higher-order
  interaction of these values is exercised), on the Qdrant server itself;
- freshness: citation-, payload-, text-, filter- and parent-related row changes rebuild
  the index without any change of chunk id or content hash; INSERT, UPDATE (including a
  no-op UPDATE) and DELETE of chunk and document rows rebuild it (M2 A2 xmin version); an
  unreachable database is reported and never answered from a stale index;
- the `search --mode bm25 --expand` CLI with citations and parent expansion.
"""

import hashlib
import itertools
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from qdrant_client import models as qm
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import REPO_ROOT, get_settings
from app.db.models import Chunk, Document
from app.db.repository import chunk_corpus_fingerprint, eligible_chunk_payloads
from app.ingestion import cli
from app.retrieval.keyword import (
    KeywordIndexManager,
    KeywordIndexUnavailable,
    payload_without_index,
)
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.search import SearchFilters, build_filter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (REPO_ROOT / "data" / "raw_pdfs" / "regulatory" / "169MD.pdf").exists(),
        reason="source PDFs not present",
    ),
]
EXPECTED_ELIGIBLE = 429

# Representative values per filter dimension (the full product is tested).
CONTEXT_ONLY = (False, True)
MANDATORY_ONLY = (False, True)
BANKS: tuple[tuple[str, ...], ...] = ((), ("RBI",), ("HDFC",), ("RBI", "HDFC"))
DOCUMENT_KEYS: tuple[tuple[str, ...], ...] = (
    (),
    ("rbi_kyc_directions_2025",),  # primary, the only MANDATORY-capable source
    ("rbi_kyc_faqs", "hdfc_nri_rekyc"),  # across banks; supporting; OCR text
    ("hdfc_netbanking_corporate_application",),  # context_only
)
ENTITY_TERMS: tuple[tuple[str, ...], ...] = (
    (),
    ("TRUST",),
    ("LEGAL_ENTITY",),  # group expansion
    ("INDIVIDUAL", "COMPANY"),  # several types: any overlap
)


@pytest.fixture
def store(corpus_store: ChunkStore) -> ChunkStore:
    return corpus_store


@pytest.fixture
def sessions(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    return corpus_db  # requesting `corpus_store` guarantees the corpus is ingested


def qdrant_payloads(store: ChunkStore) -> dict[str, dict[str, Any]]:
    points, nxt = store.client.scroll(store.collection, limit=2000, with_payload=True)
    assert nxt is None
    return {str(p.id): p.payload or {} for p in points}


def qdrant_ids(store: ChunkStore, flt: qm.Filter) -> set[str]:
    points, nxt = store.client.scroll(
        store.collection, scroll_filter=flt, limit=2000, with_payload=False
    )
    assert nxt is None
    return {str(p.id) for p in points}


# --- corpus, payloads, filters -----------------------------------------------------------------


def test_bm25_corpus_equals_postgres_eligible_and_qdrant(
    sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    index = KeywordIndexManager(sessions).get()
    with sessions() as s:
        eligible = s.scalar(sa.select(sa.func.count()).where(Chunk.embedding_eligible.is_(True)))
        parents = s.scalar(sa.select(sa.func.count()).where(Chunk.chunk_role == "parent"))
    assert len(index) == eligible == store.count() == EXPECTED_ELIGIBLE
    assert parents and not any(p["chunk_role"] == "parent" for p in index.payloads)
    print(f"\nBM25 index build over {len(index)} chunks: {index.build_seconds:.3f} s")


def test_payload_parity_with_qdrant(sessions: sessionmaker[Session], store: ChunkStore) -> None:
    with sessions() as s:
        pg = {p["chunk_id"]: p for p in eligible_chunk_payloads(s)}
    qd = {cid: payload_without_index(p) for cid, p in qdrant_payloads(store).items()}
    assert pg.keys() == qd.keys() and len(pg) == EXPECTED_ELIGIBLE
    assert [cid for cid in pg if pg[cid] != qd[cid]] == []


def test_filter_parity_with_qdrant_over_the_full_matrix(
    sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    with sessions() as s:
        payloads = eligible_chunk_payloads(s)
    combos = list(
        itertools.product(CONTEXT_ONLY, MANDATORY_ONLY, BANKS, DOCUMENT_KEYS, ENTITY_TERMS)
    )
    assert len(combos) == 256
    mismatches, sizes = [], []
    for ctx, mand, banks, docs, ents in combos:
        f = SearchFilters(
            include_context_only=ctx,
            mandatory_evidence_only=mand,
            banks=banks,
            document_keys=docs,
            entity_terms=ents,
        )
        python = {p["chunk_id"] for p in payloads if f.matches(p)}
        qdrant = qdrant_ids(store, build_filter(f))
        sizes.append(len(python))
        if python != qdrant:
            mismatches.append((f, len(python), len(qdrant)))
    assert mismatches == []
    # The matrix is meaningful: empty, partial and large selections all occur.
    assert 0 in sizes and max(sizes) > 400 and len(set(sizes)) > 20


def test_bm25_hits_respect_filters_on_the_real_corpus(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions)
    default = m.search("NetBanking facility resolution authorised signatory", top_k=20)
    assert default and all(h.payload["evidence_eligibility"] != "context_only" for h in default)
    with_ctx = m.search(
        "NetBanking facility resolution authorised signatory",
        top_k=20,
        filters=SearchFilters(include_context_only=True),
    )
    assert any(
        h.payload["document_key"] == "hdfc_netbanking_corporate_application" for h in with_ctx
    )
    trust = m.search(
        "documents for opening an account", top_k=20, filters=SearchFilters(entity_terms=("TRUST",))
    )
    assert trust and all("TRUST" in h.payload["entity_types"] for h in trust)
    mand = m.search(
        "officially valid document", top_k=20, filters=SearchFilters(mandatory_evidence_only=True)
    )
    assert mand and all(h.payload["can_support_mandatory"] is True for h in mand)


def test_search_cli_bm25_cites_and_expands_parents(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    with sessions() as s:
        child = s.scalars(
            sa.select(Chunk)
            .where(Chunk.chunk_role == "child", Chunk.document_key == "rbi_kyc_directions_2025")
            .order_by(Chunk.chunk_index)
            .limit(1)
        ).one()
        parent = s.get(Chunk, child.parent_chunk_id)
    assert parent is not None
    query = " ".join(child.content.split()[:40])
    assert (
        cli._search(
            get_settings(),
            query,
            top_k=5,
            filters=SearchFilters(),
            expand=True,
            mode="bm25",
            sessions=sessions,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "1. [" in out and "rbi_kyc_directions_2025, p" in out and "; bm25)" in out
    assert "parent paragraph" in out and " ".join(parent.content.split())[:60] in " ".join(
        out.split()
    )


# --- freshness: the fingerprint covers everything that reaches the index -----------------------


def _first_eligible(s: Session, role: str = "single") -> Chunk:
    return s.scalars(
        sa.select(Chunk)
        .where(Chunk.embedding_eligible.is_(True), Chunk.chunk_role == role)
        .order_by(Chunk.document_key, Chunk.chunk_index)
        .limit(1)
    ).one()


def _payload(m: KeywordIndexManager, chunk_id: uuid.UUID) -> dict[str, Any]:
    return next(p for p in m.get().payloads if p["chunk_id"] == str(chunk_id))


@pytest.mark.parametrize(
    "change",
    [
        "paragraph",  # citation field in chunks.metadata (JSONB)
        "page_end",  # citation column
        "section",  # payload column
        "document_title",  # document-level payload field (document_name)
        "document_version",  # document-level citation field
        "context",  # indexed text outside `content`
        "entity_types",  # filter field
        "parent_content",  # a parent row (not indexed; read for expansion)
    ],
)
def test_index_rebuilds_when_a_relevant_field_changes(
    sessions: sessionmaker[Session], change: str
) -> None:
    m = KeywordIndexManager(sessions)
    before = m.get()
    with sessions() as s:
        before_fp = chunk_corpus_fingerprint(s)
        role = "child" if change in ("context", "parent_content") else "single"
        row = _first_eligible(s, role)
        cid, sha, doc_id = row.id, row.content_sha256, row.document_id
        original: dict[str, Any] = {
            "metadata": dict(row.metadata_),
            "page_end": row.page_end,
            "section": row.section,
            "entity_types": list(row.entity_types),
        }
        doc = s.get(Document, doc_id)
        assert doc is not None
        original |= {"title": doc.title, "version": doc.version}
        parent = s.get(Chunk, row.parent_chunk_id) if row.parent_chunk_id else None
        if parent is not None:
            original["parent_content"] = parent.content
        if change == "paragraph":
            row.metadata_ = {**row.metadata_, "paragraph": "TEST-99"}
        elif change == "page_end":
            row.page_end = row.page_number + 1
        elif change == "section":
            row.section = "Test section"
        elif change == "document_title":
            doc.title = "Changed title"
        elif change == "document_version":
            doc.version = "Test version"
        elif change == "context":
            row.metadata_ = {**row.metadata_, "context": "Changed lead"}
        elif change == "entity_types":
            row.entity_types = ["INDIVIDUAL"]
        elif change == "parent_content":
            assert parent is not None
            parent.content = parent.content + " "
        s.commit()
    try:
        with sessions() as s:
            same = s.get(Chunk, cid)
            assert same is not None and same.content_sha256 == sha  # id and hash unchanged
        after = m.get()
        assert m.rebuilds == 2, f"{change}: index was not rebuilt"
        p = _payload(m, cid)
        expected = {
            "paragraph": ("paragraph", "TEST-99"),
            "section": ("section", "Test section"),
            "document_title": ("document_name", "Changed title"),
            "document_version": ("version", "Test version"),
            "context": ("context", "Changed lead"),
            "entity_types": ("entity_types", ["INDIVIDUAL"]),
        }.get(change)
        if expected:
            assert p[expected[0]] == expected[1]
        assert after.fingerprint != before.fingerprint
    finally:
        with sessions() as s:
            back = s.get(Chunk, cid)
            back_doc = s.get(Document, doc_id)
            assert back is not None and back_doc is not None
            back.metadata_, back.page_end = original["metadata"], original["page_end"]
            back.section, back.entity_types = original["section"], original["entity_types"]
            back_doc.title, back_doc.version = original["title"], original["version"]
            if "parent_content" in original:
                par = s.get(Chunk, back.parent_chunk_id)
                assert par is not None
                par.content = original["parent_content"]
            s.commit()
        with sessions() as s:
            assert chunk_corpus_fingerprint(s) == before_fp  # restored exactly


def test_unchanged_rows_do_not_rebuild(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions)
    first = m.get()
    assert m.get() is first and m.get() is first and m.rebuilds == 1


def test_unreachable_database_never_serves_a_stale_index(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions)
    m.get()
    bad = make_url(get_settings().DATABASE_URL.get_secret_value()).set(port=1)
    broken = sessionmaker(sa.create_engine(bad, connect_args={"connect_timeout": 2}))
    m._sessions = broken
    with pytest.raises(KeywordIndexUnavailable):
        m.search("trust deed")
    m._sessions = sessions
    m.get()
    assert m.rebuilds == 2  # rebuilt after recovery, the old index was dropped


# --- freshness (M2 A2): the xmin version detects INSERT, UPDATE and DELETE ---------------------


def _copy(row: Any, model: Any, **changes: Any) -> Any:
    cols = {a.key: getattr(row, a.key) for a in sa.inspect(model).column_attrs}
    return model(**(cols | changes))


def test_chunk_insert_and_delete_rebuild(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions)
    size = len(m.get())
    marker = "zzqxfreshnessmarker"
    with sessions() as s:
        src = _first_eligible(s)
        top = s.scalar(
            sa.select(sa.func.max(Chunk.chunk_index)).where(Chunk.document_id == src.document_id)
        )
        new_id = uuid.uuid4()
        text = f"{marker} inserted test chunk"
        s.add(
            _copy(
                src,
                Chunk,
                id=new_id,
                chunk_index=(top or 0) + 1,
                content=text,
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
        s.commit()
    try:
        assert len(m.get()) == size + 1 and m.rebuilds == 2  # INSERT
        assert [h.chunk_id for h in m.search(marker)] == [str(new_id)]
    finally:
        with sessions() as s:
            s.execute(sa.delete(Chunk).where(Chunk.id == new_id))
            s.commit()
    assert len(m.get()) == size and m.rebuilds == 3  # DELETE
    assert m.search(marker) == []


def test_existing_chunk_delete_and_restore_rebuild(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions)
    size = len(m.get())
    with sessions() as s:
        before_fp = chunk_corpus_fingerprint(s)
        row = _first_eligible(s)
        saved = _copy(row, Chunk)
        s.execute(sa.delete(Chunk).where(Chunk.id == row.id))
        s.commit()
    assert len(m.get()) == size - 1 and m.rebuilds == 2  # DELETE of a stored chunk
    with sessions() as s:
        s.add(saved)
        s.commit()
        assert chunk_corpus_fingerprint(s) == before_fp  # identical content restored ...
    assert len(m.get()) == size and m.rebuilds == 3  # ... but a new row version: rebuilt


def test_document_insert_update_and_delete_rebuild(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions)
    m.get()
    key = f"test_doc_{uuid.uuid4().hex[:8]}"
    sha = hashlib.sha256(key.encode()).hexdigest()
    with sessions() as s:
        src = s.scalars(sa.select(Document).order_by(Document.document_key).limit(1)).one()
        doc = _copy(
            src,
            Document,
            id=uuid.uuid4(),
            document_key=key,
            sha256=sha,
            supersedes_document_id=None,
        )
        s.add(doc)
        s.commit()
        doc_id = doc.id
    try:
        m.get()
        assert m.rebuilds == 2  # INSERT of a document row (no chunks)
        with sessions() as s:
            s.execute(sa.update(Document).where(Document.id == doc_id).values(title="Changed"))
            s.commit()
        m.get()
        assert m.rebuilds == 3  # UPDATE of a document row
    finally:
        with sessions() as s:
            s.execute(sa.delete(Document).where(Document.id == doc_id))
            s.commit()
    m.get()
    assert m.rebuilds == 4  # DELETE of a document row


@pytest.mark.parametrize("table", ["chunks", "documents"])
def test_noop_update_rebuilds(sessions: sessionmaker[Session], table: str) -> None:
    m = KeywordIndexManager(sessions)
    m.get()
    with sessions() as s:
        before_fp = chunk_corpus_fingerprint(s)
        if table == "chunks":
            cid = _first_eligible(s).id
            s.execute(sa.update(Chunk).where(Chunk.id == cid).values(content=Chunk.content))
        else:
            did = s.scalar(sa.select(Document.id).limit(1))
            s.execute(sa.update(Document).where(Document.id == did).values(title=Document.title))
        s.commit()
        assert chunk_corpus_fingerprint(s) == before_fp  # no content changed at all
    m.get()
    assert m.rebuilds == 2  # a new row version is still detected (safe direction)
