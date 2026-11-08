"""Load and validate `data/raw_pdfs/manifest.yaml` against the files on disk.

Per-entry rules live in `DocumentMetadata`; this module adds corpus-level rules:
uniqueness, file presence, SHA-256 match, unlisted PDFs, and `supersedes` consistency.
A SHA mismatch is an error, not a new version: a changed file needs a human to set up
its identity and version relationship (docs/milestones/M1_PLAN.md, refinement C).
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.schemas.corpus import DocumentMetadata
from app.schemas.taxonomy import SourceStatus

_CHUNK = 1 << 20


class ManifestError(Exception):
    """The manifest cannot be parsed into valid entries."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


@dataclass
class CorpusReport:
    entries: list[DocumentMetadata]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(_CHUNK):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path) -> list[DocumentMetadata]:
    """Parse every entry; collect all problems before failing."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError([f"cannot read manifest: {type(exc).__name__}: {exc}"]) from exc
    if not isinstance(raw, list) or not raw:
        raise ManifestError(["manifest must be a non-empty YAML list of documents"])

    entries: list[DocumentMetadata] = []
    issues: list[str] = []
    for i, item in enumerate(raw):
        label = item.get("document_key", f"#{i}") if isinstance(item, dict) else f"#{i}"
        try:
            entries.append(DocumentMetadata.model_validate(item))
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "<entry>"
                issues.append(f"{label}: {loc}: {err['msg']}")
    if issues:
        raise ManifestError(issues)
    return entries


def _duplicates(values: list[str]) -> set[str]:
    return {v for v, n in Counter(values).items() if n > 1}


def validate_corpus(entries: list[DocumentMetadata], corpus_dir: Path) -> CorpusReport:
    report = CorpusReport(entries=entries)
    root = corpus_dir.resolve()

    for name, values in (
        ("document_key", [e.document_key for e in entries]),
        ("file", [e.file for e in entries]),
        ("sha256", [e.sha256 for e in entries]),
    ):
        for dup in sorted(_duplicates(values)):
            report.errors.append(f"duplicate {name}: {dup}")

    for e in entries:
        path = (root / e.file).resolve()
        if not path.is_relative_to(root):
            report.errors.append(f"{e.document_key}: file escapes corpus dir: {e.file}")
            continue
        if not path.is_file():
            report.errors.append(f"{e.document_key}: file not found: {e.file}")
            continue
        actual = sha256_file(path)
        if actual != e.sha256:
            report.errors.append(
                f"{e.document_key}: sha256 mismatch for {e.file} (manifest {e.sha256[:12]}…, "
                f"disk {actual[:12]}…). Content changed: establish identity/version "
                "relationship and update the manifest; it is not treated as a new version."
            )

    listed = {(root / e.file).resolve() for e in entries}
    for pdf in sorted(root.rglob("*")):
        if pdf.is_file() and pdf.suffix.lower() == ".pdf" and pdf.resolve() not in listed:
            report.warnings.append(
                f"PDF not in manifest (will not be ingested): {pdf.relative_to(root).as_posix()}"
            )

    by_key = {e.document_key: e for e in entries}
    superseded_by: dict[str, list[str]] = {}
    for e in entries:
        for target in e.supersedes:
            if target not in by_key:
                report.errors.append(f"{e.document_key}: supersedes unknown document {target}")
                continue
            superseded_by.setdefault(target, []).append(e.document_key)
            if by_key[target].source_status is not SourceStatus.SUPERSEDED:
                report.errors.append(
                    f"{e.document_key}: supersedes {target}, but {target} is "
                    f"{by_key[target].source_status.value}, not superseded"
                )
    for e in entries:
        if e.source_status is SourceStatus.SUPERSEDED and e.document_key not in superseded_by:
            report.errors.append(
                f"{e.document_key}: marked superseded but no document in the corpus supersedes it"
            )
    return report
