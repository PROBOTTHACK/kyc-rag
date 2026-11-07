"""Manifest loading/validation on a synthetic corpus, plus invariants of the real manifest."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import REPO_ROOT
from app.ingestion.cli import main
from app.ingestion.manifest import ManifestError, load_manifest, validate_corpus
from app.schemas.taxonomy import DocumentType, EvidenceEligibility, SourceStatus

REAL_MANIFEST = REPO_ROOT / "data" / "raw_pdfs" / "manifest.yaml"


def make_pdf(root: Path, rel: str, payload: bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% synthetic test file\n" + payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entry(key: str, rel: str, sha: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "document_key": key,
        "file": rel,
        "sha256": sha,
        "bank": "HDFC",
        "document_type": "form",
        "source_authority": "HDFC",
        "attribution_basis": "document_text",
        "title": f"Synthetic {key}",
        "source_status": "active",
        "entity_types": ["INDIVIDUAL"],
    }
    base.update(overrides)
    return base


def write_manifest(root: Path, entries: list[dict[str, Any]]) -> Path:
    path = root / "manifest.yaml"
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, list[dict[str, Any]]]:
    a = make_pdf(tmp_path, "forms/a.pdf", b"a")
    b = make_pdf(tmp_path, "forms/b.pdf", b"b")
    return tmp_path, [entry("doc_a", "forms/a.pdf", a), entry("doc_b", "forms/b.pdf", b)]


def check(root: Path, entries: list[dict[str, Any]]) -> Any:
    return validate_corpus(load_manifest(write_manifest(root, entries)), root)


def test_valid_corpus(corpus: tuple[Path, list[dict[str, Any]]]) -> None:
    root, entries = corpus
    report = check(root, entries)
    assert report.ok, report.errors
    assert report.warnings == []


def test_changed_file_is_flagged_not_versioned(corpus: tuple[Path, list[dict[str, Any]]]) -> None:
    root, entries = corpus
    (root / "forms/a.pdf").write_bytes(b"%PDF-1.4\n% edited\n")
    report = check(root, entries)
    assert not report.ok
    assert any(
        "sha256 mismatch" in e and "not treated as a new version" in e for e in report.errors
    )
    # Nothing was silently re-labelled.
    assert all(e.source_status is SourceStatus.ACTIVE for e in report.entries)


def test_missing_file(corpus: tuple[Path, list[dict[str, Any]]]) -> None:
    root, entries = corpus
    (root / "forms/b.pdf").unlink()
    assert any("file not found" in e for e in check(root, entries).errors)


def test_duplicates(corpus: tuple[Path, list[dict[str, Any]]]) -> None:
    root, entries = corpus
    dup = entry("doc_a", "forms/b.pdf", entries[1]["sha256"])
    errors = check(root, [entries[0], entries[1], dup]).errors
    assert "duplicate document_key: doc_a" in errors
    assert "duplicate file: forms/b.pdf" in errors
    assert f"duplicate sha256: {entries[1]['sha256']}" in errors


def test_unlisted_pdf_is_reported(corpus: tuple[Path, list[dict[str, Any]]]) -> None:
    root, entries = corpus
    make_pdf(root, "forms/extra.pdf", b"x")
    report = check(root, entries)
    assert report.ok
    assert report.warnings == ["PDF not in manifest (will not be ingested): forms/extra.pdf"]


def test_supersedes_must_be_explicit_and_consistent(
    corpus: tuple[Path, list[dict[str, Any]]],
) -> None:
    root, (a, b) = corpus
    old = {**a, "source_status": "superseded", "status_reason": "replaced by doc_b"}

    # superseded without any superseder
    assert any("no document in the corpus supersedes it" in e for e in check(root, [old, b]).errors)
    # supersedes a document not marked superseded
    errors = check(root, [a, {**b, "supersedes": ["doc_a"]}]).errors
    assert any("is active, not superseded" in e for e in errors)
    # unknown target
    errors = check(root, [a, {**b, "supersedes": ["ghost_doc"]}]).errors
    assert any("unknown document ghost_doc" in e for e in errors)
    # properly linked
    assert check(root, [old, {**b, "supersedes": ["doc_a"]}]).ok


def test_load_collects_all_entry_errors(tmp_path: Path) -> None:
    path = write_manifest(
        tmp_path,
        [
            entry("doc_a", "forms/a.pdf", "0" * 64, bank="SBI"),
            entry("doc_b", "forms/b.pdf", "short"),
        ],
    )
    with pytest.raises(ManifestError) as exc:
        load_manifest(path)
    assert any(i.startswith("doc_a: bank") for i in exc.value.issues)
    assert any(i.startswith("doc_b: sha256") for i in exc.value.issues)


@pytest.mark.parametrize("text", ["", "{a: 1}", "- [unclosed"])
def test_load_rejects_malformed(tmp_path: Path, text: str) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_cli_exit_codes(
    corpus: tuple[Path, list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, entries = corpus
    path = write_manifest(root, entries)
    assert main(["validate-manifest", "--manifest", str(path), "--corpus-dir", str(root)]) == 0
    assert "OK: 2 documents" in capsys.readouterr().out
    (root / "forms/a.pdf").write_bytes(b"changed")
    assert main(["validate-manifest", "--manifest", str(path), "--corpus-dir", str(root)]) == 1
    assert "INVALID" in capsys.readouterr().out


# --- the committed manifest --------------------------------------------------


@pytest.fixture(scope="module")
def real_entries() -> Any:
    return {e.document_key: e for e in load_manifest(REAL_MANIFEST)}


def test_real_manifest_parses(real_entries: Any) -> None:
    assert len(real_entries) == 9


def test_real_manifest_never_invents_effective_dates(real_entries: Any) -> None:
    for e in real_entries.values():
        assert e.effective_from is None and e.effective_until is None, e.document_key


def test_real_manifest_decisions(real_entries: Any) -> None:
    # Approved Phase 1 decisions; changing them needs a reviewed manifest change.
    assert real_entries["rbi_kyc_faqs"].source_status is SourceStatus.DATED
    hdfc_faq = real_entries["hdfc_kyc_aml_faq"]
    assert hdfc_faq.source_authority is None and hdfc_faq.source_status is SourceStatus.DATED
    netbanking = real_entries["hdfc_netbanking_corporate_application"]
    assert netbanking.evidence_eligibility is EvidenceEligibility.CONTEXT_ONLY
    forms = [e for e in real_entries.values() if e.document_type is DocumentType.FORM]
    assert len(forms) == 5
    for form in forms:  # validity unverifiable: unknown, but still usable as supporting evidence
        assert form.source_status is SourceStatus.UNKNOWN, form.document_key
        assert form.status_reason
        assert form.evidence_eligibility is EvidenceEligibility.SUPPORTING
    assert netbanking.source_status is SourceStatus.UNKNOWN
    primary = [k for k, e in real_entries.items() if e.evidence_eligibility.value == "primary"]
    assert primary == ["rbi_kyc_directions_2025"]
    assert all(e.bank.value in ("RBI", "HDFC") for e in real_entries.values())


@pytest.mark.skipif(
    not (REAL_MANIFEST.parent / "regulatory" / "169MD.pdf").exists(),
    reason="source PDFs are gitignored and not present",
)
def test_real_manifest_matches_pdfs_on_disk(real_entries: Any) -> None:
    report = validate_corpus(list(real_entries.values()), REAL_MANIFEST.parent)
    assert report.ok, report.errors
    assert report.warnings == []
