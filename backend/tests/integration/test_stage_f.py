"""Stage F: the unified CLI workflow on real Postgres (throwaway databases) and the real
corpus, with in-memory Qdrant and the deterministic `FakeEmbedder`.

Covers `run` (= `ingest`, `make ingest`), dry runs, persist-only + `index --from-db`,
`retry` after failures, the schema preflight, `search --expand`, `evaluate`, and that
every label of the M1 benchmark points at a real eligible chunk.
"""

import pytest
import sqlalchemy as sa
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import REPO_ROOT, get_settings
from app.db.models import Chunk, Document, IngestionRun
from app.ingestion import cli
from app.ingestion.manifest import load_manifest
from app.ingestion.pipeline import RetryAction, retry_action
from app.ingestion.prepare import ocr_setup, prepare_document
from app.retrieval.evaluation import load_cases
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.search import SearchFilters
from tests.integration.conftest import sync_engine
from tests.unit.test_indexing import DIM, FakeEmbedder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore:Payload indexes have no effect"),
    pytest.mark.skipif(
        not (REPO_ROOT / "data" / "raw_pdfs" / "regulatory" / "169MD.pdf").exists(),
        reason="source PDFs not present",
    ),
]
DOCS = ["rbi_kyc_faqs", "hdfc_kyc_aml_faq"]  # small native documents
FAMILIES = "rbi_kyc_directions_2025"


