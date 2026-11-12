"""M4 S6 on the real ingested corpus: the read-only supersession lookup.

The manifest records no `supersedes` relation today (docs/14), so every document must come
back with none: no conflict can be resolved on this corpus (decision D10).
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Document
from app.graph.corpus import PostgresCorpusReader
from app.verification.conflicts import resolve

pytestmark = pytest.mark.integration


def test_every_document_has_its_relation_row_and_none_supersedes_another(
    corpus_db: sessionmaker[Session], corpus_store: Any
) -> None:
    """`corpus_store` ingests the full manifest corpus into `corpus_db`."""
    with corpus_db() as session:
        keys = sorted(session.scalars(select(Document.document_key)).all())
    reader = PostgresCorpusReader(corpus_db)
    relations = reader.document_relations(keys)
    assert sorted(relations) == keys and len(keys) >= 2
    assert all(r.supersedes == () for r in relations.values())
    assert relations["rbi_kyc_directions_2025"].source_status == "active"
    # so a conflict between any two corpus documents stays unresolved
    a, b = keys[0], keys[1]
    assert resolve(a, b, relations) == ("unresolved", "no_supersession_relation", None)


def test_unknown_and_empty_lookups(corpus_db: sessionmaker[Session]) -> None:
    reader = PostgresCorpusReader(corpus_db)
    assert reader.document_relations([]) == {}
    assert reader.document_relations(["no_such_document"]) == {}
