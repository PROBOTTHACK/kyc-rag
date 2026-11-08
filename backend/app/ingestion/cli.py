"""Ingestion CLI. Stage F folds `ingest` into `run` / `make ingest`.

python -m app.ingestion.cli validate-manifest [--manifest PATH] [--corpus-dir PATH]
python -m app.ingestion.cli check-ocr
python -m app.ingestion.cli index [--document KEY]... [--dry-run] [--force] [--prune]
python -m app.ingestion.cli ingest [--document KEY]... [--force]
python -m app.ingestion.cli reconcile [--repair] [--delete-unknown]
python -m app.ingestion.cli runs [--document KEY] [--failed] [--limit N]

`index` (Stage D) indexes Qdrant straight from memory. `ingest` (Stage E) is the safe
path: Postgres first, then Qdrant from the committed rows, with every stage recorded in
`ingestion_runs`. `reconcile` compares Postgres and Qdrant in both directions and writes
nothing unless `--repair` is given.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.repository import family_integrity
from app.db.runs import recent_runs
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.ingestion.manifest import ManifestError, load_manifest, validate_corpus
from app.ingestion.ocr import SMOKE_TEXT, OcrSetupError, ocr_smoke_test, tesseract_setup
from app.ingestion.pipeline import IngestOutcome, ingest_document
from app.ingestion.prepare import ocr_setup, prepare_document
from app.retrieval.embeddings import Embedder, embedder_from_settings
from app.retrieval.indexing import find_orphans, index_document, prune_orphans
from app.retrieval.qdrant_store import ChunkStore, store_from_settings
from app.retrieval.reconcile import ReconcileReport, reconcile, repair
from app.schemas.corpus import DocumentMetadata

MAX_LISTED = 10  # issue ids printed per category


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


def _index(
    settings: Settings,
    documents: list[str],
    *,
    dry_run: bool,
    force: bool,
    prune: bool,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
) -> int:
    """Stages A-D into Qdrant. `embedder`/`store` are injectable for tests."""
    try:
        entries = load_manifest(settings.CORPUS_MANIFEST)
    except ManifestError as exc:
        print("INVALID manifest:", *exc.issues, sep="\n  ")
        return 2
    corpus = validate_corpus(entries, settings.CORPUS_DIR)
    if not corpus.ok:
        print("INVALID corpus (nothing indexed):", *corpus.errors, sep="\n  ")
        return 2
    keys = {e.document_key for e in entries}
    unknown = sorted(set(documents) - keys)
    if unknown:
        print(f"unknown document key(s): {', '.join(unknown)}")
        return 2
    selected = [e for e in entries if not documents or e.document_key in documents]

    setup, setup_error = ocr_setup(settings)
    if not dry_run:
        embedder = embedder or embedder_from_settings(settings)
        store = store or store_from_settings(settings)
        created = store.ensure_collection(embedder.dimension)
        print(f"collection {store.collection}: {'created' if created else 'exists'}")

    failures = 0
    print(f"  {'document':<38} {'chunks':>6} {'eligible':>8} {'embedded':>8} "
          f"{'payload':>7} {'same':>5} {'stale':>5}")  # fmt: skip
    for entry in selected:
        try:
            result = prepare_document(entry, settings, setup=setup, setup_error=setup_error)
            if dry_run or embedder is None or store is None:
                eligible = sum(c.embeddable for c in result.chunks)
                print(
                    f"  {entry.document_key:<38} {len(result.chunks):>6} {eligible:>8}   (dry run)"
                )
                continue
            r = index_document(result, embedder, store, force=force)
        except Exception as exc:  # report and continue: existing vectors stay as they were
            failures += 1
            print(f"  {entry.document_key:<38} FAILED: {type(exc).__name__}: {exc}")
            continue
        print(f"  {r.document_key:<38} {r.chunks:>6} {r.eligible:>8} {r.embedded:>8} "
              f"{r.payload_updated:>7} {r.unchanged:>5} {r.stale_deleted:>5}")  # fmt: skip

    if not dry_run and store is not None:
        orphans = find_orphans(store, keys)
        for key, n in sorted(orphans.items()):
            print(f"  orphan: {key} ({n} points) is not in the manifest")
        if orphans and prune:
            print(f"  pruned {prune_orphans(store, orphans)} orphan point(s)")
        elif orphans:
            print("  (run with --prune to delete orphaned points)")
        print(f"collection {store.collection}: {store.count()} points")
    return 1 if failures else 0


def _selected_entries(
    settings: Settings, documents: list[str]
) -> tuple[list[DocumentMetadata], set[str]] | None:
    """Manifest entries to process, or None (after printing why) if invalid."""
    try:
        entries = load_manifest(settings.CORPUS_MANIFEST)
    except ManifestError as exc:
        print("INVALID manifest:", *exc.issues, sep="\n  ")
        return None
    corpus = validate_corpus(entries, settings.CORPUS_DIR)
    if not corpus.ok:
        print("INVALID corpus (nothing ingested):", *corpus.errors, sep="\n  ")
        return None
    keys = {e.document_key for e in entries}
    unknown = sorted(set(documents) - keys)
    if unknown:
        print(f"unknown document key(s): {', '.join(unknown)}")
        return None
    return [e for e in entries if not documents or e.document_key in documents], keys


def _print_outcome(o: IngestOutcome) -> None:
    p, i = o.persist, o.index
    if o.failed or p is None or i is None:
        print(f"  {o.document_key:<38} FAILED at {o.failed_stage}: {o.error}")
        if o.run_id:
            print(f"  {'':<38} recorded as run {o.run_id}")
    else:
        print(f"  {o.document_key:<38} {o.status.value:<9} {p.pages:>5} {p.chunks:>6} "
              f"{p.parents:>4} {p.chunks_inserted:>4}/{p.chunks_updated}/{p.chunks_deleted:<3} "
              f"{i.eligible:>8} {i.embedded:>5} {i.stale_deleted:>5}")  # fmt: skip
    for w in o.warnings:
        print(f"  {'':<38} warning: {w}")


def _print_reconcile(r: ReconcileReport) -> None:
    print(
        f"postgres: {r.postgres_documents} documents, {r.postgres_chunks} chunks, "
        f"{r.postgres_eligible} embedding-eligible; qdrant: {r.qdrant_points} points"
    )
    for label, issues in (
        ("eligible Postgres chunks missing from Qdrant", r.missing),
        ("Qdrant points with no Postgres chunk", r.no_postgres_chunk),
        ("Qdrant points of chunks that are not embedding-eligible", r.not_eligible),
        ("Qdrant points out of date (payload/input/model)", r.outdated),
    ):
        print(f"  {label}: {len(issues)}")
        for issue in issues[:MAX_LISTED]:
            extra = f" ({issue.reason})" if issue.reason else ""
            print(f"    {issue.point_id}  {issue.document_key}{extra}")
        if len(issues) > MAX_LISTED:
            print(f"    ... {len(issues) - MAX_LISTED} more")
    for key, status in sorted(r.unfinished_documents.items()):
        print(f"  document not fully ingested: {key} ({status})")
    print("CONSISTENT" if r.consistent else "INCONSISTENT")


def _sessions(settings: Settings) -> sessionmaker[Session]:
    return create_sync_sessionmaker(create_sync_engine(settings))


def _ingest(
    settings: Settings,
    documents: list[str],
    *,
    force: bool,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """Stages A-C, then Postgres, then Qdrant from Postgres, then reconciliation."""
    selected = _selected_entries(settings, documents)
    if selected is None:
        return 2
    entries, _ = selected
    sessions = sessions or _sessions(settings)
    embedder = embedder or embedder_from_settings(settings)
    store = store or store_from_settings(settings)
    setup, setup_error = ocr_setup(settings)
    if setup_error:
        print(f"warning: OCR unavailable ({setup_error}); scanned pages will fail")
    try:
        created = store.ensure_collection(embedder.dimension)
    except Exception as exc:
        print(f"Qdrant not ready (nothing ingested): {type(exc).__name__}: {exc}")
        return 2
    print(f"collection {store.collection}: {'created' if created else 'exists'}")

    print(f"  {'document':<38} {'status':<9} {'pages':>5} {'chunks':>6} {'par':>4} "
          f"{'ins/upd/del':<11} {'eligible':>8} {'embed':>5} {'stale':>5}")  # fmt: skip
    failed = 0
    for entry in entries:
        outcome = ingest_document(
            entry, settings, sessions=sessions, embedder=embedder, store=store,
            setup=setup, setup_error=setup_error, force=force,
        )  # fmt: skip
        _print_outcome(outcome)
        failed += outcome.failed

    with sessions() as s:
        families = family_integrity(s)
        report = reconcile(s, store, embedder)
    print(f"parent/child families: {families.parents} parents, {families.children} children, "
          f"{'intact' if families.ok else 'PROBLEMS'}")  # fmt: skip
    for problem in families.problems[:MAX_LISTED]:
        print(f"  {problem}")
    _print_reconcile(report)
    if failed:
        print(f"{failed} document(s) failed; see `runs --failed`. Re-run `ingest` "
              "(idempotent) or `reconcile --repair` once the cause is fixed.")  # fmt: skip
    return 1 if failed or not families.ok or not report.consistent else 0


def _reconcile(
    settings: Settings,
    *,
    do_repair: bool,
    delete_unknown: bool,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    sessions = sessions or _sessions(settings)
    embedder = embedder or embedder_from_settings(settings)  # the model loads only to embed
    store = store or store_from_settings(settings)
    with sessions() as s:
        report = reconcile(s, store, embedder)
    _print_reconcile(report)
    if not do_repair:
        if not report.consistent:
            print("(diagnostic only: nothing changed; use --repair to re-index from Postgres)")
        return 0 if report.consistent else 1
    fixed = repair(sessions, store, embedder, report, delete_unknown=delete_unknown)
    for key, r in sorted(fixed.reindexed.items()):
        print(f"  reindexed {key}: embedded {r.embedded}, payload {r.payload_updated}, "
              f"stale deleted {r.stale_deleted}")  # fmt: skip
    for key, err in sorted(fixed.failed.items()):
        print(f"  repair FAILED for {key}: {err}")
    if fixed.unknown_deleted:
        print(f"  deleted {fixed.unknown_deleted} point(s) of documents unknown to Postgres")
    if fixed.unknown_kept:
        print(f"  kept {fixed.unknown_kept} point(s) of documents unknown to Postgres "
              "(use --delete-unknown)")  # fmt: skip
    with sessions() as s:
        after = reconcile(s, store, embedder)
    print("after repair:")
    _print_reconcile(after)
    return 0 if after.consistent and not fixed.failed else 1


def _runs(
    settings: Settings,
    document: str | None,
    failed: bool,
    limit: int,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    with (sessions or _sessions(settings))() as s:
        rows = recent_runs(s, document_key=document, failed_only=failed, limit=limit)
        for r in rows:
            when = r.started_at.strftime("%Y-%m-%d %H:%M:%S")
            stage = f" at {r.failed_stage}" if r.failed_stage else ""
            print(f"{when}  {r.document_key:<38} {r.status:<10}{stage}  run {r.id}")
            if r.error:
                print(f"    error: {r.error}")
    if not rows:
        print("no ingestion runs recorded")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m app.ingestion.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    vm = sub.add_parser("validate-manifest", help="check manifest.yaml against the corpus")
    vm.add_argument("--manifest", type=Path, default=settings.CORPUS_MANIFEST)
    vm.add_argument("--corpus-dir", type=Path, default=settings.CORPUS_DIR)
    sub.add_parser("check-ocr", help="locate Tesseract and run a real OCR smoke test")
    ix = sub.add_parser("index", help="extract, chunk, embed and index into Qdrant")
    ix.add_argument("--document", action="append", default=[], metavar="KEY",
                    help="index only this document_key (repeatable)")  # fmt: skip
    ix.add_argument("--dry-run", action="store_true", help="chunk and count; no model, no writes")
    ix.add_argument("--force", action="store_true", help="re-embed every eligible chunk")
    ix.add_argument("--prune", action="store_true", help="delete points of unknown documents")
    ig = sub.add_parser("ingest", help="extract, chunk, persist to Postgres, index Qdrant")
    ig.add_argument("--document", action="append", default=[], metavar="KEY",
                    help="ingest only this document_key (repeatable)")  # fmt: skip
    ig.add_argument("--force", action="store_true", help="re-embed every eligible chunk")
    rc = sub.add_parser("reconcile", help="compare Postgres and Qdrant (read-only by default)")
    rc.add_argument("--repair", action="store_true",
                    help="re-index affected documents from Postgres")  # fmt: skip
    rc.add_argument("--delete-unknown", action="store_true",
                    help="with --repair: delete points of documents not in Postgres")  # fmt: skip
    rn = sub.add_parser("runs", help="show recorded ingestion runs")
    rn.add_argument("--document", metavar="KEY")
    rn.add_argument("--failed", action="store_true", help="only failed runs")
    rn.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.command == "validate-manifest":
        return _validate_manifest(args.manifest, args.corpus_dir)
    if args.command == "check-ocr":
        return _check_ocr(settings)
    if args.command == "index":
        return _index(
            settings, args.document, dry_run=args.dry_run, force=args.force, prune=args.prune
        )
    if args.command == "ingest":
        return _ingest(settings, args.document, force=args.force)
    if args.command == "reconcile":
        if args.delete_unknown and not args.repair:
            parser.error("--delete-unknown requires --repair")
        return _reconcile(settings, do_repair=args.repair, delete_unknown=args.delete_unknown)
    if args.command == "runs":
        return _runs(settings, args.document, args.failed, args.limit)
    return 2  # unreachable: argparse enforces a known command


if __name__ == "__main__":
    sys.exit(main())
