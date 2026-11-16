"""Precomputed query vectors (owner decision, 2026-11-16): complete, exact, fail-closed, and
the assistant never loads torch when they are on. No network, no model (except the opt-in
`--check` command, which is not a test)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.assistant.query_vectors import (
    FORMAT_VERSION,
    VECTORS_FILE,
    PrecomputedEmbedder,
    QueryVectorsError,
    _decode,
    _encode,
    catalogue_queries,
)
from app.config import REPO_ROOT, Settings
from app.retrieval.embeddings import BGE_QUERY_INSTRUCTION, EmbeddingError

EXAMPLE_ENV = REPO_ROOT / "infra" / ".env.example"  # never the real .env


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=EXAMPLE_ENV, **kw)  # type: ignore[call-arg]


def test_the_committed_file_covers_exactly_the_catalogue_titles_for_the_configured_model() -> None:
    emb = PrecomputedEmbedder.for_settings(settings())
    assert sorted(emb.vectors) == catalogue_queries()  # a title change needs --write
    assert emb.dimension == 384
    assert all(len(v) == 384 for v in emb.vectors.values())
    # BGE vectors are L2-normalised.
    assert all(abs(sum(x * x for x in v) - 1.0) < 1e-4 for v in emb.vectors.values())


def test_a_stored_vector_is_returned_as_is_and_unknown_text_fails_closed() -> None:
    emb = PrecomputedEmbedder.for_settings(settings())
    title = catalogue_queries()[0]
    assert emb.embed_query(title) == emb.vectors[title]
    assert emb.embed_query(title) is not emb.vectors[title]  # a copy: callers can't mutate it
    with pytest.raises(EmbeddingError):
        emb.embed_query("a customer message is never embedded")
    with pytest.raises(EmbeddingError):
        emb.embed_passages(["passages are never embedded from stored vectors"])


def test_another_model_or_revision_is_refused() -> None:
    with pytest.raises(QueryVectorsError):
        PrecomputedEmbedder.for_settings(settings(EMBEDDING_MODEL_REVISION="0" * 40))
    with pytest.raises(QueryVectorsError):
        PrecomputedEmbedder.for_settings(settings(EMBEDDING_MODEL="BAAI/bge-base-en-v1.5"))


def test_the_float32_round_trip_is_exact() -> None:
    import struct

    # Values as the live embedder returns them: float32 values held as Python floats.
    vector = [struct.unpack("<f", struct.pack("<f", x / 7))[0] for x in range(-5, 5)]
    assert _decode(_encode(vector), len(vector)) == vector
    with pytest.raises(QueryVectorsError):
        _decode(_encode(vector), len(vector) + 1)


@pytest.mark.parametrize(
    "change", [{"format": FORMAT_VERSION + 1}, {"instruction": "another instruction: "}]
)
def test_an_incompatible_file_is_refused(tmp_path: Path, change: dict[str, Any]) -> None:
    data = json.loads(VECTORS_FILE.read_text(encoding="utf-8")) | change
    path = tmp_path / "query_vectors.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(QueryVectorsError):
        PrecomputedEmbedder.load(path)
    assert json.loads(VECTORS_FILE.read_text(encoding="utf-8"))["instruction"] == (
        BGE_QUERY_INSTRUCTION
    )


def test_the_assistant_serves_stored_vectors_without_importing_torch() -> None:
    # A subprocess: the test session may already have torch loaded by other tests.
    code = f"""
import sys
from app.config import Settings
from app.assistant.service import LazyChecklist
from app.assistant.query_vectors import PrecomputedEmbedder, catalogue_queries
s = Settings(_env_file={str(EXAMPLE_ENV)!r})
svc = LazyChecklist(s)()
emb = svc.retriever._retriever._embedder_factory()
assert isinstance(emb, PrecomputedEmbedder), type(emb)
emb.embed_query(catalogue_queries()[0])
print('torch' in sys.modules, 'sentence_transformers' in sys.modules)
"""
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code],
        cwd=REPO_ROOT / "backend",
        env={k: v for k, v in __import__("os").environ.items() if k != "ASSISTANT_QUERY_VECTORS"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip().splitlines()[-1] == "False False"


def test_live_embedding_stays_available_when_switched_off() -> None:
    from app.assistant.service import LazyChecklist
    from app.retrieval.embeddings import SentenceTransformerEmbedder

    svc = LazyChecklist(settings(ASSISTANT_QUERY_VECTORS=False))()
    emb = svc.retriever._retriever._embedder_factory()  # type: ignore[union-attr]
    assert isinstance(emb, SentenceTransformerEmbedder)  # constructed, not loaded
