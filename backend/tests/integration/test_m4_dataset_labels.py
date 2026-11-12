"""M4 S3: every evidence reference in the claim datasets resolves in PostgreSQL with
correct provenance, and the database-checkable labels agree with the stored chunks.

Read-only against the developer database (`make ingest`). It fails rather than skips when
the corpus is missing: a label check that silently skipped would prove nothing.
"""

import re
from typing import Any

import pytest
import sqlalchemy as sa

from app.config import get_settings
from tests.integration.conftest import sync_engine
from tests.unit.test_m4_datasets import SPLITS

pytestmark = pytest.mark.integration


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def stored() -> dict[str, Any]:
    ids = sorted({e["chunk_id"] for rows in SPLITS.values() for r in rows for e in r["evidence"]})
    engine = sync_engine(get_settings().DATABASE_URL.get_secret_value())
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT c.id::text AS id, c.document_key, c.page_number, c.page_end, "
                    "c.content, c.document_type, c.evidence_eligibility, c.source_status, "
                    "c.text_reliability, c.can_support_mandatory, c.chunk_role "
                    "FROM chunks c WHERE c.id::text = ANY(:ids)"
                ),
                {"ids": ids},
            ).all()
    finally:
        engine.dispose()
    found = {r.id: r for r in rows}
    missing = sorted(set(ids) - set(found))
    assert not missing, f"cited chunks missing from Postgres (run `make ingest`): {missing}"
    return found


CASES = [(split, r) for split, rows in SPLITS.items() for r in rows]


@pytest.mark.parametrize(("split", "case"), CASES, ids=[c["id"] for _, c in CASES])
def test_evidence_provenance_and_quotes(split: str, case: dict[str, Any], stored: Any) -> None:
    for e in case["evidence"]:
        row = stored[e["chunk_id"]]
        assert e["document_key"] == row.document_key
        assert e["page"] == row.page_number
        assert row.evidence_eligibility in ("primary", "supporting")  # never context_only
        assert row.chunk_role in ("single", "child")  # retrievable units, never a parent
        content = norm(row.content)
        for quote in e["quotes"]:
            assert norm(quote) in content, (case["id"], quote)


@pytest.mark.parametrize(("split", "case"), CASES, ids=[c["id"] for _, c in CASES])
def test_database_checkable_tags_agree_with_the_stored_chunks(
    split: str, case: dict[str, Any], stored: Any
) -> None:
    rows = [stored[e["chunk_id"]] for e in case["evidence"]]
    tags = set(case["perturbations"])
    assert ("ocr_uncertain" in tags) == any(r.text_reliability == "ocr_uncertain" for r in rows)
    assert ("source_status" in tags) == any(r.source_status == "dated" for r in rows)
    assert ("form_only" in tags) == all(r.document_type == "form" for r in rows)
    if case["claim_type"] == "form_request":
        assert all(r.document_type == "form" for r in rows)
    if case["expected_obligation"]:  # rule 10: only eligible primary text may back MANDATORY
        for r in rows:
            assert r.evidence_eligibility == "primary" and r.source_status == "active"
            assert r.text_reliability != "ocr_uncertain" and r.can_support_mandatory


def test_coverage_spans_every_evidence_role_and_reliability(stored: Any) -> None:
    for split, rows in SPLITS.items():
        cited = [stored[e["chunk_id"]] for r in rows for e in r["evidence"]]
        assert {"regulatory", "policy", "form"} <= {c.document_type for c in cited}, split
        assert {"active", "dated", "unknown"} <= {c.source_status for c in cited}, split
        assert {"native", "ocr_uncertain"} <= {c.text_reliability for c in cited}, split
