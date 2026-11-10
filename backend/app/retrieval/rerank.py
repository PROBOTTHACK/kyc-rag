"""Optional cross-encoder reranking (M2 B1, docs/04; disabled by default).

A cross-encoder scores each (query, passage) pair jointly and reorders a fixed candidate
set. Here it only REORDERS: the candidates come from the normal filtered retrieval, so
metadata filters and evidence eligibility hold by construction, and no candidate is
added, dropped for its score, or modified. Payloads, citations and page references are
passed through unchanged (the same objects).

- Scored text: the retrieved chunk's `embedding_input` (child `context` + `content`), the
  same text dense retrieval embeds; never the parent paragraph (parents can exceed the
  512-token window and are a different retrieval unit). Over-long pairs are truncated by
  the model for SCORING only; stored text is never touched.
- Scores are RAW model outputs (logits; the activation is fixed to identity). They are
  uncalibrated and model-specific: never a confidence, never a probability, never
  thresholded, never an evidence-quality signal. They only order the candidates.
- The model loads lazily, on the first `score()` call, so nothing is loaded unless
  reranking is requested. A load or inference failure raises `RerankError`; the caller
  (`Retriever`) turns it into a labelled degraded result with the original order.
"""

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from app.config import Settings
from app.retrieval.keyword import index_text
from app.retrieval.search import SearchHit

if TYPE_CHECKING:  # heavy import (torch); loaded lazily at first use
    from sentence_transformers import CrossEncoder

MAX_LENGTH = 512  # tokens per (query, passage) pair; the model's window
BATCH_SIZE = 16
DEVICE = "cpu"  # M2: CPU only (the installed torch is a CPU build)


class RerankError(RuntimeError):
    """The reranker model cannot be loaded or failed to score."""


class Reranker(Protocol):
    model_name: str
    revision: str | None

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    """`sentence_transformers.CrossEncoder` at a pinned revision, on CPU, loaded lazily."""

    def __init__(self, model_name: str, revision: str | None = None) -> None:
        self.model_name = model_name
        self.revision = revision
        self._model: CrossEncoder | None = None
        self.load_ms = 0.0  # time of the one-off model load (0 until loaded)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> "CrossEncoder":
        if self._model is None:
            started = time.perf_counter()
            try:
                import torch
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(
                    self.model_name,
                    revision=self.revision,
                    max_length=MAX_LENGTH,
                    device=DEVICE,
                    activation_fn=torch.nn.Identity(),  # raw scores, never squashed
                )
            except Exception as exc:
                raise RerankError(
                    f"cannot load reranker {self.model_name}@{self.revision}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self.load_ms = (time.perf_counter() - started) * 1000
        return self._model

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        model = self._load()
        try:
            raw: Any = model.predict(
                [(query, t) for t in texts],
                batch_size=BATCH_SIZE,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            return [float(x) for x in raw]
        except Exception as exc:
            raise RerankError(f"reranker inference failed: {type(exc).__name__}: {exc}") from exc


def reranker_from_settings(settings: Settings) -> CrossEncoderReranker:
    return CrossEncoderReranker(settings.RERANK_MODEL, settings.RERANK_MODEL_REVISION)


def rerank(
    query: str, hits: Sequence[SearchHit], reranker: Reranker, top_k: int
) -> list[tuple[SearchHit, SearchHit]]:
    """Reorder `hits` by raw cross-encoder score and keep `top_k`.

    Returns `(new_hit, original_hit)` pairs in the new order. `new_hit` has the new rank,
    the raw cross-encoder score and the SAME payload object as `original_hit`. Ties on
    the cross-encoder score keep the original retrieval order (then `chunk_id`).
    """
    scores = reranker.score(query, [index_text(h.payload) for h in hits])
    if len(scores) != len(hits):
        raise RerankError(f"reranker returned {len(scores)} scores for {len(hits)} passages")
    order = sorted(
        zip(scores, hits, strict=True), key=lambda sh: (-sh[0], sh[1].rank, sh[1].chunk_id)
    )[:top_k]
    return [
        (SearchHit(rank, score, h.chunk_id, h.payload), h)
        for rank, (score, h) in enumerate(order, start=1)
    ]
