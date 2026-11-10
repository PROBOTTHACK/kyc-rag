"""M2 A3 on real services: the stopword keyword index over the full corpus, and the
`evaluate --select` harness end to end.

Uses the full-corpus throwaway database and collection (`FakeEmbedder` vectors). The
selection harness is exercised on a SYNTHETIC two-query dataset (written to a temp file,
targeting passages used by neither the development nor the held-out set), so validating
the harness reveals no development-set or held-out result before the official A3 run.
"""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Chunk
from app.ingestion import cli
from app.retrieval.keyword import KeywordIndexManager
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.selection import CANDIDATES
from tests.unit.test_indexing import FakeEmbedder

pytestmark = pytest.mark.integration


@pytest.fixture
def store(corpus_store: ChunkStore) -> ChunkStore:
    return corpus_store


@pytest.fixture
def sessions(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    return corpus_db


def test_stopword_index_over_the_full_corpus(sessions: sessionmaker[Session]) -> None:
    plain = KeywordIndexManager(sessions).get()
    stop = KeywordIndexManager(sessions, stopwords=True).get()
    assert len(plain) == len(stop) == 429
    assert plain.fingerprint.startswith("t1:x:") and stop.fingerprint.startswith("t1s:x:")
    assert plain.fingerprint[5:] == stop.fingerprint[6:]  # same rows, different variant
    q = "the training of employees"
    assert [h.chunk_id for h in stop.search(q)] == [
        h.chunk_id for h in stop.search("training employees")
    ]


def test_stopword_index_freshness(sessions: sessionmaker[Session]) -> None:
    m = KeywordIndexManager(sessions, stopwords=True)
    m.get()
    with sessions() as s:
        cid = s.scalar(sa.select(Chunk.id).where(Chunk.embedding_eligible.is_(True)).limit(1))
        s.execute(sa.update(Chunk).where(Chunk.id == cid).values(section=Chunk.section))
        s.commit()
    m.get()
    assert m.rebuilds == 2 and m.get() is m.get()


SYNTHETIC = [
    {"id": "syn-01", "query": "screening and training when hiring employees",
     "targets": [{"document_key": "rbi_kyc_directions_2025", "page": 71, "paragraph": "79"}]},
    {"id": "syn-02", "query": "technological innovations for name screening against sanctions",
     "targets": [{"document_key": "rbi_kyc_directions_2025", "page": 59, "paragraph": "62"}]},
]  # fmt: skip


def test_select_harness_end_to_end_on_a_synthetic_dataset(
    sessions: sessionmaker[Session],
    store: ChunkStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = tmp_path / "synthetic.jsonl"
    data.write_text("\n".join(json.dumps(c) for c in SYNTHETIC) + "\n", encoding="utf-8")
    record = tmp_path / "record.json"
    report = tmp_path / "report.md"
    assert cli._select(get_settings(), report_path=report, record_path=record, dataset=data,
                       embedder=FakeEmbedder(), store=store, sessions=sessions) == 0  # fmt: skip
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert [c["id"] for c in rec["candidates"]] == [c.id for c in CANDIDATES]
    assert rec["selected"]["id"] in {c.id for c in CANDIDATES}
    assert len(rec["code_commit"]) == 40 and "not read" in rec["heldout"]
    text = report.read_text(encoding="utf-8")
    assert "## Decision" in text and "trade-off evidence" in text
    for c in CANDIDATES:
        assert f"| {c.id} | {c.label} |" in text
    capsys.readouterr()
