"""Precomputed query vectors for the assistant's retrieval (owner decision, 2026-11-16).

The assistant embeds exactly one kind of text at runtime: a checklist catalogue item's
title (`checklist.py`, supplementary references). Customer text is never embedded. Every
query vector the assistant can need is therefore known in advance, so it is computed once,
offline, with the configured model and revision, and stored in `query_vectors.json`.

At runtime `PrecomputedEmbedder` returns the stored vector instead of loading torch and
sentence-transformers (about 490 MB), so the service fits a small instance. Retrieval is
unchanged: the stored vector is exactly what `SentenceTransformerEmbedder.embed_query`
returns (float32 values, the BGE query instruction included), and it goes to the same
Qdrant search with the same filters and tie-break.

Fails closed:
- the file must name the configured model and revision (`for_settings`);
- a query without a stored vector raises `EmbeddingError`. The checklist then keeps its
  cited evidence without supplementary references (A1 §7), never a different vector;
- `tests/unit/test_query_vectors.py` checks the file covers every catalogue title.

Regenerate after a catalogue title or the model changes (needs the model, e.g. locally):

    python -m app.assistant.query_vectors --write      (`make assistant-vectors`)
    python -m app.assistant.query_vectors --check      (bitwise comparison with live)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.retrieval.embeddings import BGE_QUERY_INSTRUCTION, EmbeddingError

VECTORS_FILE = Path(__file__).with_name("query_vectors.json")
FORMAT_VERSION = 1


class QueryVectorsError(RuntimeError):
    """The stored vectors do not match the configured model, or the file is invalid."""


def _encode(vector: Sequence[float]) -> str:
    # float32 little-endian: the model's own precision, so the round trip is exact.
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _decode(data: str, dimension: int) -> list[float]:
    raw = base64.b64decode(data)
    if len(raw) != 4 * dimension:
        raise QueryVectorsError(f"a stored vector has {len(raw) // 4} values, not {dimension}")
    return list(struct.unpack(f"<{dimension}f", raw))


@dataclass
class PrecomputedEmbedder:
    """An `Embedder` that serves stored query vectors (never loads a model)."""

    model_name: str
    revision: str | None
    vectors: dict[str, list[float]]
    dim: int

    @property
    def dimension(self) -> int:
        return self.dim

    def embed_query(self, text: str) -> list[float]:
        vector = self.vectors.get(text)
        if vector is None:
            raise EmbeddingError(
                "no precomputed query vector for this text; regenerate "
                "app/assistant/query_vectors.json (python -m app.assistant.query_vectors --write)"
            )
        return list(vector)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("PrecomputedEmbedder serves query vectors only")

    @classmethod
    def load(cls, path: Path = VECTORS_FILE) -> PrecomputedEmbedder:
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QueryVectorsError(f"cannot read {path.name}: {type(exc).__name__}") from exc
        if data.get("format") != FORMAT_VERSION:
            raise QueryVectorsError(f"{path.name}: unsupported format {data.get('format')!r}")
        if data.get("instruction") != BGE_QUERY_INSTRUCTION:
            raise QueryVectorsError(f"{path.name}: computed with another query instruction")
        dim = int(data["dimension"])
        vectors = {title: _decode(v, dim) for title, v in data["vectors"].items()}
        return cls(data["model"], data.get("revision"), vectors, dim)

    @classmethod
    def for_settings(cls, settings: Any, path: Path = VECTORS_FILE) -> PrecomputedEmbedder:
        """The stored vectors, only if they were computed with the configured model."""
        emb = cls.load(path)
        wanted = (settings.EMBEDDING_MODEL, settings.EMBEDDING_MODEL_REVISION)
        if (emb.model_name, emb.revision) != wanted:
            raise QueryVectorsError(
                f"{path.name} was computed with {emb.model_name}@{emb.revision}, but the "
                f"configured model is {wanted[0]}@{wanted[1]}; regenerate it"
            )
        return emb


def catalogue_queries() -> list[str]:
    """Every text the assistant can embed at runtime: the catalogue item titles."""
    from app.assistant.catalogue import catalogue

    return sorted({item.title for item in catalogue().items})


def _live(settings: Any, queries: list[str]) -> dict[str, list[float]]:
    from app.retrieval.embeddings import embedder_from_settings

    live = embedder_from_settings(settings)
    return {q: live.embed_query(q) for q in queries}


def write(settings: Any, path: Path = VECTORS_FILE) -> dict[str, Any]:
    queries = catalogue_queries()
    vectors = _live(settings, queries)
    dimension = len(next(iter(vectors.values())))
    data = {
        "format": FORMAT_VERSION,
        "model": settings.EMBEDDING_MODEL,
        "revision": settings.EMBEDDING_MODEL_REVISION,
        "instruction": BGE_QUERY_INSTRUCTION,
        "dimension": dimension,
        "count": len(queries),
        "vectors": {q: _encode(vectors[q]) for q in queries},
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data["vectors"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def check(settings: Any, path: Path = VECTORS_FILE) -> list[str]:
    """Differences between the stored vectors and a live embedding (empty = identical)."""
    stored = PrecomputedEmbedder.for_settings(settings, path)
    queries = catalogue_queries()
    problems = [f"missing: {q}" for q in queries if q not in stored.vectors]
    problems += [f"not a catalogue title: {q}" for q in stored.vectors if q not in queries]
    live = _live(settings, [q for q in queries if q in stored.vectors])
    problems += [f"differs: {q}" for q, v in live.items() if v != stored.vectors[q]]
    return problems


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="compute and store the vectors")
    mode.add_argument("--check", action="store_true", help="compare with a live embedding")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.write:
        data = write(settings)
        print(f"wrote {data['count']} vectors ({data['model']}@{data['revision']}), "
              f"sha256 {data['sha256'][:16]}")  # fmt: skip
        return 0
    problems = check(settings)
    print("\n".join(problems) if problems else "identical: every stored vector equals live")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
