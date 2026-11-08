"""BGE-small-en-v1.5 embeddings for Pipeline A (M1 Stage D).

- Passage input = child context (if any) + chunk content (`embedding_input`). Title and
  section stay in the payload for filtering/display but are not embedded: on a dev probe
  set (8 queries, separate from the Stage G benchmark) adding them let short chunks rank
  on the document title alone (MRR 0.67 with title+section vs 0.81 without; docs/04).
  All 429 candidates fit in the model's 512 tokens; an input that would be truncated
  raises `EmbeddingError` instead of being silently shortened.
- Queries get the BGE v1.5 retrieval instruction; passages do not (model card usage).
- Vectors are L2-normalised (the model ends in a Normalize layer and `encode` is asked
  to normalise too), so cosine similarity = dot product.
- The model id and revision come from settings (`EMBEDDING_MODEL`,
  `EMBEDDING_MODEL_REVISION`); runs on CPU.
"""

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from app.config import Settings
from app.schemas.corpus import ChunkMetadata

if TYPE_CHECKING:  # heavy import (torch); loaded lazily at first use
    from sentence_transformers import SentenceTransformer

EMBEDDING_INPUT_VERSION = 2  # bump when `embedding_input` changes: forces re-embedding
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """The embedding model cannot be loaded or an input does not fit."""


def embedding_input(meta: ChunkMetadata) -> str:
    """Deterministic text that is embedded for a chunk (never stored as chunk content)."""
    return "\n".join(p for p in (meta.context, meta.content) if p)


def input_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Embedder(Protocol):
    model_name: str
    revision: str | None

    @property
    def dimension(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedder:
    """Lazy-loading sentence-transformers wrapper (CPU)."""

    def __init__(
        self, model_name: str, revision: str | None = None, batch_size: int = BATCH_SIZE
    ) -> None:
        self.model_name = model_name
        self.revision = revision
        self.batch_size = batch_size
        self._model: SentenceTransformer | None = None

    @property
    def model(self) -> "SentenceTransformer":
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - dependency is required
                raise EmbeddingError("sentence-transformers is not installed") from exc
            try:
                self._model = SentenceTransformer(
                    self.model_name, revision=self.revision, device="cpu"
                )
            except Exception as exc:  # network, missing revision, corrupt cache, ...
                raise EmbeddingError(
                    f"cannot load embedding model {self.model_name}"
                    f"@{self.revision or 'latest'}: {type(exc).__name__}: {exc}"
                ) from exc
        return self._model

    @property
    def dimension(self) -> int:
        dim = self.model.get_embedding_dimension()
        if dim is None:
            raise EmbeddingError(f"{self.model_name} does not report an embedding dimension")
        return int(dim)

    @property
    def max_tokens(self) -> int:
        limit = self.model.max_seq_length
        if limit is None:
            raise EmbeddingError(f"{self.model_name} does not report a maximum sequence length")
        return int(limit)

    def token_count(self, text: str) -> int:
        return len(self.model.tokenizer(text)["input_ids"])

    def _check_lengths(self, texts: Sequence[str]) -> None:
        too_long = [
            (i, n) for i, t in enumerate(texts) if (n := self.token_count(t)) > self.max_tokens
        ]
        if too_long:
            raise EmbeddingError(
                f"{len(too_long)} input(s) exceed {self.max_tokens} tokens and would be "
                f"truncated (index, tokens): {too_long[:5]}"
            )

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        self._check_lengths(texts)
        vectors = self.model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in vectors]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts) if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._encode([BGE_QUERY_INSTRUCTION + text])[0]


def embedder_from_settings(settings: Settings) -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(
        settings.EMBEDDING_MODEL, revision=settings.EMBEDDING_MODEL_REVISION
    )
