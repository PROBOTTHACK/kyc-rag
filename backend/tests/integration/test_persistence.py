"""Stage E on real Postgres with the real corpus: persistence, idempotency, failures,
recovery and Postgres <-> Qdrant reconciliation.

Each test gets a throwaway database (`temp_db`). Vectors come from the deterministic
`FakeEmbedder` (8-d), so these tests check data flow, not retrieval quality. Qdrant is
in-memory, except for the full-corpus test, which uses a throwaway collection on the
docker Qdrant. Needs the gitignored corpus PDFs and the OCR cache or Tesseract.
"""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from sqlalchemy.orm import Session, sessionmaker

from app.config import REPO_ROOT, get_settings
from app.db.models import Chunk, Document, DocumentExclusion, DocumentPage, IngestionRun
from app.db.persistence import PersistError, persist_document
from app.db.repository import (
    evidence_context,
    family_integrity,
    get_chunks,
    load_chunking_result,
)
from app.ingestion import cli
from app.ingestion.chunking import ChunkRecord
from app.ingestion.manifest import load_manifest
from app.ingestion.pipeline import IngestOutcome, ingest_document
from app.ingestion.prepare import PreparedDocument, ocr_setup, prepare
from app.retrieval.indexing import point_payload
from app.retrieval.qdrant_store import INDEX_KEY, ChunkStore, store_from_settings
from app.retrieval.reconcile import reconcile, repair
from app.schemas.corpus import chunk_id
from app.schemas.taxonomy import (
    ChunkRole,
    IngestionStage,
    IngestionStatus,
    PageStatus,
)
from tests.unit.test_indexing import DIM, FakeEmbedder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant"),
]
CORPUS = REPO_ROOT / "data" / "raw_pdfs"
EXPECTED = {"documents": 9, "pages": 135, "chunks": 456, "parents": 27, "eligible": 429}
SMALL = "rbi_kyc_faqs"  # a text document without parents
FAMILIES = "rbi_kyc_directions_2025"  # holds the parent/child families
SCANNED = "hdfc_nri_rekyc"  # image-only pages: OCR


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> dict[str, PreparedDocument]:
    if not (CORPUS / "regulatory" / "169MD.pdf").exists():
        pytest.skip("source PDFs not present")
    settings = get_settings()
    setup, err = ocr_setup(settings)
    return {
        e.document_key: prepare(e, settings, setup=setup, setup_error=err)
        for e in load_manifest(settings.CORPUS_MANIFEST)
    }


