"""Qdrant access for chunk vectors (collection `QDRANT_COLLECTION`, docs/08).

- One point per embedding-eligible chunk; point id = `chunk_id` (deterministic UUID).
- Payload = `ChunkMetadata.to_qdrant_payload()` plus an `index` object with indexing
  bookkeeping (model, revision, input/payload hashes) used for idempotent re-indexing.
- `ensure_collection` creates the collection when it is missing and refuses to touch
  one whose vector size or distance differs (never silently recreated).
- Writes wait for completion (`wait=True`); the client has a request timeout.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import Settings
from app.schemas.corpus import QDRANT_BOOL_INDEXES, QDRANT_KEYWORD_INDEXES

INDEX_KEY = "index"  # payload object holding indexing bookkeeping
UPSERT_BATCH = 64
SCROLL_PAGE = 256
CLIENT_TIMEOUT_S = 30


class QdrantSchemaError(RuntimeError):
    """The existing collection does not match what the pipeline expects."""


@dataclass(frozen=True)
class PointState:
    """What is currently indexed for one point (from its payload)."""

    document_id: str
    input_sha256: str | None
    payload_sha256: str | None
    model: str | None
    revision: str | None
    document_key: str | None = None


def _state(payload: dict[str, Any]) -> PointState:
    idx = payload.get(INDEX_KEY) or {}
    return PointState(
        document_id=str(payload.get("document_id")),
        input_sha256=idx.get("input_sha256"),
        payload_sha256=idx.get("payload_sha256"),
        model=idx.get("embedding_model"),
        revision=idx.get("embedding_revision"),
        document_key=payload.get("document_key"),
    )


def _match(key: str, value: Any) -> qm.FieldCondition:
    return qm.FieldCondition(key=key, match=qm.MatchValue(value=value))


class ChunkStore:
    def __init__(self, client: QdrantClient, collection: str) -> None:
        self.client = client
        self.collection = collection

    # --- collection --------------------------------------------------------------

    def ensure_collection(self, dimension: int) -> bool:
        """Create the collection if missing (True) or validate the existing one (False)."""
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=qm.VectorParams(size=dimension, distance=qm.Distance.COSINE),
            )
            created = True
        else:
            params = self.client.get_collection(self.collection).config.params.vectors
            if not isinstance(params, qm.VectorParams):
                raise QdrantSchemaError(f"{self.collection}: expected one unnamed vector")
            if params.size != dimension or params.distance != qm.Distance.COSINE:
                raise QdrantSchemaError(
                    f"{self.collection}: vectors are {params.size}-d {params.distance}, "
                    f"pipeline needs {dimension}-d Cosine. Use a new QDRANT_COLLECTION or "
                    "delete the collection deliberately; it is never recreated automatically."
                )
            created = False
        for name in QDRANT_KEYWORD_INDEXES:
            self.client.create_payload_index(
                self.collection, name, qm.PayloadSchemaType.KEYWORD, wait=True
            )
        for name in QDRANT_BOOL_INDEXES:
            self.client.create_payload_index(
                self.collection, name, qm.PayloadSchemaType.BOOL, wait=True
            )
        return created

    # --- reads -------------------------------------------------------------------

    def _scroll(self, flt: qm.Filter | None, fields: list[str]) -> Iterable[qm.Record]:
        offset: Any = None
        while True:
            points, offset = self.client.scroll(
                self.collection,
                scroll_filter=flt,
                limit=SCROLL_PAGE,
                offset=offset,
                with_payload=fields,
                with_vectors=False,
            )
            yield from points
            if offset is None:
                return

    def point_states(self, document_key: str) -> dict[str, PointState]:
        states: dict[str, PointState] = {}
        flt = qm.Filter(must=[_match("document_key", document_key)])
        for p in self._scroll(flt, ["document_id", "document_key", INDEX_KEY]):
            states[str(p.id)] = _state(p.payload or {})
        return states

    def all_point_states(self) -> dict[str, PointState]:
        """Every point in the collection (reconciliation against Postgres)."""
        return {
            str(p.id): _state(p.payload or {})
            for p in self._scroll(None, ["document_id", "document_key", INDEX_KEY])
        }

    def document_keys(self) -> dict[str, int]:
        """Point count per document_key across the whole collection."""
        counts: dict[str, int] = {}
        for p in self._scroll(None, ["document_key"]):
            key = str((p.payload or {}).get("document_key"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def count(self, flt: qm.Filter | None = None) -> int:
        return self.client.count(self.collection, count_filter=flt, exact=True).count

    def search(
        self, vector: Sequence[float], limit: int, flt: qm.Filter | None = None
    ) -> list[qm.ScoredPoint]:
        return self.client.query_points(
            self.collection, query=list(vector), query_filter=flt, limit=limit,
            with_payload=True,
        ).points  # fmt: skip

    # --- writes ------------------------------------------------------------------

    def upsert(self, points: Sequence[qm.PointStruct]) -> None:
        for i in range(0, len(points), UPSERT_BATCH):
            self.client.upsert(
                self.collection, points=list(points[i : i + UPSERT_BATCH]), wait=True
            )

    def overwrite_payload(self, point_id: str, payload: dict[str, Any]) -> None:
        self.client.overwrite_payload(
            self.collection, payload=payload, points=[point_id], wait=True
        )

    def delete(self, ids: Iterable[str]) -> int:
        ids = sorted(ids)
        for i in range(0, len(ids), UPSERT_BATCH):
            self.client.delete(
                self.collection,
                points_selector=qm.PointIdsList(points=list(ids[i : i + UPSERT_BATCH])),
                wait=True,
            )
        return len(ids)

    def delete_document(self, document_key: str) -> int:
        n = self.count(qm.Filter(must=[_match("document_key", document_key)]))
        if n:
            self.client.delete(
                self.collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(must=[_match("document_key", document_key)])
                ),
                wait=True,
            )
        return n


def store_from_settings(settings: Settings) -> ChunkStore:
    api_key = settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None
    client = QdrantClient(url=settings.QDRANT_URL, api_key=api_key, timeout=CLIENT_TIMEOUT_S)
    return ChunkStore(client, settings.QDRANT_COLLECTION)
