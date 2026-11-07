"""Ingestion CLI. `run` (Pipeline A) is added in M1 Phase 3.

python -m app.ingestion.cli validate-manifest [--manifest PATH] [--corpus-dir PATH]
python -m app.ingestion.cli check-ocr
"""

import argparse
import sys
from pathlib import Path

from app.config import Settings, get_settings
from app.ingestion.manifest import ManifestError, load_manifest, validate_corpus
from app.ingestion.ocr import SMOKE_TEXT, OcrSetupError, ocr_smoke_test, tesseract_setup


def _validate_manifest(manifest: Path, corpus_dir: Path) -> int:
    try:
        entries = load_manifest(manifest)
    except ManifestError as exc:
        print(f"INVALID manifest {manifest}:")
        for issue in exc.issues:
            print(f"  error: {issue}")
        return 1
    report = validate_corpus(entries, corpus_dir)
    for e in report.entries:
        print(
            f"  {e.document_key:<40} {e.bank.value:<5} {e.document_type.value:<10} "
            f"{e.source_status.value:<8} {e.evidence_eligibility.value}"
        )
    for w in report.warnings:
        print(f"  warning: {w}")
    for err in report.errors:
        print(f"  error: {err}")
    verdict = "OK" if report.ok else "INVALID"
    print(
        f"{verdict}: {len(report.entries)} documents, {len(report.errors)} errors, "
        f"{len(report.warnings)} warnings"
    )
    return 0 if report.ok else 1


def _check_ocr(settings: Settings) -> int:
    try:
        setup = tesseract_setup(settings)
        text = ocr_smoke_test(setup)
    except OcrSetupError as exc:
        print(f"OCR NOT READY: {exc}")
        return 1
    print(f"  tesseract: {setup.cmd} ({setup.version})")
    print(f"  tessdata:  {setup.tessdata} (languages: {', '.join(setup.languages)})")
    print(f"  smoke OCR: {text!r}")
    ok = all(word in text.upper() for word in SMOKE_TEXT.split())
    print("OCR OK" if ok else f"OCR NOT READY: expected {SMOKE_TEXT!r}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m app.ingestion.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    vm = sub.add_parser("validate-manifest", help="check manifest.yaml against the corpus")
    vm.add_argument("--manifest", type=Path, default=settings.CORPUS_MANIFEST)
    vm.add_argument("--corpus-dir", type=Path, default=settings.CORPUS_DIR)
    sub.add_parser("check-ocr", help="locate Tesseract and run a real OCR smoke test")
    args = parser.parse_args(argv)

    if args.command == "validate-manifest":
        return _validate_manifest(args.manifest, args.corpus_dir)
    if args.command == "check-ocr":
        return _check_ocr(settings)
    return 2  # unreachable: argparse enforces a known command


if __name__ == "__main__":
    sys.exit(main())
