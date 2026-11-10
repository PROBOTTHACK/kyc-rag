"""Ingestion CLI (M1). Run from `backend/`: `python -m app.ingestion.cli <command>`.

Unified workflow (Make targets in parentheses):
  run      [--document KEY]... [--path DIR] [--dry-run] [--force]   (make ingest)
           validate manifest + PDFs -> extract -> OCR -> chunk -> Postgres ->
           embed -> Qdrant (from the committed rows) -> integrity + reconciliation.
           `ingest` is an alias.
Independent steps:
  persist  [--document KEY]... [--path DIR] [--dry-run]   A-C + Postgres only
  index    [--document KEY]... [--from-db] [--dry-run] [--force] [--prune]
           --from-db: index Qdrant from Postgres rows (no PDF read; finalises status);
           without it: the Stage D in-memory indexer (A-C -> Qdrant, no Postgres).
Recovery and inspection:
  retry    [--document KEY]... [--force]       re-run what failed, per document
  reconcile [--repair [--delete-unknown]]      Postgres <-> Qdrant (read-only default)
  runs     [--document KEY] [--failed] [--limit N]
Retrieval:
  search   QUERY [--top-k N] [--entity T]... [--bank B]... [--document KEY]...
           [--include-context-only] [--mandatory-only] [--expand] [--mode dense|bm25]
  evaluate [--dataset PATH] [--top-k N] [--report PATH] [--mode dense|bm25]
           Recall@k benchmark (the frozen held-out set is refused; docs/10)
Checks:
  validate-manifest [--manifest PATH] [--corpus-dir PATH]
  check-ocr

Exit codes: 0 ok; 1 a document failed or Postgres/Qdrant are inconsistent;
2 invalid input or a service is not ready (nothing was written).
"""

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.migrations import current_revision, head_revision
from app.db.repository import evidence_context, family_integrity
from app.db.runs import recent_runs
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.ingestion.manifest import ManifestError, load_manifest, validate_corpus
from app.ingestion.ocr import SMOKE_TEXT, OcrSetupError, ocr_smoke_test, tesseract_setup
from app.ingestion.pipeline import (
    DryRunReport,
    IngestOutcome,
    Mode,
    RetryAction,
    dry_run_document,
    ingest_document,
    retry_action,
)
from app.ingestion.prepare import ocr_setup, prepare_document
from app.retrieval.embeddings import Embedder, embedder_from_settings
from app.retrieval.evaluation import (
    DEFAULT_K,
    evaluate,
    evaluate_with,
    load_cases,
    render_markdown,
)
from app.retrieval.indexing import find_orphans, index_document, prune_orphans
from app.retrieval.keyword import TOKENIZER_VERSION, KeywordIndexManager, KeywordIndexUnavailable
from app.retrieval.qdrant_store import ChunkStore, store_from_settings
from app.retrieval.reconcile import ReconcileReport, reconcile, repair
from app.retrieval.search import SearchFilters, SearchHit, search
from app.schemas.corpus import DocumentMetadata

MAX_LISTED = 10  # issue ids printed per category
DEFAULT_DATASET = Path("data/eval/m1_retrieval.jsonl")
# Retrieval modes. `dense` is the M1 path and the default; hybrid arrives in M2 A2.
MODES = ("dense", "bm25")
# The frozen M2 held-out set is used only to report an already-selected configuration
# (docs/10, M2_PLAN section 6); `evaluate` refuses it until that explicit final run.
HELDOUT_DATASET_NAME = "m2_heldout.jsonl"
BM25_TIMED_PASSES = 3


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


# --- shared helpers ---------------------------------------------------------------------------


def _with_corpus_dir(settings: Settings, path: Path | None) -> Settings:
    """`--path DIR`: read PDFs and `manifest.yaml` from DIR instead of CORPUS_DIR."""
    if path is None:
        return settings
    root = path.resolve()
    return settings.model_copy(
        update={"CORPUS_DIR": root, "CORPUS_MANIFEST": root / "manifest.yaml"}
    )


def _selected_entries(
    settings: Settings, documents: list[str], *, check_files: bool = True
) -> tuple[list[DocumentMetadata], set[str]] | None:
    """Manifest entries to process, or None (after printing why) if invalid.

    `check_files` validates every PDF against the manifest (existence + SHA-256)."""
    try:
        entries = load_manifest(settings.CORPUS_MANIFEST)
    except ManifestError as exc:
        print("INVALID manifest:", *exc.issues, sep="\n  ")
        return None
    if check_files:
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


