"""M2 held-out retrieval set: frozen contents and structure (docs/10, M2_PLAN section 6).

The held-out set is used only to report an already-selected configuration, never to
choose or tune one. Its labels are frozen: any edit fails `test_heldout_file_is_frozen`.
A label error is fixed in a new versioned file, never by editing this one.
"""

import hashlib
import re

import yaml

from app.config import REPO_ROOT
from app.retrieval.evaluation import load_cases

HELDOUT = REPO_ROOT / "data" / "eval" / "m2_heldout.jsonl"
DEV = REPO_ROOT / "data" / "eval" / "m1_retrieval.jsonl"
# SHA-256 of the committed file (LF line endings). Also recorded in docs/10.
HELDOUT_SHA256 = "55ba14d9893333b89cc260ac0ece3ac9115ebede28781aa98adfee4d54b0210c"
ALT_MARKER = "ALTERNATIVE (non-scoring):"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def alternative_chunk_ids(notes: str | None) -> set[str]:
    """Reporting-only alternative evidence: chunk ids after the ALTERNATIVE marker."""
    _, found, tail = (notes or "").partition(ALT_MARKER)
    return set(UUID.findall(tail)) if found else set()


def test_heldout_file_is_frozen() -> None:
    # Normalise CRLF so a Windows checkout (core.autocrlf) hashes like the committed blob.
    data = HELDOUT.read_bytes().replace(b"\r\n", b"\n")
    assert (
        hashlib.sha256(data).hexdigest() == HELDOUT_SHA256
    ), "m2_heldout.jsonl changed: the held-out labels are frozen (docs/10)"


def test_heldout_loads_with_the_m1_loader() -> None:
    cases = load_cases(HELDOUT)
    assert len(cases) == 20
    assert sum(len(c.targets) for c in cases) == 26
    assert all(c.id.startswith("m2h-") for c in cases)
    manifest = yaml.safe_load((REPO_ROOT / "data" / "raw_pdfs" / "manifest.yaml").read_text(
        encoding="utf-8"))  # fmt: skip
    keys = {d["document_key"] for d in manifest}
    for case in cases:
        targets = {t.document_key for t in case.targets}
        assert targets <= keys, case.id
        assert "hdfc_netbanking_corporate_application" not in targets, case.id


def test_heldout_is_disjoint_from_the_development_set() -> None:
    dev = load_cases(DEV)
    held = load_cases(HELDOUT)
    assert not {c.id for c in dev} & {c.id for c in held}
    assert not {c.query.lower() for c in dev} & {c.query.lower() for c in held}


def test_alternative_evidence_is_recorded_only_in_notes() -> None:
    alts = {c.id: alternative_chunk_ids(c.notes) for c in load_cases(HELDOUT)}
    assert {cid for cid, ids in alts.items() if ids} == {"m2h-02", "m2h-16", "m2h-17"}
    assert all(len(ids) == 1 for ids in alts.values() if ids)
    # m2h-15 cites the m1-04 chunk for context only; it is not alternative evidence.
    assert alts["m2h-15"] == set()
