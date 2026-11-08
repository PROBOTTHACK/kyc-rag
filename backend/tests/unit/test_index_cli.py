"""`python -m app.ingestion.cli index` with an injected fake embedder and in-memory Qdrant."""

import pytest
from qdrant_client import QdrantClient

from app.config import REPO_ROOT, get_settings
from app.ingestion.cli import _index, main
from app.retrieval.qdrant_store import ChunkStore
from tests.unit.test_indexing import DIM, FakeEmbedder

pytestmark = [
    pytest.mark.skipif(
        not (REPO_ROOT / "data" / "raw_pdfs" / "policy" / "hdfc_kyc_norms_aml.pdf").exists(),
        reason="source PDFs not present",
    ),
    pytest.mark.filterwarnings("ignore:Payload indexes have no effect:UserWarning"),
]
DOC = "hdfc_kyc_aml_faq"  # small native document: 13 eligible chunks


def test_unknown_document_key_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["index", "--document", "no_such_doc", "--dry-run"]) == 2
    assert "unknown document key(s): no_such_doc" in capsys.readouterr().out


def test_dry_run_counts_without_model_or_writes(capsys: pytest.CaptureFixture[str]) -> None:
    emb = FakeEmbedder()
    code = _index(get_settings(), [DOC], dry_run=True, force=False, prune=False, embedder=emb)
    out = capsys.readouterr().out
    assert code == 0 and "(dry run)" in out and emb.passages == []
    assert f"{DOC}" in out and "13" in out


def test_index_then_reindex_then_prune(capsys: pytest.CaptureFixture[str]) -> None:
    store = ChunkStore(QdrantClient(":memory:"), "cli_test")
    emb = FakeEmbedder()
    settings = get_settings()
    assert (
        _index(settings, [DOC], dry_run=False, force=False, prune=False, embedder=emb, store=store)
        == 0
    )
    assert store.count() == 13 and len(emb.passages) == 13
    capsys.readouterr()

    again = FakeEmbedder()
    assert (
        _index(
            settings, [DOC], dry_run=False, force=False, prune=False, embedder=again, store=store
        )
        == 0
    )
    out = capsys.readouterr().out
    assert again.passages == [] and "collection cli_test: 13 points" in out

    # A point for a document no longer in the manifest: reported, deleted only on --prune.
    from qdrant_client import models as qm

    store.upsert(
        [
            qm.PointStruct(
                id="00000000-0000-0000-0000-000000000001",
                vector=[1.0] + [0.0] * (DIM - 1),
                payload={"document_key": "retired_doc"},
            )
        ]
    )
    _index(settings, [DOC], dry_run=False, force=False, prune=False, embedder=again, store=store)
    out = capsys.readouterr().out
    assert "orphan: retired_doc (1 points)" in out and store.count() == 14
    _index(settings, [DOC], dry_run=False, force=False, prune=True, embedder=again, store=store)
    assert "pruned 1 orphan point(s)" in capsys.readouterr().out and store.count() == 13