def _sessions(settings: Settings) -> sessionmaker[Session]:
    return create_sync_sessionmaker(create_sync_engine(settings))


def _check_database(sessions: sessionmaker[Session]) -> bool:
    """Postgres reachable and migrated to the Alembic head this code expects."""
    engine = sessions.kw.get("bind")
    if not isinstance(engine, sa.Engine):
        raise TypeError("sessionmaker must be bound to an Engine")
    try:
        current = current_revision(engine)
    except Exception as exc:
        print(f"Postgres not reachable: {type(exc).__name__}: {exc}. Start it with `make up`.")
        return False
    head = head_revision()
    if current != head:
        print(f"Postgres schema is at {current}, code expects {head}: run `make migrate`.")
        return False
    print(f"  postgres: schema {current}")
    return True


def _check_qdrant(store: ChunkStore, embedder: Embedder) -> bool:
    try:
        created = store.ensure_collection(embedder.dimension)
    except Exception as exc:
        print(f"Qdrant / embedding model not ready: {type(exc).__name__}: {exc}")
        return False
    state = "created" if created else "exists"
    print(f"  qdrant:   collection {store.collection} ({state}); "
          f"model {embedder.model_name}@{embedder.revision}")  # fmt: skip
    return True


def _print_outcome(o: IngestOutcome) -> None:
    p, i = o.persist, o.index
    if o.failed:
        print(f"  {o.document_key:<38} FAILED at {o.failed_stage} ({o.elapsed_s:.1f}s): {o.error}")
        if o.run_id:
            print(f"  {'':<38} recorded as run {o.run_id}")
    else:
        rows = (
            f"{p.pages:>5} {p.chunks:>6} {p.parents:>4} "
            f"{p.chunks_inserted:>4}/{p.chunks_updated}/{p.chunks_deleted:<3}"
            if p
            else f"{'':>5} {'':>6} {'':>4} {'(from db)':<10}"
        )
        idx = (
            f"{i.eligible:>8} {i.embedded:>5} {i.payload_updated:>5} {i.stale_deleted:>5}"
            if i
            else f"{'(not indexed)':>26}"
        )
        print(f"  {o.document_key:<38} {o.status.value:<9} {o.elapsed_s:>6.1f} {rows} {idx}")
    for w in o.warnings:
        print(f"  {'':<38} warning: {w}")


OUTCOME_HEADER = (
    f"  {'document':<38} {'status':<9} {'secs':>6} {'pages':>5} {'chunks':>6} {'par':>4} "
    f"{'ins/upd/del':<10} {'eligible':>8} {'embed':>5} {'paylo':>5} {'stale':>5}"
)


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


def _verify(
    sessions: sessionmaker[Session], store: ChunkStore | None, embedder: Embedder | None
) -> bool:
    """Parent/child integrity and (if a store is given) bidirectional reconciliation.

    A service failing here is reported (exit 1), never raised: the per-document results
    above are already final and recorded."""
    try:
        with sessions() as s:
            families = family_integrity(s)
            report = reconcile(s, store, embedder) if store and embedder else None
    except Exception as exc:
        print(f"verification incomplete: {type(exc).__name__}: {exc} "
              "(re-run `make reconcile` when the service is back)")  # fmt: skip
        return False
    print(f"parent/child families: {families.parents} parents, {families.children} children, "
          f"{'intact' if families.ok else 'PROBLEMS'}")  # fmt: skip
    for problem in families.problems[:MAX_LISTED]:
        print(f"  {problem}")
    if report is None:
        return families.ok
    _print_reconcile(report)
    return families.ok and report.consistent


def _summary(outcomes: list[IngestOutcome], started: float) -> None:
    by_status: dict[str, int] = {}
    for o in outcomes:
        by_status[o.status.value] = by_status.get(o.status.value, 0) + 1
    counts = ", ".join(f"{n} {s}" for s, n in sorted(by_status.items())) or "nothing to do"
    print(f"summary: {len(outcomes)} document(s): {counts}; "
          f"{time.perf_counter() - started:.1f}s total")  # fmt: skip
    failed = [o for o in outcomes if o.failed]
    if failed:
        print(f"{len(failed)} document(s) failed ({', '.join(o.document_key for o in failed)}); "
              "see `runs --failed`, fix the cause, then `retry`.")  # fmt: skip


# --- run / ingest -------------------------------------------------------------------------------


