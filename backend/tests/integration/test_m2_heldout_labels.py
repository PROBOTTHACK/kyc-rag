"""M2 held-out labels against the real corpus (Stages A-C from the source PDFs).

Every scoring target matches exactly one eligible chunk (production eligibility filter
plus the case's entity filter), and it is the chunk named after `SCORING:` in the notes.
Every non-scoring alternative chunk exists, is eligible, and can never satisfy a scoring
target, so it cannot count toward Recall@5 / Hit@5 / MRR@5. No retrieval is run.
"""

import re
from typing import Any

import pytest

from app.config import REPO_ROOT, get_settings
from app.ingestion.manifest import load_manifest
from app.ingestion.prepare import ocr_setup, prepare_document
from app.retrieval.evaluation import load_cases
from app.schemas.taxonomy import expand_entity_terms
from tests.unit.test_m2_heldout import ALT_MARKER, HELDOUT, UUID, alternative_chunk_ids

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (REPO_ROOT / "data" / "raw_pdfs" / "regulatory" / "169MD.pdf").exists(),
        reason="source PDFs not present",
    ),
]
SEARCHED = {"primary", "supporting"}  # SearchFilters() default


@pytest.fixture(scope="module")
def eligible() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    setup, err = ocr_setup(settings)
    return {
        c.metadata.chunk_id: c.metadata.to_qdrant_payload()
        for e in load_manifest(settings.CORPUS_MANIFEST)
        for c in prepare_document(e, settings, setup=setup, setup_error=err).chunks
        if c.embeddable and c.metadata.evidence_eligibility.value in SEARCHED
    }


def _scoring_ids(notes: str | None) -> set[str]:
    head = (notes or "").partition(ALT_MARKER)[0]
    return set(UUID.findall(head.partition("SCORING:")[2] or head))


def test_every_scoring_target_maps_to_exactly_one_eligible_chunk(
    eligible: dict[str, dict[str, Any]],
) -> None:
    problems: list[tuple[str, str, object]] = []
    for case in load_cases(HELDOUT):
        types = {t.value for t in expand_entity_terms(case.entity_terms)}
        matched: set[str] = set()
        for t in case.targets:
            hits = [
                cid for cid, p in eligible.items()
                if t.matches(p) and (not types or types & set(p["entity_types"]))
            ]  # fmt: skip
            if len(hits) != 1:
                problems.append((case.id, t.label(), len(hits)))
            matched |= set(hits)
        expected = _scoring_ids(case.notes)
        if case.id == "m2h-15":  # its notes also cite the m1-04 chunk, for context
            expected = {i for i in expected if not i.startswith("a50d12d1")}
        if matched != expected:
            problems.append((case.id, "scoring ids", sorted(matched ^ expected)))
    assert problems == []


def test_alternatives_exist_are_eligible_and_never_score(
    eligible: dict[str, dict[str, Any]],
) -> None:
    seen = 0
    for case in load_cases(HELDOUT):
        for cid in alternative_chunk_ids(case.notes):
            seen += 1
            assert cid in eligible, (case.id, cid)
            assert not any(t.matches(eligible[cid]) for t in case.targets), (case.id, cid)
    assert seen == 3


def test_scoring_ids_parse_ignores_alternatives() -> None:
    notes = "SCORING: chunk " + "1" * 8 + "-1111-1111-1111-" + "1" * 12 + ". " + ALT_MARKER + (
        " chunk " + "2" * 8 + "-2222-2222-2222-" + "2" * 12)  # fmt: skip
    assert _scoring_ids(notes) == {"11111111-1111-1111-1111-111111111111"}
    assert re.fullmatch(UUID, next(iter(alternative_chunk_ids(notes))))