@pytest.fixture
def sessions(migrated: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(migrated, expire_on_commit=False)


@pytest.fixture
def store() -> ChunkStore:
    s = ChunkStore(QdrantClient(":memory:"), "stage_e")
    s.ensure_collection(DIM)
    return s


def ingest(
    prepared: PreparedDocument,
    sessions: sessionmaker[Session],
    store: ChunkStore,
    embedder: Any = None,
    **kw: Any,
) -> IngestOutcome:
    return ingest_document(
        prepared.entry,
        get_settings(),
        sessions=sessions,
        embedder=embedder or FakeEmbedder(),
        store=store,
        setup=None,
        prepared=prepared,
        **kw,
    )


def count(sessions: sessionmaker[Session], model: Any, *where: Any) -> int:
    with sessions() as s:
        return s.scalar(sa.select(sa.func.count()).select_from(model).where(*where)) or 0


def with_chunks(p: PreparedDocument, chunks: list[ChunkRecord]) -> PreparedDocument:
    return replace(p, result=replace(p.result, chunks=chunks))


def edit(rec: ChunkRecord, **update: Any) -> ChunkRecord:
    return ChunkRecord(rec.metadata.model_copy(update=update), dict(rec.extras))


def runs(sessions: sessionmaker[Session], key: str) -> list[IngestionRun]:
    with sessions() as s:
        return list(
            s.scalars(
                sa.select(IngestionRun)
                .where(IngestionRun.document_key == key)
                .order_by(IngestionRun.started_at)
            )
        )


# --- the whole corpus -----------------------------------------------------------------------


@pytest.fixture
def server_store() -> Iterator[ChunkStore]:
    base = store_from_settings(get_settings())
    s = ChunkStore(base.client, f"test_{uuid.uuid4().hex[:12]}")
    s.ensure_collection(DIM)
    try:
        yield s
    finally:
        if s.client.collection_exists(s.collection):
            s.client.delete_collection(s.collection)


def test_full_corpus_persists_every_chunk_and_matches_qdrant(
    corpus: dict[str, PreparedDocument],
    sessions: sessionmaker[Session],
    server_store: ChunkStore,
) -> None:
    outcomes = [ingest(p, sessions, server_store) for p in corpus.values()]
    assert [o.failed_stage for o in outcomes if o.failed] == []
    assert {o.status for o in outcomes} <= {IngestionStatus.COMPLETED, IngestionStatus.PARTIAL}

    assert count(sessions, Document) == EXPECTED["documents"]
    assert count(sessions, DocumentPage) == EXPECTED["pages"]
    assert count(sessions, Chunk) == EXPECTED["chunks"]
    assert count(sessions, Chunk, Chunk.chunk_role == "parent") == EXPECTED["parents"]
    assert count(sessions, Chunk, Chunk.embedding_eligible) == EXPECTED["eligible"]
    assert count(sessions, DocumentExclusion) == sum(
        len(p.result.exclusions) for p in corpus.values()
    )

    with sessions() as s:
        families = family_integrity(s)
        report = reconcile(s, server_store, FakeEmbedder())
        docs = {d.document_key: d for d in s.scalars(sa.select(Document))}
        for key, p in corpus.items():
            # Every chunk, parents included, comes back exactly as Stage C produced it.
            stored = load_chunking_result(s, docs[key])
            assert [c.metadata for c in stored.chunks] == [c.metadata for c in p.result.chunks]
            assert [c.extras for c in stored.chunks] == [c.extras for c in p.result.chunks]
            assert docs[key].page_count == len(p.pages)
            assert docs[key].ingestion_status == ("partial" if p.failed_pages else "completed")
    assert families.ok, families.problems
    assert (families.parents, families.children) == (
        EXPECTED["parents"],
        sum(c.metadata.chunk_role is ChunkRole.CHILD for p in corpus.values()
            for c in p.result.chunks),
    )  # fmt: skip
    assert report.consistent, report
    assert report.qdrant_points == server_store.count() == EXPECTED["eligible"]
    assert report.postgres_eligible == EXPECTED["eligible"]


def test_provenance_and_ocr_details_are_stored(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    for key in (SCANNED, "hdfc_non_individual_account_opening"):
        assert not ingest(corpus[key], sessions, store).failed
    with sessions() as s:
        pages = list(
            s.scalars(sa.select(DocumentPage).where(DocumentPage.ocr_confidence.is_not(None)))
        )
        chunks = list(s.scalars(sa.select(Chunk).where(Chunk.extraction_method == "ocr")))
    assert pages and chunks
    # Page records keep OCR regions and printed page numbers, without per-run cache flags.
    regions = [r for p in pages for r in p.metadata_["ocr"]["regions"]]
    assert regions and all("from_cache" not in r for r in regions)
    assert any(r["printed_page"] is not None for r in regions)
    assert all(0 <= (p.ocr_confidence or 0) <= 1 for p in pages)
    # Chunks keep reliability, confidence, region, page status and excluded OCR lines.
    assert {c.text_reliability for c in chunks} <= {"ocr_reliable", "ocr_uncertain"}
    assert all(c.metadata_["ocr_confidence"] is not None for c in chunks)
    assert any(c.metadata_["region"] in ("left", "right") for c in chunks)
    assert any(c.metadata_["printed_page"] is not None for c in chunks)
    excluded = [ln for c in chunks for ln in c.metadata_.get("excluded_lines", [])]
    assert excluded and all({"text", "page_number", "confidence"} <= ln.keys() for ln in excluded)
    assert all(c.raw_text for c in chunks)
    uncertain = [c for c in chunks if c.text_reliability == "ocr_uncertain"]
    assert all(not c.can_support_mandatory for c in uncertain)


# --- idempotency, updates, removals -----------------------------------------------------------


def test_reingesting_unchanged_documents_writes_nothing(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[FAMILIES]
    first = ingest(p, sessions, store)
    assert first.persist and first.persist.document == "inserted"
    with sessions() as s:
        before = {c.id: c.updated_at for c in s.scalars(sa.select(Chunk))}
    embedder = FakeEmbedder()
    second = ingest(p, sessions, store, embedder)
    assert second.status is first.status and second.persist and second.index
    r = second.persist
    assert (r.document, r.pages_written, r.exclusions_replaced) == ("unchanged", 0, False)
    assert (r.chunks_inserted, r.chunks_updated, r.chunks_deleted) == (0, 0, 0)
    assert r.chunks_unchanged == len(p.result.chunks) and not r.changed
    assert second.index.embedded == second.index.payload_updated == 0
    assert embedder.passages == []  # no re-embedding
    with sessions() as s:
        assert {c.id: c.updated_at for c in s.scalars(sa.select(Chunk))} == before
    assert count(sessions, Document) == 1
    assert [r.status for r in runs(sessions, FAMILIES)] == [first.status.value] * 2


def test_content_and_metadata_changes_update_the_right_rows(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[SMALL]
    ingest(p, sessions, store)
    chunks = list(p.result.chunks)
    target_text, target_meta = chunks[3], chunks[5]
    chunks[3] = edit(target_text, content=target_text.metadata.content + " (amended)")
    chunks[5] = edit(target_meta, section="Q99: renamed section")
    embedder = FakeEmbedder()
    out = ingest(with_chunks(p, chunks), sessions, store, embedder)
    assert out.persist and out.index
    assert (out.persist.chunks_updated, out.persist.chunks_inserted) == (2, 0)
    assert (out.index.embedded, out.index.payload_updated) == (1, 1)  # vector kept for meta
    with sessions() as s:
        got = get_chunks(s, [target_text.metadata.chunk_id, target_meta.metadata.chunk_id])
        assert got[target_text.metadata.chunk_id].metadata.content.endswith("(amended)")
        assert got[target_meta.metadata.chunk_id].metadata.section == "Q99: renamed section"
        assert reconcile(s, store, embedder).consistent


def test_removed_chunks_and_families_are_deleted_with_their_vectors(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[FAMILIES]
    ingest(p, sessions, store)
    parent = next(c for c in p.result.chunks if c.metadata.chunk_role is ChunkRole.PARENT)
    family = {parent.metadata.chunk_id} | {
        c.metadata.chunk_id for c in p.result.children_of(parent)
    }
    single = next(c for c in p.result.chunks if c.metadata.chunk_role is ChunkRole.SINGLE)
    gone = family | {single.metadata.chunk_id}
    kept = [c for c in p.result.chunks if c.metadata.chunk_id not in gone]
    out = ingest(with_chunks(p, kept), sessions, store)
    assert out.persist and out.index and not out.failed
    assert out.persist.chunks_deleted == len(gone)
    assert out.index.stale_deleted == len(gone) - 1  # the parent never had a point
    with sessions() as s:
        assert get_chunks(s, gone) == {}
        assert family_integrity(s).ok
        assert reconcile(s, store, FakeEmbedder()).consistent


def test_removing_a_parent_but_keeping_its_children_is_refused_and_changes_nothing(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[FAMILIES]
    ingest(p, sessions, store)
    parent = next(c for c in p.result.chunks if c.metadata.chunk_role is ChunkRole.PARENT)
    broken = with_chunks(p, [c for c in p.result.chunks if c is not parent])
    out = ingest(broken, sessions, store)
    assert out.failed and out.failed_stage is IngestionStage.PERSISTENCE
    assert "has no parent chunk" in (out.error or "")
    assert count(sessions, Chunk) == len(p.result.chunks)  # rolled back
    with sessions() as s:
        assert s.get(Document, p.entry.row_id).ingestion_status == "failed"  # type: ignore[union-attr]
        assert family_integrity(s).ok


def test_a_new_version_under_an_existing_key_is_refused(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[SMALL]
    ingest(p, sessions, store)
    other_sha = "0" * 64
    entry = p.entry.model_copy(update={"sha256": other_sha})
    assert not any(c.metadata.parent_chunk_id for c in p.result.chunks)
    rekeyed = [
        edit(c, document_id=str(entry.row_id),
             chunk_id=str(chunk_id(other_sha, c.metadata.chunk_index)))
        for c in p.result.chunks
    ]  # fmt: skip
    new_version = replace(with_chunks(p, rekeyed), entry=entry)
    with sessions() as s, pytest.raises(PersistError, match="another version"), s.begin():
        persist_document(s, new_version)
    with sessions() as s:
        assert s.scalars(sa.select(Document.sha256)).all() == [p.entry.sha256]


# --- evidence expansion -----------------------------------------------------------------------


def test_children_expand_to_their_complete_parent(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[FAMILIES]
    ingest(p, sessions, store)
    parent = next(c for c in p.result.chunks if c.metadata.chunk_role is ChunkRole.PARENT)
    child = p.result.children_of(parent)[1]
    single = next(c for c in p.result.chunks if c.metadata.chunk_role is ChunkRole.SINGLE)
    with sessions() as s:
        ctx = evidence_context(s, [child.metadata.chunk_id, single.metadata.chunk_id])
    assert ctx[child.metadata.chunk_id].expanded
    assert ctx[child.metadata.chunk_id].context.metadata == parent.metadata
    assert ctx[child.metadata.chunk_id].context.extras["raw_text"] == parent.extras["raw_text"]
    assert not ctx[single.metadata.chunk_id].expanded


# --- failures and recovery ---------------------------------------------------------------------


class FlakyStore(ChunkStore):
    """In-memory store that fails the Nth upsert batch or every delete, on demand."""

    def __init__(self, client: QdrantClient, collection: str) -> None:
        super().__init__(client, collection)
        self.fail_upsert_after: int | None = None
        self.fail_delete = False
        self.upsert_calls = 0

    def upsert(self, points: Sequence[qm.PointStruct]) -> None:
        batch = 16
        for i in range(0, len(points), batch):
            self.upsert_calls += 1
            if self.fail_upsert_after is not None and self.upsert_calls > self.fail_upsert_after:
                raise ConnectionError("qdrant unavailable (injected)")
            super().upsert(points[i : i + batch])

    def delete(self, ids: Any) -> int:
        if self.fail_delete:
            raise TimeoutError("qdrant delete timed out (injected)")
        return super().delete(ids)


@pytest.fixture
def flaky() -> FlakyStore:
    s = FlakyStore(QdrantClient(":memory:"), "flaky")
    s.ensure_collection(DIM)
    return s


def test_partial_qdrant_upsert_failure_keeps_postgres_and_is_recoverable(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], flaky: FlakyStore
) -> None:
    p = corpus[FAMILIES]
    eligible = sum(c.embeddable for c in p.result.chunks)
    flaky.fail_upsert_after = 2  # two batches land, then Qdrant "goes away"
    out = ingest(p, sessions, flaky)
    assert out.failed and out.failed_stage is IngestionStage.INDEXING
    assert "injected" in (out.error or "")
    assert 0 < flaky.count() < eligible  # a partial index exists
    assert count(sessions, Chunk) == len(p.result.chunks)  # Postgres is intact
    [run] = runs(sessions, p.entry.document_key)
    assert (run.status, run.failed_stage) == ("failed", "indexing")
    assert run.stages["persistence"]["status"] == "ok"
    with sessions() as s:
        assert s.get(Document, p.entry.row_id).ingestion_status == "failed"  # type: ignore[union-attr]
        report = reconcile(s, flaky, FakeEmbedder())
    assert len(report.missing) == eligible - flaky.count()
    assert report.unfinished_documents == {p.entry.document_key: "failed"}
    assert not report.no_postgres_chunk and not report.not_eligible

    # Recovery 1: re-run ingestion (idempotent; only the missing points are embedded).
    flaky.fail_upsert_after = None
    retry = ingest(p, sessions, flaky)
    assert not retry.failed and retry.index
    assert retry.index.embedded == len(report.missing)
    with sessions() as s:
        assert reconcile(s, flaky, FakeEmbedder()).ok


def test_reconcile_repair_recovers_without_reprocessing_pdfs(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], flaky: FlakyStore
) -> None:
    p = corpus[FAMILIES]
    flaky.fail_upsert_after = 1
    assert ingest(p, sessions, flaky).failed
    flaky.fail_upsert_after = None
    with sessions() as s:
        before = reconcile(s, flaky, FakeEmbedder())
    assert before.missing
    fixed = repair(sessions, flaky, FakeEmbedder(), before)
    assert not fixed.failed and fixed.reindexed[p.entry.document_key].embedded == len(
        before.missing
    )
    with sessions() as s:
        assert reconcile(s, flaky, FakeEmbedder()).consistent


def test_embedding_failure_leaves_existing_vectors_untouched(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[SMALL]
    ingest(p, sessions, store)
    points_before = {str(pt.id) for pt in store.client.scroll(store.collection, limit=500)[0]}
    chunks = list(p.result.chunks)
    chunks[0] = edit(chunks[0], content=chunks[0].metadata.content + " v2")
    out = ingest(with_chunks(p, chunks), sessions, store, FakeEmbedder(fail=True))
    assert out.failed and out.failed_stage is IngestionStage.EMBEDDING
    assert {str(pt.id) for pt in store.client.scroll(store.collection, limit=500)[0]} == (
        points_before
    )
    with sessions() as s:
        report = reconcile(s, store, FakeEmbedder())
    # Postgres has the new text; Qdrant still has the old vector -> reported, not lost.
    assert [i.point_id for i in report.outdated] == [chunks[0].metadata.chunk_id]
    assert ingest(with_chunks(p, chunks), sessions, store).status is IngestionStatus.COMPLETED


def test_failed_stale_delete_is_reported_and_repaired(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], flaky: FlakyStore
) -> None:
    p = corpus[SMALL]
    ingest(p, sessions, flaky)
    removed = p.result.chunks[-1]
    flaky.fail_delete = True
    out = ingest(with_chunks(p, p.result.chunks[:-1]), sessions, flaky)
    assert out.failed and out.failed_stage is IngestionStage.INDEXING
    with sessions() as s:
        report = reconcile(s, flaky, FakeEmbedder())
    assert [i.point_id for i in report.no_postgres_chunk] == [removed.metadata.chunk_id]
    flaky.fail_delete = False
    repair(sessions, flaky, FakeEmbedder(), report)
    with sessions() as s:
        assert reconcile(s, flaky, FakeEmbedder()).consistent


def test_first_ingestion_rollback_is_still_recorded(
    corpus: dict[str, PreparedDocument],
    sessions: sessionmaker[Session],
    store: ChunkStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.ingestion.pipeline as pipeline

    def explode(session: Session, prepared: PreparedDocument) -> Any:
        persist_document(session, prepared)  # rows written in the transaction...
        raise RuntimeError("disk full (injected)")  # ...then rolled back

    monkeypatch.setattr(pipeline, "persist_document", explode)
    p = corpus[SMALL]
    out = ingest(p, sessions, store)
    assert out.failed and out.failed_stage is IngestionStage.PERSISTENCE
    assert count(sessions, Document) == 0 and count(sessions, Chunk) == 0  # rolled back
    [run] = runs(sessions, p.entry.document_key)
    assert (run.status, run.failed_stage) == ("failed", "persistence")
    assert "disk full" in (run.error or "") and run.document_id == p.entry.row_id
    assert run.stages["indexing"]["status"] == "skipped" and run.finished_at is not None
    assert store.count() == 0  # nothing indexed for an unpersisted document


def test_extraction_failure_is_recorded_with_its_stage(
    corpus: dict[str, PreparedDocument],
    sessions: sessionmaker[Session],
    store: ChunkStore,
    tmp_path: Path,
) -> None:
    entry = corpus[SMALL].entry
    (tmp_path / "regulatory").mkdir()
    (tmp_path / entry.file).write_bytes(b"%PDF-1.7 not really a pdf")
    settings = get_settings().model_copy(update={"CORPUS_DIR": tmp_path})
    out = ingest_document(
        entry, settings, sessions=sessions, embedder=FakeEmbedder(), store=store, setup=None
    )
    assert out.failed and out.failed_stage is IngestionStage.EXTRACTION
    [run] = runs(sessions, entry.document_key)
    assert run.stages["extraction"]["status"] == "failed"
    assert {run.stages[s]["status"] for s in ("ocr", "chunking", "persistence")} == {"skipped"}
    assert count(sessions, Document) == 0


def test_ocr_failure_makes_the_document_partial_not_completed(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    entry = corpus[SCANNED].entry
    out = ingest_document(
        entry, get_settings(), sessions=sessions, embedder=FakeEmbedder(), store=store,
        setup=None, setup_error="Tesseract not installed (simulated)",
    )  # fmt: skip
    assert out.status is IngestionStatus.PARTIAL and out.failed_stage is None
    [run] = runs(sessions, entry.document_key)
    assert run.status == "partial" and run.stages["ocr"]["status"] == "failed"
    assert run.stages["ocr"]["failed_pages"]
    with sessions() as s:
        doc = s.get(Document, entry.row_id)
        assert doc is not None and doc.ingestion_status == "partial"
        failed = s.scalars(
            sa.select(DocumentPage).where(DocumentPage.status == PageStatus.FAILED.value)
        ).all()
    assert failed and all("Tesseract" in (pg.error or "") for pg in failed)


# --- reconciliation -------------------------------------------------------------------------------


def test_reconcile_reports_every_direction_and_never_deletes(
    corpus: dict[str, PreparedDocument], sessions: sessionmaker[Session], store: ChunkStore
) -> None:
    p = corpus[FAMILIES]
    ingest(p, sessions, store)
    embedder = FakeEmbedder()
    eligible = [c for c in p.result.chunks if c.embeddable]
    parent = next(c for c in p.result.chunks if c.metadata.chunk_role is ChunkRole.PARENT)
    missing, outdated = eligible[0], eligible[1]
    store.delete([missing.metadata.chunk_id])
    stray_known = str(uuid.uuid4())
    stray_unknown = str(uuid.uuid4())
    vec = [1.0] + [0.0] * (DIM - 1)
    store.upsert(
        [
            qm.PointStruct(id=stray_known, vector=vec,
                           payload={"document_key": p.entry.document_key}),
            qm.PointStruct(id=stray_unknown, vector=vec,
                           payload={"document_key": "retired_document"}),
            qm.PointStruct(id=parent.metadata.chunk_id, vector=vec,
                           payload=point_payload(parent, embedder)),
        ]
    )  # fmt: skip
    stale = point_payload(outdated, embedder)
    stale[INDEX_KEY]["payload_sha256"] = "0" * 64
    store.overwrite_payload(outdated.metadata.chunk_id, stale)
    points_before = store.count()

    with sessions() as s:
        r = reconcile(s, store, embedder)
    assert [i.point_id for i in r.missing] == [missing.metadata.chunk_id]
    assert {i.point_id for i in r.no_postgres_chunk} == {stray_known, stray_unknown}
    assert [i.point_id for i in r.not_eligible] == [parent.metadata.chunk_id]
    assert [(i.point_id, i.reason) for i in r.outdated] == [(outdated.metadata.chunk_id, "payload")]
    assert not r.consistent and store.count() == points_before  # diagnostic only

    # CLI diagnostic: reports, exits 1, still deletes nothing.
    assert cli._reconcile(get_settings(), do_repair=False, delete_unknown=False,
                          embedder=embedder, store=store, sessions=sessions) == 1  # fmt: skip
    assert store.count() == points_before

    fixed = repair(sessions, store, embedder, r)  # unknown-document point is kept
    assert fixed.unknown_kept == 1 and fixed.unknown_deleted == 0
    with sessions() as s:
        after = reconcile(s, store, embedder)
    assert [i.point_id for i in after.no_postgres_chunk] == [stray_unknown]
    assert not (after.missing or after.not_eligible or after.outdated)
    fixed = repair(sessions, store, embedder, after, delete_unknown=True)
    assert fixed.unknown_deleted == 1
    with sessions() as s:
        assert reconcile(s, store, embedder).consistent


# --- CLI -----------------------------------------------------------------------------


def test_cli_ingest_and_runs(
    corpus: dict[str, PreparedDocument],
    sessions: sessionmaker[Session],
    store: ChunkStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli._ingest(get_settings(), [SMALL], force=False, embedder=FakeEmbedder(),
                       store=store, sessions=sessions)  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "CONSISTENT" in out and "families" in out
    assert cli._runs(get_settings(), SMALL, False, 5, sessions=sessions) == 0
    assert "completed" in capsys.readouterr().out
    assert cli._ingest(get_settings(), ["no_such_doc"], force=False) == 2
