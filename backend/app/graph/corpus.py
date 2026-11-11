"""Read-only corpus access for the workflow nodes (M3 S4).

`CorpusReader` is the only way the S4 nodes read Postgres, so they stay pure and testable
with a fake. `PostgresCorpusReader` implements it with the existing M1/M2 functions only:
`evidence_context` (parent expansion), `form_documents`, `list_form_schemas` and
`load_form_schema_cached` (Postgres decides the current row; Redis is a verified
read-through cache). Nothing here writes, and nothing calls a model.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.core.cache import JsonCache
from app.db.forms import list_form_schemas, load_form_schema_cached
from app.db.repository import EvidenceContext, evidence_context, form_documents
from app.retrieval.retrieve import RetrievalMode, RetrievalResult
from app.retrieval.search import SearchFilters
from app.schemas.forms import FormSchema


@dataclass(frozen=True)
class FormDocument:
    document_key: str
    document_id: str
    title: str
    entity_types: tuple[str, ...]
    residency_statuses: tuple[str, ...]


@dataclass(frozen=True)
class FormSchemaRowInfo:
    """Identity and flags of one stored `form_schemas` row (no schema JSON)."""

    form_key: str
    method: str
    version: int
    row_id: str
    schema_sha256: str
    quality_flags: tuple[str, ...]


class SupportsRetrieve(Protocol):
    """What the retrieval node needs from `app.retrieval.retrieve.Retriever` (sync)."""

    def retrieve(
        self,
        query: str,
        *,
        mode: RetrievalMode | str,
        top_k: int = 5,
        filters: SearchFilters | None = None,
        rerank: bool = False,
    ) -> RetrievalResult: ...


class CorpusReader(Protocol):
    def evidence(self, chunk_ids: Iterable[str]) -> dict[str, EvidenceContext]: ...

    def form_documents(self) -> list[FormDocument]: ...

    def form_schema_rows(self) -> list[FormSchemaRowInfo]: ...

    def load_schema(self, form_key: str, method: str, version: int) -> FormSchema | None: ...


@dataclass
class PostgresCorpusReader:
    sessions: sessionmaker[Session]
    cache: JsonCache | None = None

    def evidence(self, chunk_ids: Iterable[str]) -> dict[str, EvidenceContext]:
        with self.sessions() as session:
            return evidence_context(session, list(chunk_ids))

    def form_documents(self) -> list[FormDocument]:
        with self.sessions() as session:
            return [
                FormDocument(
                    document_key=d.document_key,
                    document_id=str(d.id),
                    title=d.title,
                    entity_types=tuple(d.entity_types or ()),
                    residency_statuses=tuple(d.residency_statuses or ()),
                )
                for d in form_documents(session)
            ]

    def form_schema_rows(self) -> list[FormSchemaRowInfo]:
        with self.sessions() as session:
            return [
                FormSchemaRowInfo(
                    form_key=r.form_key,
                    method=r.extraction_method,
                    version=r.extractor_version,
                    row_id=str(r.id),
                    schema_sha256=r.schema_sha256,
                    quality_flags=tuple(r.quality_flags or ()),
                )
                for r in list_form_schemas(session)
            ]

    def load_schema(self, form_key: str, method: str, version: int) -> FormSchema | None:
        with self.sessions() as session:
            return load_form_schema_cached(
                session, self.cache, form_key, extractor_version=version, extraction_method=method
            )