def _print_dry_run(r: DryRunReport) -> None:
    if r.error:
        print(f"  {r.document_key:<38} WOULD FAIL: {r.error}")
        return
    pg = (
        f"doc {r.persist.document}, chunks +{r.persist.chunks_inserted} "
        f"~{r.persist.chunks_updated} -{r.persist.chunks_deleted}"
        if r.persist
        else "postgres: n/a"
    )
    qd = (
        f"embed {len(r.index.to_embed)}, payload {len(r.index.to_overwrite)}, "
        f"stale {len(r.index.stale)}"
        if r.index
        else "qdrant: n/a"
    )
    print(f"  {r.document_key:<38} pages {r.pages:>3} (failed {r.failed_pages}) "
          f"chunks {r.chunks:>4} parents {r.parents:>2} eligible {r.eligible:>4} "
          f"| {pg} | {qd}")  # fmt: skip
    for note in r.notes:
        print(f"  {'':<38} note: {note}")


def _run(
    settings: Settings,
    documents: list[str],
    *,
    force: bool = False,
    dry_run: bool = False,
    mode: Mode = Mode.FULL,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """The unified workflow (`mode` FULL), or persist-only (`mode` PERSIST)."""
    started = time.perf_counter()
    print("[1/4] manifest and PDFs (existence, SHA-256)")
    selected = _selected_entries(settings, documents)
    if selected is None:
        return 2
    entries, _ = selected
    print(f"  {len(entries)} document(s) selected")

    print("[2/4] services")
    sessions = sessions or _sessions(settings)
    needs_index = mode is Mode.FULL
    db_ok = _check_database(sessions)
    if needs_index:
        embedder = embedder or embedder_from_settings(settings)  # loads lazily
        store = store or store_from_settings(settings)
    if dry_run:
        print("[3/4] dry run: Stages A-C, Postgres write rolled back, Qdrant compared read-only")
        dry_setup, dry_setup_error = ocr_setup(settings)
        for entry in entries:
            _print_dry_run(
                dry_run_document(
                    entry, settings, sessions=sessions if db_ok else None,
                    store=store if needs_index else None, embedder=embedder,
                    setup=dry_setup, setup_error=dry_setup_error,
                )  # fmt: skip
            )
        print(f"dry run: nothing written ({time.perf_counter() - started:.1f}s)")
        return 0
    if not db_ok:
        return 2
    if needs_index and (embedder is None or store is None or not _check_qdrant(store, embedder)):
        return 2
    setup, setup_error = ocr_setup(settings)
    ocr_state = f"tesseract {setup.version}" if setup else f"UNAVAILABLE: {setup_error}"
    print(f"  ocr:      {ocr_state}")

    label = "documents" if mode is Mode.FULL else "documents (persist only; not indexed)"
    print(f"[3/4] {label}")
    print(OUTCOME_HEADER)
    outcomes = []
    for entry in entries:
        outcome = ingest_document(
            entry, settings, sessions=sessions, embedder=embedder, store=store,
            setup=setup, setup_error=setup_error, force=force, mode=mode,
            command="ingest" if mode is Mode.FULL else "persist",
        )  # fmt: skip
        _print_outcome(outcome)
        outcomes.append(outcome)

    print("[4/4] verification")
    consistent = _verify(sessions, store if needs_index else None, embedder)
    _summary(outcomes, started)
    return 1 if any(o.failed for o in outcomes) or not consistent else 0


def _ingest(
    settings: Settings,
    documents: list[str],
    *,
    force: bool,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """Stage E entry point, kept: the unified workflow."""
    return _run(settings, documents, force=force, embedder=embedder, store=store,
                sessions=sessions)  # fmt: skip


# --- index from Postgres, retry ------------------------------------------------------------------


def _index_from_db(
    settings: Settings,
    documents: list[str],
    *,
    force: bool,
    dry_run: bool,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """Index Qdrant from committed Postgres rows (no PDF is read) and finalise status."""
    started = time.perf_counter()
    selected = _selected_entries(settings, documents, check_files=False)
    if selected is None:
        return 2
    entries, _ = selected
    sessions = sessions or _sessions(settings)
    if not _check_database(sessions):
        return 2
    embedder = embedder or embedder_from_settings(settings)
    store = store or store_from_settings(settings)
    if dry_run:
        with sessions() as s:
            report = reconcile(s, store, embedder)
        _print_reconcile(report)
        print("dry run: nothing written")
        return 0
    if not _check_qdrant(store, embedder):
        return 2
    print(OUTCOME_HEADER)
    outcomes = []
    for entry in entries:
        outcome = ingest_document(entry, settings, sessions=sessions, embedder=embedder,
                                  store=store, force=force, mode=Mode.INDEX,
                                  command="index")  # fmt: skip
        _print_outcome(outcome)
        outcomes.append(outcome)
    consistent = _verify(sessions, store, embedder)
    _summary(outcomes, started)
    return 1 if any(o.failed for o in outcomes) or not consistent else 0


def _retry(
    settings: Settings,
    documents: list[str],
    *,
    force: bool,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """One recovery attempt per unfinished document (bounded: no internal loop).

    Rows already committed are reused: a document that failed at embedding/indexing, or
    was only persisted, is indexed from Postgres; anything else is re-run in full."""
    started = time.perf_counter()
    selected = _selected_entries(settings, documents)
    if selected is None:
        return 2
    entries, _ = selected
    sessions = sessions or _sessions(settings)
    if not _check_database(sessions):
        return 2
    with sessions() as s:
        plan = [(e, *retry_action(s, e)) for e in entries]
    todo = [(e, a, why) for e, a, why in plan if a is not RetryAction.NONE]
    for e, a, why in plan:
        print(f"  {e.document_key:<38} {a.value:<6} ({why})")
    embedder = embedder or embedder_from_settings(settings)
    store = store or store_from_settings(settings)
    if todo:
        if not _check_qdrant(store, embedder):
            return 2
        setup, setup_error = ocr_setup(settings)
        print(OUTCOME_HEADER)
        outcomes = []
        for entry, action, _ in todo:
            outcome = ingest_document(
                entry, settings, sessions=sessions, embedder=embedder, store=store,
                setup=setup, setup_error=setup_error, force=force,
                mode=Mode.INDEX if action is RetryAction.INDEX else Mode.FULL, command="retry",
            )  # fmt: skip
            _print_outcome(outcome)
            outcomes.append(outcome)
    else:
        outcomes = []
        print("nothing to retry")
    consistent = _verify(sessions, store, embedder)
    _summary(outcomes, started)
    return 1 if any(o.failed for o in outcomes) or not consistent else 0


# --- reconcile, runs ------------------------------------------------------------------------------


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
            print(f"{when}  {r.command:<8} {r.document_key:<38} {r.status:<10}{stage}  run {r.id}")
            if r.error:
                print(f"    error: {r.error}")
    if not rows:
        print("no ingestion runs recorded")
    return 0


# --- search, evaluate ------------------------------------------------------------------


def _short(text: str, limit: int = 320) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _search(
    settings: Settings,
    query: str,
    *,
    top_k: int,
    filters: SearchFilters,
    expand: bool,
    mode: str = "dense",
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """Top-k passages with document + page citations (docs/04).

    `dense`: BGE + Qdrant (M1). `bm25`: keyword index built from Postgres (M2 A1); it
    loads no embedding model and does not contact Qdrant."""
    hits: list[SearchHit]
    try:
        if mode == "bm25":
            sessions = sessions or _sessions(settings)
            hits = KeywordIndexManager(sessions).search(query, top_k=top_k, filters=filters)
        else:
            embedder = embedder or embedder_from_settings(settings)
            store = store or store_from_settings(settings)
            hits = search(query, embedder, store, top_k=top_k, filters=filters)
    except ValueError as exc:
        print(f"invalid query: {exc}")
        return 2
    except KeywordIndexUnavailable as exc:
        print(f"search failed: {exc}")
        return 2
    except Exception as exc:
        print(f"search failed: {type(exc).__name__}: {exc}")
        return 2
    scope = "primary+supporting" + (" +context_only" if filters.include_context_only else "")
    if filters.mandatory_evidence_only:
        scope += ", MANDATORY-capable evidence only"
    if mode != "dense":
        scope += f"; {mode}"
    print(f"query: {query!r}  (top {top_k}; {scope})")
    contexts = {}
    if expand and hits:
        try:
            with (sessions or _sessions(settings))() as s:
                contexts = evidence_context(s, [h.chunk_id for h in hits])
        except Exception as exc:
            print(f"(parent expansion unavailable: {type(exc).__name__}: {exc})")
    if not hits:
        print("no passages found")
    for h in hits:
        p = h.payload
        flags = [p["evidence_eligibility"], p["text_reliability"]]
        if p.get("can_support_mandatory"):
            flags.append("can support MANDATORY")
        print(f"{h.rank}. [{h.score:.3f}] {h.citation}  ({', '.join(flags)})")
        if p.get("section"):
            print(f"   section: {p['section']}")
        print(f"   {_short(p['content'])}")
        ctx = contexts.get(h.chunk_id)
        if ctx is not None and ctx.expanded:
            print(f"   parent paragraph ({len(ctx.context.metadata.content.split())} words): "
                  f"{_short(ctx.context.metadata.content, 600)}")  # fmt: skip
    return 0


def _evaluate(
    settings: Settings,
    dataset: Path,
    *,
    top_k: int,
    report_path: Path | None,
    mode: str = "dense",
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    from importlib.metadata import version

    from app.config import REPO_ROOT

    path = dataset if dataset.is_absolute() else REPO_ROOT / dataset
    if path.name == HELDOUT_DATASET_NAME:
        print(
            f"refused: {path.name} is the frozen held-out set. It is never used to choose or "
            "tune; only the final run of a selected configuration may read it (docs/10)."
        )
        return 2
    try:
        cases = load_cases(path)
    except (OSError, ValueError) as exc:
        print(f"invalid dataset: {exc}")
        return 2
    if mode == "bm25":
        manager = KeywordIndexManager(sessions or _sessions(settings))
        try:
            index = manager.get()
        except KeywordIndexUnavailable as exc:
            print(f"evaluation failed: {exc}")
            return 2
        description = (
            f"BM25 (rank_bm25 {version('rank_bm25')} BM25Okapi, k1=1.5, b=0.75, plain "
            f"tokenizer v{TOKENIZER_VERSION}) over {len(index)} eligible chunks from Postgres"
        )

        def retriever(query: str, filters: SearchFilters, k: int) -> list[SearchHit]:
            return manager.search(query, top_k=k, filters=filters)

        report = evaluate_with(cases, retriever, mode=mode, description=description, k=top_k,
                               dataset=str(dataset), timed_passes=BM25_TIMED_PASSES,
                               index_build_s=index.build_seconds)  # fmt: skip
    else:
        embedder = embedder or embedder_from_settings(settings)
        store = store or store_from_settings(settings)
        report = evaluate(cases, embedder, store, k=top_k, dataset=str(dataset))
    text = render_markdown(report)
    print(text)
    if report_path is not None:
        out = report_path if report_path.is_absolute() else REPO_ROOT / report_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"report written to {out}")
    return 0


# --- argument parsing ------------------------------------------------------------------


def _add_documents(p: argparse.ArgumentParser, what: str = "process") -> None:
    p.add_argument("--document", action="append", default=[], metavar="KEY",
                   help=f"{what} only this document_key (repeatable; default: all)")  # fmt: skip


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingestion.cli",
        description="M1 document ingestion and retrieval (see docs/04).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    vm = sub.add_parser("validate-manifest", help="check manifest.yaml against the corpus")
    vm.add_argument("--manifest", type=Path, default=settings.CORPUS_MANIFEST)
    vm.add_argument("--corpus-dir", type=Path, default=settings.CORPUS_DIR)
    sub.add_parser("check-ocr", help="locate Tesseract and run a real OCR smoke test")

    for name, help_text in (
        ("run", "full workflow: validate, extract, OCR, chunk, Postgres, embed, Qdrant, verify"),
        ("ingest", "alias of `run`"),
    ):
        r = sub.add_parser(name, help=help_text)
        _add_documents(r)
        r.add_argument("--path", type=Path, help="corpus directory holding manifest.yaml")
        r.add_argument("--dry-run", action="store_true",
                       help="report what would change; write nothing")  # fmt: skip
        r.add_argument("--force", action="store_true", help="re-embed every eligible chunk")

    ps = sub.add_parser("persist", help="Stages A-C + Postgres only (document left `pending`)")
    _add_documents(ps)
    ps.add_argument("--path", type=Path, help="corpus directory holding manifest.yaml")
    ps.add_argument("--dry-run", action="store_true", help="report what would change")

    ix = sub.add_parser("index", help="index Qdrant (--from-db: from Postgres rows)")
    _add_documents(ix, "index")
    ix.add_argument("--from-db", action="store_true",
                    help="index from committed Postgres rows and finalise the status")  # fmt: skip
    ix.add_argument("--dry-run", action="store_true", help="report only; no model, no writes")
    ix.add_argument("--force", action="store_true", help="re-embed every eligible chunk")
    ix.add_argument("--prune", action="store_true",
                    help="(in-memory mode) delete points of unknown documents")  # fmt: skip

    rt = sub.add_parser("retry", help="recover documents that are not completed")
    _add_documents(rt, "retry")
    rt.add_argument("--force", action="store_true", help="re-embed every eligible chunk")

    rc = sub.add_parser("reconcile", help="compare Postgres and Qdrant (read-only by default)")
    rc.add_argument("--repair", action="store_true",
                    help="re-index affected documents from Postgres")  # fmt: skip
    rc.add_argument("--delete-unknown", action="store_true",
                    help="with --repair: delete points of documents not in Postgres")  # fmt: skip

    rn = sub.add_parser("runs", help="show recorded ingestion runs")
    rn.add_argument("--document", metavar="KEY")
    rn.add_argument("--failed", action="store_true", help="only failed runs")
    rn.add_argument("--limit", type=int, default=20)

    se = sub.add_parser("search", help="top-k passages with document + page citations")
    se.add_argument("query")
    se.add_argument("--top-k", type=int, default=DEFAULT_K)
    se.add_argument("--entity", action="append", default=[], metavar="TYPE",
                    help="entity type or group, e.g. COMPANY or LEGAL_ENTITY")  # fmt: skip
    se.add_argument("--bank", action="append", default=[], choices=["RBI", "HDFC"])
    _add_documents(se, "search")
    se.add_argument("--include-context-only", action="store_true",
                    help="also search context_only sources (NetBanking form)")  # fmt: skip
    se.add_argument("--mandatory-only", action="store_true",
                    help="only evidence that can support a MANDATORY requirement")  # fmt: skip
    se.add_argument("--expand", action="store_true",
                    help="show the complete parent paragraph of child hits (Postgres)")  # fmt: skip
    se.add_argument("--mode", choices=MODES, default="dense",
                    help="dense (BGE + Qdrant; default) or bm25 (Postgres keywords)")  # fmt: skip

    ev = sub.add_parser("evaluate", help="Recall@k on the labelled retrieval benchmark")
    ev.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ev.add_argument("--top-k", type=int, default=DEFAULT_K)
    ev.add_argument("--report", type=Path, help="also write the markdown report here")
    ev.add_argument("--mode", choices=MODES, default="dense",
                    help="retrieval mode to evaluate (default dense)")  # fmt: skip
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    commands: dict[str, Callable[[], int]] = {
        "validate-manifest": lambda: _validate_manifest(args.manifest, args.corpus_dir),
        "check-ocr": lambda: _check_ocr(settings),
        "run": lambda: _run(_with_corpus_dir(settings, args.path), args.document,
                            force=args.force, dry_run=args.dry_run),
        "persist": lambda: _run(_with_corpus_dir(settings, args.path), args.document,
                                dry_run=args.dry_run, mode=Mode.PERSIST),
        "retry": lambda: _retry(settings, args.document, force=args.force),
        "runs": lambda: _runs(settings, args.document, args.failed, args.limit),
        "evaluate": lambda: _evaluate(settings, args.dataset, top_k=args.top_k,
                                      report_path=args.report, mode=args.mode),
    }  # fmt: skip
    commands["ingest"] = commands["run"]
    if args.command == "index":
        if args.from_db:
            if args.prune:
                parser.error("--prune applies to in-memory indexing; use reconcile --repair")
            return _index_from_db(settings, args.document, force=args.force,
                                  dry_run=args.dry_run)  # fmt: skip
        return _index(settings, args.document, dry_run=args.dry_run, force=args.force,
                      prune=args.prune)  # fmt: skip
    if args.command == "reconcile":
        if args.delete_unknown and not args.repair:
            parser.error("--delete-unknown requires --repair")
        return _reconcile(settings, do_repair=args.repair, delete_unknown=args.delete_unknown)
    if args.command == "search":
        if args.top_k < 1:
            parser.error("--top-k must be >= 1")
        filters = SearchFilters(
            include_context_only=args.include_context_only,
            mandatory_evidence_only=args.mandatory_only,
            banks=tuple(args.bank),
            document_keys=tuple(args.document),
            entity_terms=tuple(args.entity),
        )
        return _search(settings, args.query, top_k=args.top_k, filters=filters,
                       expand=args.expand, mode=args.mode)  # fmt: skip
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
