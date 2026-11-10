"""Persisted form schemas (M2 C2; table `form_schemas`, migration 0003; docs/08).

Versioning semantics (docs/milestones/M2_REMAINING_PLAN.md, C2):
- Identity = (document, extraction method, extractor version). The document is the exact
  file (its id is derived from the SHA-256), so a new PDF is a new identity, and a new
  `extractor_version` is a new identity. Older rows are kept.
- Saving an identity that is not stored inserts it (`inserted`).
- Saving an identity that is stored with the SAME schema (same canonical SHA-256) writes
  nothing (`unchanged`): repeat extraction is idempotent.
- Saving an identity that is stored with a DIFFERENT schema is refused with
  `FormSchemaConflict`; the stored row is never overwritten. Extraction is deterministic,
  so this means the stored chunks changed (e.g. a re-chunked document) or the extractor
  changed without a version bump; either needs a person (or a new extractor version).
- Nothing here infers obligations: `mandatory_under_policy` stays unset, and the database
  rejects any row whose fields carry it.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.cache import JsonCache
from app.db.models import Document, FormSchemaRow
from app.forms.extract import extract_form
from app.schemas.corpus import ID_NAMESPACE
from app.schemas.forms import EXTRACTION_METHOD, FormSchema

SaveStatus = Literal["inserted", "unchanged"]


class FormSchemaConflict(RuntimeError):
    """A different schema exists under the same (document, method, version) identity."""


def form_schema_id(
    document_sha256: str, extraction_method: str, extractor_version: int
) -> uuid.UUID:
    return uuid.uuid5(
        ID_NAMESPACE, f"form_schema:{document_sha256}:{extraction_method}:{extractor_version}"
    )


def canonical_json(schema: FormSchema) -> str:
    """Stable serialisation (sorted keys, no whitespace) used for the schema hash."""
    return json.dumps(schema.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def schema_sha256(schema: FormSchema) -> str:
    return hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()


def form_schema_row(schema: FormSchema) -> FormSchemaRow:
    return FormSchemaRow(
        id=form_schema_id(
            schema.document_sha256, schema.extraction_method, schema.extractor_version
        ),
        document_id=uuid.UUID(schema.document_id),
        form_key=schema.form_key,
        document_sha256=schema.document_sha256,
        document_role=schema.document_role,
        evidence_eligibility=schema.evidence_eligibility,
        extraction_method=schema.extraction_method,
        extractor_version=schema.extractor_version,
        schema_sha256=schema_sha256(schema),
        schema_json=schema.model_dump(mode="json"),
        section_count=len(schema.sections),
        field_count=schema.field_count,
        required_as_shown_count=schema.required_as_shown_count,
        text_block_count=schema.text_block_count,
        quality_flags=list(schema.flags),
    )


@dataclass(frozen=True)
class SaveOutcome:
    form_key: str
    status: SaveStatus
    extractor_version: int
    schema_sha256: str


def save_form_schema(session: Session, schema: FormSchema) -> SaveOutcome:
    """Insert `schema`, or confirm an identical stored one; never overwrite (see module)."""
    document = session.get(Document, uuid.UUID(schema.document_id))
    if document is None or document.sha256 != schema.document_sha256:
        raise ValueError(f"{schema.form_key}: schema does not match a stored document")
    row = form_schema_row(schema)
    existing = session.scalar(
        select(FormSchemaRow).where(
            FormSchemaRow.document_id == row.document_id,
            FormSchemaRow.extraction_method == row.extraction_method,
            FormSchemaRow.extractor_version == row.extractor_version,
        )
    )
    if existing is not None:
        if existing.schema_sha256 != row.schema_sha256:
            raise FormSchemaConflict(
                f"{schema.form_key}: a different schema is already stored for extractor "
                f"version {schema.extractor_version} ({existing.schema_sha256[:12]} vs "
                f"{row.schema_sha256[:12]}); refusing to overwrite. The stored chunks or the "
                "extractor changed: bump the extractor version or review the change."
            )
        return SaveOutcome(
            schema.form_key, "unchanged", schema.extractor_version, row.schema_sha256
        )
    session.add(row)
    session.flush()
    return SaveOutcome(schema.form_key, "inserted", schema.extractor_version, row.schema_sha256)


def extract_and_save(session: Session, document_key: str) -> SaveOutcome:
    """Extract one stored form (C1) and persist it; the caller commits."""
    return save_form_schema(session, extract_form(session, document_key))


def stored_form_schema_row(
    session: Session, document_id: uuid.UUID, extraction_method: str, extractor_version: int
) -> FormSchemaRow | None:
    """The row stored under one identity, if any (used to skip provider calls)."""
    return session.scalar(
        select(FormSchemaRow).where(
            FormSchemaRow.document_id == document_id,
            FormSchemaRow.extraction_method == extraction_method,
            FormSchemaRow.extractor_version == extractor_version,
        )
    )


def load_form_schema(
    session: Session,
    form_key: str,
    *,
    extractor_version: int | None = None,
    extraction_method: str = EXTRACTION_METHOD,
) -> FormSchema | None:
    """The stored schema of the CURRENT document under `form_key` for one extraction
    method (default: the deterministic `m1_chunks`; methods are never mixed), at the
    highest extractor version unless one is given, validated back into a `FormSchema`."""
    document = session.scalar(select(Document).where(Document.document_key == form_key))
    if document is None:
        return None
    stmt = select(FormSchemaRow).where(
        FormSchemaRow.document_id == document.id,
        FormSchemaRow.extraction_method == extraction_method,
    )
    if extractor_version is not None:
        stmt = stmt.where(FormSchemaRow.extractor_version == extractor_version)
    row = session.scalar(stmt.order_by(FormSchemaRow.extractor_version.desc()).limit(1))
    return FormSchema.model_validate(row.schema_json) if row is not None else None


CACHE_FORMAT = 1  # bump when the cached value's structure changes


def _identity(
    session: Session, form_key: str, extraction_method: str, extractor_version: int | None
) -> tuple[uuid.UUID, int, str] | None:
    """(row id, version, schema SHA-256) of the row `load_form_schema` would return, read
    from Postgres without the JSON payload."""
    stmt = (
        select(FormSchemaRow.id, FormSchemaRow.extractor_version, FormSchemaRow.schema_sha256)
        .join(Document, Document.id == FormSchemaRow.document_id)
        .where(
            Document.document_key == form_key,
            FormSchemaRow.extraction_method == extraction_method,
        )
    )
    if extractor_version is not None:
        stmt = stmt.where(FormSchemaRow.extractor_version == extractor_version)
    row = session.execute(stmt.order_by(FormSchemaRow.extractor_version.desc()).limit(1)).first()
    return (row[0], row[1], row[2]) if row is not None else None


def load_form_schema_cached(
    session: Session,
    cache: JsonCache | None,
    form_key: str,
    *,
    extractor_version: int | None = None,
    extraction_method: str = EXTRACTION_METHOD,
) -> FormSchema | None:
    """`load_form_schema` with a Redis read-through cache keyed by
    (form_key, extraction_method, extractor_version).

    Postgres decides WHICH row is current (a small query without the JSON), so a newly
    inserted version is found immediately and a deleted row is never served. A cached
    entry is used only if its row id and schema SHA-256 equal that row's and its JSON
    re-hashes to the same SHA-256; otherwise it is deleted. Misses, unusable entries and
    any Redis failure load from Postgres (identical result) and refresh the entry."""
    if cache is None:
        return load_form_schema(
            session,
            form_key,
            extractor_version=extractor_version,
            extraction_method=extraction_method,
        )
    ident = _identity(session, form_key, extraction_method, extractor_version)
    if ident is None:
        return None
    row_id, version, sha = ident
    key = cache.key(form_key, extraction_method, f"v{version}")
    entry = cache.get(key)
    if entry is not None:
        schema = _usable(entry, row_id, sha)
        if schema is not None:
            cache.stats.hits += 1
            return schema
        cache.reject(key)
    cache.stats.misses += 1
    row = session.get(FormSchemaRow, row_id)
    if row is None:  # deleted between the two reads
        return None
    schema = FormSchema.model_validate(row.schema_json)
    cache.set(
        key,
        {
            "format": CACHE_FORMAT,
            "row_id": str(row.id),
            "schema_sha256": row.schema_sha256,
            "schema_json": row.schema_json,
        },
    )
    return schema


def _usable(entry: dict[str, object], row_id: uuid.UUID, sha: str) -> FormSchema | None:
    if entry.get("format") != CACHE_FORMAT or entry.get("row_id") != str(row_id):
        return None
    if entry.get("schema_sha256") != sha:
        return None
    try:
        schema = FormSchema.model_validate(entry.get("schema_json"))
    except ValidationError:
        return None
    return schema if schema_sha256(schema) == sha else None  # integrity of the payload


def list_form_schemas(session: Session) -> list[FormSchemaRow]:
    return list(
        session.scalars(
            select(FormSchemaRow).order_by(
                FormSchemaRow.form_key,
                FormSchemaRow.extraction_method.desc(),  # m1_chunks first
                FormSchemaRow.extractor_version,
            )
        )
    )
