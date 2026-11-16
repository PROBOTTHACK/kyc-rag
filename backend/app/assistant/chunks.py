"""Stored chunks as the checklist sees them (A4). Citations are always copied from these
rows, never from retrieval payloads or LLM output (A1 §8.2 rule 2)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.db.repository import get_chunks


@dataclass(frozen=True)
class StoredChunk:
    chunk_id: str
    document_key: str
    bank: str
    page: int
    page_end: int | None
    section: str | None
    content: str
    text_reliability: str
    extraction_method: str


class ChunkSource(Protocol):
    def get(self, chunk_ids: Iterable[str]) -> dict[str, StoredChunk]:
        """Unknown ids are omitted. Raises on a storage failure."""
        ...


@dataclass(frozen=True)
class PostgresChunkSource:
    """Reuses the existing repository lookup (`get_chunks`); one query per build."""

    sessions: sessionmaker[Session]

    def get(self, chunk_ids: Iterable[str]) -> dict[str, StoredChunk]:
        ids = sorted(set(chunk_ids))
        if not ids:
            return {}
        with self.sessions() as session:
            records = get_chunks(session, ids)
        out: dict[str, StoredChunk] = {}
        for cid, rec in records.items():
            m = rec.metadata
            out[cid] = StoredChunk(
                chunk_id=cid,
                document_key=m.document_key,
                bank=str(m.bank),
                page=m.page_number,
                page_end=m.page_end,
                section=m.section,
                content=m.content,
                text_reliability=str(m.text_reliability),
                extraction_method=str(m.extraction_method),
            )
        return out
