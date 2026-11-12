"""M4 S3: claim-verification datasets: structure, balance, coverage, leakage and the
held-out freeze (docs/milestones/M4_LABELLING_GUIDELINES.md).

No database and no model here. Provenance against Postgres is checked in
tests/integration/test_m4_dataset_labels.py. The held-out set is only ever used to report
an already-selected configuration (S8): any edit fails `test_heldout_file_is_frozen`.
"""

import hashlib
import json
import re
from collections import Counter
from typing import Any

import pytest

from app.config import REPO_ROOT
from app.graph.state import ClaimType
from app.schemas.taxonomy import EntityType

DEV = REPO_ROOT / "data" / "eval" / "m4_claims_dev.jsonl"
DEV_LEDGER = REPO_ROOT / "data" / "eval" / "m4_claims_dev_review.jsonl"
HELDOUT_LEDGER = REPO_ROOT / "data" / "eval" / "m4_claims_heldout_review.jsonl"
HELDOUT = REPO_ROOT / "data" / "eval" / "m4_claims_heldout.jsonl"
# SHA-256 of the committed held-out file (LF line endings). Also recorded in docs/10.
HELDOUT_SHA256 = "eee0a575319d7df4bc3d8f5c0771a89e4c8bf9f68abe36eb56fcac3275d90203"

GOLD = {"entailed", "contradicted", "insufficient"}
PERTURBATIONS = {
    "paraphrase", "verbatim_fact", "negation", "polarity_flip", "modality_change",
    "number_change", "acronym_variant", "entity_swap", "subject_swap", "scope_widening",
    "condition_dropped", "document_swap", "form_only", "entity_group", "ocr_uncertain",
    "source_status",
}  # fmt: skip
# The categories the owner required in BOTH splits (S3 brief).
REQUIRED_COVERAGE = {
    "negation", "polarity_flip", "number_change", "acronym_variant", "entity_swap",
    "scope_widening", "form_only", "entity_group", "ocr_uncertain", "source_status",
    "modality_change",  # dev added after the owner review (2026-11-12)
}  # fmt: skip
FIELDS = {
    "id", "split", "claim", "claim_type", "entity", "match_scope", "evidence", "gold",
    "expected_obligation", "perturbations", "rationale", "annotator", "owner_review",
    "owner_note", "second_review", "label_version",
}  # fmt: skip
MIN_PER_CLASS = 15
NEAR_DUPLICATE_JACCARD = 0.8