@pytest.fixture
def sessions(migrated: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(migrated, expire_on_commit=False)


@pytest.fixture
def store() -> ChunkStore:
    s = ChunkStore(QdrantClient(":memory:"), "stage_f")
    s.ensure_collection(DIM)
    return s


def count(sessions: sessionmaker[Session], model: object, *where: object) -> int:
    with sessions() as s:
        return s.scalar(sa.select(sa.func.count()).select_from(model).where(*where)) or 0  # type: ignore[arg-type]


def statuses(sessions: sessionmaker[Session]) -> dict[str, str]:
    with sessions() as s:
        return {d.document_key: d.ingestion_status for d in s.scalars(sa.select(Document))}


def run(sessions: sessionmaker[Session], store: ChunkStore, embedder: FakeEmbedder | None = None,
        docs: list[str] = DOCS, **kw: object) -> int:  # fmt: skip
    return cli._run(get_settings(), docs, embedder=embedder or FakeEmbedder(), store=store,
                    sessions=sessions, **kw)  # type: ignore[arg-type]  # fmt: skip


# --- the unified workflow -------------------------------------------------------------------


def test_run_is_end_to_end_and_idempotent(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(sessions, store) == 0
    out = capsys.readouterr().out
    for step in ("[1/4]", "[2/4]", "[3/4]", "[4/4]", "CONSISTENT", "summary: 2 document(s)"):
        assert step in out, step
    assert set(statuses(sessions).values()) == {"completed"}
    eligible = count(sessions, Chunk, Chunk.embedding_eligible)
    assert store.count() == eligible > 0

    embedder = FakeEmbedder()
    assert run(sessions, store, embedder) == 0
    assert embedder.passages == []  # second run: nothing re-embedded
    assert "0/0/0" in capsys.readouterr().out  # nothing inserted/updated/deleted
    assert count(sessions, IngestionRun) == 4  # every run is recorded


def test_dry_run_writes_nothing(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    embedder = FakeEmbedder()
    assert run(sessions, store, embedder, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "nothing written" in out and "chunks +37" in out  # rbi_kyc_faqs: 37 new chunks
    assert count(sessions, Document) == count(sessions, IngestionRun) == 0
    assert store.count() == 0 and embedder.passages == []


def test_persist_only_then_index_from_postgres(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.ingestion.pipeline import Mode

    assert cli._run(get_settings(), DOCS, mode=Mode.PERSIST, sessions=sessions) == 0
    assert set(statuses(sessions).values()) == {"pending"}  # stored, not fully ingested
    assert store.count() == 0
    with sessions() as s:
        assert [retry_action(s, e)[0] for e in load_manifest(get_settings().CORPUS_MANIFEST)
                if e.document_key in DOCS] == [RetryAction.INDEX] * 2  # fmt: skip
    code = cli._index_from_db(get_settings(), DOCS, force=False, dry_run=False,
                              embedder=FakeEmbedder(), store=store, sessions=sessions)  # fmt: skip
    assert code == 0
    assert set(statuses(sessions).values()) == {"completed"}
    assert store.count() == count(sessions, Chunk, Chunk.embedding_eligible)
    with sessions() as s:
        commands = sorted(r.command for r in s.scalars(sa.select(IngestionRun)))
    assert commands == ["index", "index", "persist", "persist"]
    assert "CONSISTENT" in capsys.readouterr().out


def test_failed_embedding_is_reported_and_retry_recovers_from_postgres(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(sessions, store, FakeEmbedder(fail=True)) == 1  # model unavailable
    out = capsys.readouterr().out
    assert out.count("FAILED at embedding") == 2 and "see `runs --failed`" in out
    assert set(statuses(sessions).values()) == {"failed"}  # never `completed`
    assert store.count() == 0
    chunks_before = count(sessions, Chunk)

    with sessions() as s:
        actions = {e.document_key: retry_action(s, e)[0]
                   for e in load_manifest(get_settings().CORPUS_MANIFEST)}  # fmt: skip
    assert actions["rbi_kyc_faqs"] is RetryAction.INDEX  # rows are reused
    assert actions[FAMILIES] is RetryAction.FULL  # never ingested

    embedder = FakeEmbedder()
    assert cli._retry(get_settings(), DOCS, force=False, embedder=embedder, store=store,
                      sessions=sessions) == 0  # fmt: skip
    assert set(statuses(sessions).values()) == {"completed"}
    assert count(sessions, Chunk) == chunks_before  # recovery re-read no PDF, wrote no rows
    assert store.count() == count(sessions, Chunk, Chunk.embedding_eligible)
    assert cli._retry(get_settings(), DOCS, force=False, embedder=embedder, store=store,
                      sessions=sessions) == 0  # fmt: skip
    assert "nothing to retry" in capsys.readouterr().out


def test_unmigrated_database_is_refused_before_any_work(
    temp_db: str, store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = sessionmaker(sync_engine(temp_db))
    assert run(sessions, store) == 2
    assert "make migrate" in capsys.readouterr().out
    assert store.count() == 0


def test_unknown_document_is_rejected(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(sessions, store, docs=["no_such_doc"]) == 2
    assert "unknown document key(s): no_such_doc" in capsys.readouterr().out


# --- retrieval ----------------------------------------------------------------------------------


def test_search_cli_cites_pages_and_expands_children(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(sessions, store, docs=[FAMILIES]) == 0
    capsys.readouterr()
    # FakeEmbedder vectors are hash-based, so query the exact embedding input of a child.
    with sessions() as s:
        child = s.scalars(sa.select(Chunk).where(Chunk.chunk_role == "child").limit(1)).one()
        parent = s.get(Chunk, child.parent_chunk_id)
    assert parent is not None
    from app.db.corpus import chunk_metadata_from_rows
    from app.retrieval.embeddings import embedding_input

    with sessions() as s:
        doc = s.get(Document, child.document_id)
        query = embedding_input(chunk_metadata_from_rows(child, doc))  # type: ignore[arg-type]
    assert cli._search(get_settings(), query, top_k=5, filters=SearchFilters(), expand=True,
                       embedder=FakeEmbedder(), store=store, sessions=sessions) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert f"1. [1.000] {FAMILIES}, p" in out
    assert "parent paragraph" in out and parent.content[:60] in " ".join(out.split())


def test_every_benchmark_label_points_at_an_eligible_chunk() -> None:
    settings = get_settings()
    setup, err = ocr_setup(settings)
    payloads = [
        c.metadata.to_qdrant_payload()
        for e in load_manifest(settings.CORPUS_MANIFEST)
        for c in prepare_document(e, settings, setup=setup, setup_error=err).chunks
        if c.embeddable
    ]
    cases = load_cases(REPO_ROOT / "data" / "eval" / "m1_retrieval.jsonl")
    unmatched = [
        (c.id, t.label())
        for c in cases
        for t in c.targets
        if not any(t.matches(p) for p in payloads)
    ]
    assert unmatched == []


def test_evaluate_cli_runs_the_benchmark(
    sessions: sessionmaker[Session], store: ChunkStore, capsys: pytest.CaptureFixture[str],
    tmp_path: object,
) -> None:  # fmt: skip
    assert run(sessions, store, docs=["rbi_kyc_faqs"]) == 0
    capsys.readouterr()
    from pathlib import Path

    report = Path(str(tmp_path)) / "report.md"
    assert cli._evaluate(get_settings(), Path("data/eval/m1_retrieval.jsonl"), top_k=5,
                         report_path=report, embedder=FakeEmbedder(), store=store) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "mean Recall@5" in out and report.read_text(encoding="utf-8").startswith("# M1")


class UnreachableAfterIndexing(ChunkStore):
    """Indexing works; the whole-store scan used by verification finds Qdrant gone."""

    def all_point_states(self):  # type: ignore[no-untyped-def]
        from qdrant_client.http.exceptions import ResponseHandlingException

        raise ResponseHandlingException(ConnectionRefusedError("connection refused"))


def test_verification_reports_an_unreachable_store_instead_of_crashing(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    store = UnreachableAfterIndexing(QdrantClient(":memory:"), "stage_f_down")
    store.ensure_collection(DIM)
    assert run(sessions, store, docs=["hdfc_kyc_aml_faq"]) == 1  # not a traceback
    out = capsys.readouterr().out
    assert "verification incomplete: ResponseHandlingException" in out
    assert "make reconcile" in out and "summary: 1 document(s)" in out
