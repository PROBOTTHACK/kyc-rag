"""Idempotent indexing of embedding-eligible chunks into Qdrant (M1 Stage D).

Per document, the current chunks are compared with what Qdrant already holds:

- new chunk, or a changed embedding input / model / revision -> embed and upsert;
- only the metadata payload changed (e.g. an OCR-policy flag) -> overwrite the payload,
  keeping the vector (no re-embedding);
- identical -> skipped;
- points of this document that are no longer current eligible chunks (re-chunked,
  policy-excluded, parents, an older version of the document) -> deleted as stale,
  after the upserts succeed, so a failure never leaves a document without vectors.

Documents absent from the manifest are only reported (`find_orphans`); deleting them
requires an explicit `prune_orphans`. Parents and chunks with
`embedding_eligible = false` are never indexed. The Stage E pipeline
(`app.ingestion.pipeline`) calls this with chunks rebuilt from committed Postgres rows.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import models as qm

from app.ingestion.chunking import ChunkingResult, ChunkRecord
from app.retrieval.embeddings import (
    EMBEDDING_INPUT_VERSION,
    Embedder,
    embedding_input,
    input_sha256,
)
from app.retrieval.qdrant_store import INDEX_KEY, ChunkStore


@dataclass
class IndexReport:
    document_key: str
    chunks: int  # all chunks of the document, parents included
    eligible: int  # embedding-eligible (indexed) chunks
    embedded: int = 0  # new or re-embedded
    payload_updated: int = 0  # metadata changed, vector kept
    unchanged: int = 0
    stale_deleted: int = 0
    stale_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.embedded or self.payload_updated or self.stale_deleted)


def payload_sha256(payload: dict[str, Any]) -> str:
    """Hash of the metadata payload (sorted JSON), excluding indexing bookkeeping."""
    body = {k: v for k, v in payload.items() if k != INDEX_KEY}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def point_payload(record: ChunkRecord, embedder: Embedder) -> dict[str, Any]:
    payload = record.metadata.to_qdrant_payload()
    text = embedding_input(record.metadata)
    payload[INDEX_KEY] = {
        "embedding_model": embedder.model_name,
        "embedding_revision": embedder.revision,
        "input_version": EMBEDDING_INPUT_VERSION,
        "input_sha256": input_sha256(f"{EMBEDDING_INPUT_VERSION}|{text}"),
        "payload_sha256": payload_sha256(payload),
    }
    return payload


@dataclass
class IndexPlan:
    """What `index_document` would do for one document (computed read-only)."""

    report: IndexReport
    to_embed: list[tuple[ChunkRecord, dict[str, Any]]] = field(default_factory=list)
    to_overwrite: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)


def plan_index(
    result: ChunkingResult, embedder: Embedder, store: ChunkStore, *, force: bool = False
) -> IndexPlan:
    """Compare `result` with Qdrant without writing or loading the model (dry runs).

    Only `embedder.model_name` / `revision` are read; nothing is embedded.
    """
    eligible = [c for c in result.chunks if c.embeddable]
    plan = IndexPlan(
        IndexReport(result.document_key, chunks=len(result.chunks), eligible=len(eligible))
    )
    existing = store.point_states(result.document_key)
    for rec in eligible:
        payload = point_payload(rec, embedder)
        idx = payload[INDEX_KEY]
        have = existing.get(rec.metadata.chunk_id)
        same_vector = (
            have is not None
            and have.input_sha256 == idx["input_sha256"]
            and have.model == embedder.model_name
            and have.revision == embedder.revision
        )
        if force or not same_vector:
            plan.to_embed.append((rec, payload))
        elif have is not None and have.payload_sha256 != idx["payload_sha256"]:
            plan.to_overwrite.append((rec.metadata.chunk_id, payload))
        else:
            plan.report.unchanged += 1
    current = {rec.metadata.chunk_id for rec in eligible}
    plan.stale = sorted(set(existing) - current)
    return plan


def index_document(
    result: ChunkingResult, embedder: Embedder, store: ChunkStore, *, force: bool = False
) -> IndexReport:
    """Bring Qdrant in line with `result` for one document. `force` re-embeds all."""
    plan = plan_index(result, embedder, store, force=force)
    report = plan.report
    if plan.to_embed:
        vectors = embedder.embed_passages([embedding_input(r.metadata) for r, _ in plan.to_embed])
        store.upsert(
            [
                qm.PointStruct(id=rec.metadata.chunk_id, vector=vec, payload=payload)
                for (rec, payload), vec in zip(plan.to_embed, vectors, strict=True)
            ]
        )
        report.embedded = len(plan.to_embed)
    for point_id, payload in plan.to_overwrite:
        store.overwrite_payload(point_id, payload)
    report.payload_updated = len(plan.to_overwrite)
    # Stale points go only after the upserts succeeded.
    report.stale_deleted = store.delete(plan.stale)
    report.stale_ids = plan.stale
    return report


def find_orphans(store: ChunkStore, manifest_keys: Iterable[str]) -> dict[str, int]:
    """Indexed document_keys that are not in the manifest, with their point counts."""
    keep = set(manifest_keys)
    return {k: n for k, n in store.document_keys().items() if k not in keep}


def prune_orphans(store: ChunkStore, orphans: Iterable[str]) -> int:
    return sum(store.delete_document(key) for key in sorted(orphans))