def load(path: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


SPLITS = {"dev": load(DEV), "heldout": load(HELDOUT)}


def test_heldout_file_is_frozen() -> None:
    data = HELDOUT.read_bytes().replace(b"\r\n", b"\n")  # a Windows checkout hashes the same
    assert hashlib.sha256(data).hexdigest() == HELDOUT_SHA256, (
        "m4_claims_heldout.jsonl changed: the held-out labels are frozen "
        "(M4_LABELLING_GUIDELINES §6); corrections go into a new versioned file"
    )


@pytest.mark.parametrize("path", [DEV, HELDOUT], ids=["dev", "heldout"])
def test_files_are_canonical_jsonl(path: Any) -> None:
    """One sorted-key JSON object per line, UTF-8, LF: reproducible byte for byte."""
    text = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    canonical = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    assert text == canonical


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_every_case_is_well_formed(split: str) -> None:
    prefix = {"dev": "m4d", "heldout": "m4h"}[split]
    rows = SPLITS[split]
    assert [r["id"] for r in rows] == [f"{prefix}-{i:03d}" for i in range(1, len(rows) + 1)]
    for r in rows:
        assert set(r) == FIELDS, r["id"]
        assert r["split"] == split and r["gold"] in GOLD
        ClaimType(r["claim_type"])
        assert r["entity"] is None or EntityType(r["entity"])
        assert r["match_scope"] in ("entity", "entity_group")
        assert 10 <= len(r["claim"]) <= 300 and r["rationale"]
        assert r["perturbations"] and set(r["perturbations"]) <= PERTURBATIONS
        assert len(set(r["perturbations"])) == len(r["perturbations"])
        assert r["evidence"]
        for e in r["evidence"]:
            assert set(e) == {"chunk_id", "document_key", "page", "quotes"}
            assert re.fullmatch(r"[0-9a-f-]{36}", e["chunk_id"]) and e["page"] >= 1
            assert e["quotes"] and all(q.strip() for q in e["quotes"])
        # annotation provenance (M4_LABELLING_GUIDELINES §7-§8)
        assert r["annotator"] == "implementing_assistant" and r["label_version"] == 1
        assert r["owner_review"] in ("pending", "accepted", "changed")
        if split == "heldout":
            # the frozen file is never edited: owner verdicts go to the review ledger
            assert r["owner_review"] == "pending" and r["owner_note"] is None
        assert r["second_review"] is None or {"reviewer", "verdict"} <= set(r["second_review"])


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_classes_are_balanced_and_large_enough(split: str) -> None:
    counts = Counter(r["gold"] for r in SPLITS[split])
    assert set(counts) == GOLD and min(counts.values()) >= MIN_PER_CLASS, counts


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_required_perturbations_are_covered_in_each_split(split: str) -> None:
    seen = {t for r in SPLITS[split] for t in r["perturbations"]}
    assert seen >= REQUIRED_COVERAGE, REQUIRED_COVERAGE - seen


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_expected_obligation_follows_rule_10(split: str) -> None:
    """Only an entailed policy obligation on entity-scope evidence may expect MANDATORY.
    Eligibility, status and OCR are checked against Postgres in the integration test."""
    for r in SPLITS[split]:
        if r["expected_obligation"]:
            assert r["gold"] == "entailed", r["id"]
            assert r["claim_type"] == "policy_obligation", r["id"]
            assert r["match_scope"] == "entity", r["id"]
            assert not {"form_only", "ocr_uncertain", "source_status", "entity_group"} & set(
                r["perturbations"]
            ), r["id"]
        if r["claim_type"] == "form_request" or "form_only" in r["perturbations"]:
            assert not r["expected_obligation"], r["id"]


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_every_class_has_obligation_and_non_obligation_claims(split: str) -> None:
    rows = SPLITS[split]
    for gold in GOLD:
        types = {r["claim_type"] for r in rows if r["gold"] == gold}
        assert "policy_obligation" in types and len(types) >= 3, (gold, types)
    assert any(r["expected_obligation"] for r in rows)


# --- leakage ----------------------------------------------------------------------------------


def _chunks(rows: list[dict[str, Any]]) -> set[str]:
    return {e["chunk_id"] for r in rows for e in r["evidence"]}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def test_no_evidence_chunk_is_shared_between_splits() -> None:
    shared = _chunks(SPLITS["dev"]) & _chunks(SPLITS["heldout"])
    assert not shared, sorted(shared)


def test_no_claim_is_duplicated_or_near_duplicated_across_splits() -> None:
    worst = 0.0
    for a in SPLITS["dev"]:
        ta = _tokens(a["claim"])
        for b in SPLITS["heldout"]:
            tb = _tokens(b["claim"])
            jaccard = len(ta & tb) / len(ta | tb)
            worst = max(worst, jaccard)
            assert jaccard < NEAR_DUPLICATE_JACCARD, (a["id"], b["id"], jaccard)
    assert worst < NEAR_DUPLICATE_JACCARD


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_no_duplicate_cases_within_a_split(split: str) -> None:
    keys = [
        (r["claim"].lower(), tuple(e["chunk_id"] for e in r["evidence"])) for r in SPLITS[split]
    ]
    assert len(keys) == len(set(keys))
    claims = [r["claim"].lower() for r in SPLITS[split]]
    assert len(claims) == len(set(claims))


# --- owner review (2026-11-12) ---------------------------------------------------------------


def test_dev_rule_10_entity_null_never_expects_an_obligation() -> None:
    """Revised rule 10 applies to the dev set. The frozen held-out set was labelled under
    the earlier wording; its affected cases are documented, not edited."""
    for r in SPLITS["dev"]:
        if r["entity"] is None:
            assert not r["expected_obligation"], r["id"]


def test_the_dev_ledger_matches_the_dataset() -> None:
    ledger = load(DEV_LEDGER)
    rows = {r["id"]: r for r in SPLITS["dev"]}
    assert [e["entry"] for e in ledger] == [f"D{i:02d}" for i in range(1, len(ledger) + 1)]
    touched = set()
    for e in ledger:
        assert e["split"] == "dev" and e["owner_words"]
        assert e["scope"] in ("case", "addition", "acceptance", "guideline")
        if e["scope"] == "guideline":
            assert e["case_id"] is None
            continue
        r = rows[e["case_id"]]
        touched.add(r["id"])
        assert e["validated_against_postgres"] is True
        if e["scope"] == "case":
            assert r["owner_review"] == "changed" and r["owner_note"]
            for field, value in e["applied"].items():  # the last entry per field wins
                later = [
                    x for x in ledger if x["case_id"] == r["id"] and field in (x["applied"] or {})
                ]
                if later[-1] is e:
                    assert r[field] == value, (r["id"], field)
        elif e["scope"] == "acceptance":
            # accepted with the existing label; explicitly NOT an independent re-annotation
            assert e["independently_reannotated"] is False
            assert r["owner_review"] == "accepted" and r["gold"] == e["applied"]["gold"]
        else:
            assert r["owner_review"] == "accepted" and r["gold"] == e["applied"]["gold"]
    reviewed = {r["id"] for r in SPLITS["dev"] if r["owner_review"] != "pending"}
    assert reviewed == touched  # nothing reviewed outside the ledger
    assert all(r["owner_review"] != "pending" for r in SPLITS["dev"])  # owner review complete
    assert {e["decision"] for e in ledger if e["scope"] == "guideline"} >= {
        "rule_1",
        "rule_3",
        "rule_10",
    }


def test_the_cross_source_conflict_pair_is_kept_and_unresolved() -> None:
    rows = {r["id"]: r for r in SPLITS["dev"]}
    hdfc, rbi = rows["m4d-032"], rows["m4d-013"]
    assert hdfc["evidence"][0]["document_key"] == "hdfc_kyc_aml_faq"
    assert rbi["evidence"][0]["document_key"] == "rbi_kyc_faqs"
    assert "source_status" in hdfc["perturbations"] and "source_status" in rbi["perturbations"]
    assert not hdfc["expected_obligation"] and not rbi["expected_obligation"]


def test_heldout_issues_are_recorded_for_a_versioned_s8_correction_only() -> None:
    """The issues the revised rules raise in the held-out set are logged, never applied:
    the frozen file and its hash stay unchanged through S3/S4."""
    issues = load(HELDOUT_LEDGER)
    held = {r["id"]: r for r in SPLITS["heldout"]}
    assert {i["case_id"] for i in issues} == {
        "m4h-007", "m4h-008", "m4h-009", "m4h-010", "m4h-011", "m4h-022",
    }  # fmt: skip
    for i in issues:
        assert i["split"] == "heldout" and i["frozen_sha256"] == HELDOUT_SHA256
        assert i["status"] == "deferred_to_S8_versioned_correction"
        current = held[i["case_id"]]
        assert i["current"]["gold"] == current["gold"]  # recorded, not applied
        assert i["current"]["expected_obligation"] == current["expected_obligation"]
