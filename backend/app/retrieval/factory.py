"""The settings-based `Retriever` factory (M3 S2).

Public so that the CLI and the M3 workflow build retrieval the same way. It is the former
private `app.ingestion.cli._retriever`, moved unchanged: every dependency loads lazily
(BM25 alone never loads the embedding model or contacts Qdrant; the cross-encoder loads
only when reranking is requested), and the configuration comes from `Settings`.
"""

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.retrieval.embeddings import Embedder, embedder_from_settings
from app.retrieval.keyword import KeywordIndexManager
from app.retrieval.qdrant_store import ChunkStore, store_from_settings
from app.retrieval.rerank import reranker_from_settings
from app.retrieval.retrieve import Retriever


def retriever_from_settings(
    settings: Settings,
    *,
    sessions: sessionmaker[Session],
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
) -> Retriever:
    """A retriever whose dependencies load lazily. `embedder` / `store` replace the
    settings-built ones (tests and long-lived processes share them); `sessions` is the
    sync sessionmaker the BM25 index reads Postgres chunk rows through."""
    return Retriever(
        embedder=lambda: embedder or embedder_from_settings(settings),
        store=lambda: store or store_from_settings(settings),
        keyword=lambda: KeywordIndexManager(sessions, stopwords=settings.BM25_STOPWORDS),
        rrf_k=settings.RRF_K,
        candidates=settings.RETRIEVAL_CANDIDATES,
        reranker=lambda: reranker_from_settings(settings),  # loaded only if reranking
        rerank_candidates=settings.RERANK_CANDIDATES,
    )
