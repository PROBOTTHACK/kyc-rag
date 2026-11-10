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
           [--include-context-only] [--mandatory-only] [--expand] [--mode dense|bm25|hybrid]
           [--rerank|--no-rerank]   (optional cross-encoder reranking; default RERANK_ENABLED)
  evaluate [--dataset PATH] [--top-k N] [--report PATH] [--mode dense|bm25|hybrid]
           [--compare]   (--mode defaults to RETRIEVAL_MODE, i.e. dense)
  evaluate --select [--report PATH] [--record PATH]   M2 A3 selection: the 7 registered
           configurations on the development set only; applies the pre-registered rule
  evaluate --final-heldout [--report PATH] [--record PATH]   the ONE-TIME held-out run of
           C0 and the A3-selected configuration (refuses to run twice; docs/10)
           Recall@k benchmark (the frozen held-out set is refused; docs/10)
Form schemas (M2 C2; deterministic, from stored M1 chunks):
  forms extract [--document KEY]...   extract and persist (idempotent; never overwrites)
  forms list                          stored schemas
  forms show KEY [--json] [--version N]
Checks:
  validate-manifest [--manifest PATH] [--corpus-dir PATH]
  check-ocr

Exit codes: 0 ok; 1 a document failed or Postgres/Qdrant are inconsistent;
2 invalid input or a service is not ready (nothing was written). A degraded hybrid
search (one retriever failed) is a valid, labelled result: exit 0 + a DEGRADED banner.
"""

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.forms import (
    FormSchemaConflict,
    extract_and_save,
    list_form_schemas,
    load_form_schema,
)
from app.db.migrations import current_revision, head_revision
from app.db.models import Chunk
from app.db.repository import evidence_context, family_integrity
from app.db.runs import recent_runs
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.forms.extract import NotAFormError, extractable_document_keys
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
    DegradedRunError,
    EvalReport,
    RetrievalCase,
    evaluate,
    evaluate_retrieval,
    evaluate_with,
    load_cases,
    render_comparison,
    render_markdown,
)
from app.retrieval.heldout import (
    FROZEN_HELDOUT_SHA256,
    HeldoutProtocolError,
    configurations,
    normalized_sha256,
    render_report,
    results_record,
    run_once,
)
from app.retrieval.indexing import find_orphans, index_document, prune_orphans
from app.retrieval.keyword import TOKENIZER_VERSION, KeywordIndexManager, KeywordIndexUnavailable
from app.retrieval.qdrant_store import ChunkStore, store_from_settings
from app.retrieval.reconcile import ReconcileReport, reconcile, repair
from app.retrieval.rerank import reranker_from_settings
from app.retrieval.retrieve import (
    HitProvenance,
    RetrievalError,
    RetrievalMode,
    RetrievalResult,
    Retriever,
)
from app.retrieval.search import SearchFilters, SearchHit, search
from app.retrieval.selection import (
    CANDIDATES,
    Candidate,
    decide,
    outcome,
    render_selection,
    selection_record,
)
from app.schemas.corpus import DocumentMetadata

MAX_LISTED = 10  # issue ids printed per category
DEFAULT_DATASET = Path("data/eval/m1_retrieval.jsonl")
# Retrieval modes (M2). `dense` is the M1 path and, via RETRIEVAL_MODE, the default.
MODES = tuple(m.value for m in RetrievalMode)
# The frozen M2 held-out set is used only to report an already-selected configuration
# (docs/10, M2_PLAN section 6); `evaluate` refuses it until that explicit final run.
HELDOUT_DATASET_NAME = "m2_heldout.jsonl"
HELDOUT_DATASET = Path("data/eval/m2_heldout.jsonl")
A3_DECISION_RECORD = Path("data/eval/m2_a3_selection.json")
BM25_TIMED_PASSES = 3
TIMED_PASSES = 3  # evaluation latency: 1 scored warm-up pass + 3 timed passes


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
    _print_keyword_info(sessions, store)  # informational only: never changes the verdict
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


def _print_keyword_info(sessions: sessionmaker[Session], store: ChunkStore) -> None:
    """One informational line about the BM25 keyword index (M2 A2). It never raises and
    never affects reconciliation, repair or the exit code."""
    try:
        size = len(KeywordIndexManager(sessions).get())
        with sessions() as s:
            eligible = s.scalar(
                sa.select(sa.func.count()).select_from(Chunk).where(Chunk.embedding_eligible)
            )
        print(
            f"keyword index (informational): {size} chunks; Postgres eligible {eligible}; "
            f"Qdrant {store.count()}"
        )
    except Exception as exc:
        print(f"keyword index (informational): unavailable: {type(exc).__name__}: {exc}")


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
    rerank: bool = False,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """Top-k passages with document + page citations (docs/04).

    `dense`: BGE + Qdrant (M1). `bm25`: keyword index built from Postgres (M2 A1); it
    loads no embedding model and does not contact Qdrant. `rerank` (M2 B1, default off):
    retrieve `RERANK_CANDIDATES`, reorder them with the cross-encoder, keep top-k."""
    hits: list[SearchHit]
    result: RetrievalResult | None = None
    try:
        if rerank:
            sessions = sessions or _sessions(settings)
            result = _retriever(settings, embedder, store, sessions).retrieve(
                query, mode=mode, top_k=top_k, filters=filters, rerank=True
            )
            hits = result.hits
        elif mode == "hybrid":
            sessions = sessions or _sessions(settings)
            result = _retriever(settings, embedder, store, sessions).retrieve(
                query, mode=mode, top_k=top_k, filters=filters
            )
            hits = result.hits
        elif mode == "bm25":
            sessions = sessions or _sessions(settings)
            manager = KeywordIndexManager(sessions, stopwords=settings.BM25_STOPWORDS)
            hits = manager.search(query, top_k=top_k, filters=filters)
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
    except RetrievalError as exc:
        print(f"search failed: every retriever failed: {exc}")
        return 2
    except Exception as exc:
        print(f"search failed: {type(exc).__name__}: {exc}")
        return 2
    scope = "primary+supporting" + (" +context_only" if filters.include_context_only else "")
    if filters.mandatory_evidence_only:
        scope += ", MANDATORY-capable evidence only"
    if mode == "hybrid":
        scope += f"; hybrid RRF k={settings.RRF_K}, {settings.RETRIEVAL_CANDIDATES} per retriever"
    elif mode != "dense":
        scope += f"; {mode}"
    if rerank:
        scope += f"; reranked from {settings.RERANK_CANDIDATES} candidates"
    print(f"query: {query!r}  (top {top_k}; {scope})")
    if result is not None:
        for f in result.failures:
            if f.retriever == "rerank":
                print(f"DEGRADED: reranking failed ({f.error}); "
                      f"showing the original {result.score_kind.value} ranking")  # fmt: skip
            else:
                print(f"DEGRADED: {f.retriever} retrieval failed ({f.error}); "
                      f"showing {result.score_kind.value} results only")  # fmt: skip
        kind = result.score_kind.value
        if result.reranked:
            kind += " (raw, uncalibrated: an ordering score, not a confidence)"
        print(f"scores: {kind}")
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
        if result is not None:
            print(f"   via: {_via(result.provenance[h.chunk_id])}")
        print(f"   {_short(p['content'])}")
        ctx = contexts.get(h.chunk_id)
        if ctx is not None and ctx.expanded:
            print(f"   parent paragraph ({len(ctx.context.metadata.content.split())} words): "
                  f"{_short(ctx.context.metadata.content, 600)}")  # fmt: skip
    return 0


def _via(p: HitProvenance) -> str:
    """`dense #3 (0.812) · bm25 #1 (14.200)`: original ranks and scores behind a hit."""
    parts = []
    for name in ("dense", "bm25"):
        rank, score = getattr(p, f"{name}_rank"), getattr(p, f"{name}_score")
        if rank is not None:
            parts.append(f"{name} #{rank} ({score:.3f})")
    out = " · ".join(parts) or "-"
    if p.pre_rerank_rank is not None:
        out += (f" → retrieval rank #{p.pre_rerank_rank} ({p.pre_rerank_score or 0:.3f})"
                f" · cross-encoder {p.rerank_score or 0:.3f}")  # fmt: skip
    return out


def _retriever(
    settings: Settings,
    embedder: Embedder | None,
    store: ChunkStore | None,
    sessions: sessionmaker[Session],
) -> Retriever:
    """A retriever whose dependencies load lazily (bm25 alone never loads the model)."""
    return Retriever(
        embedder=lambda: embedder or embedder_from_settings(settings),
        store=lambda: store or store_from_settings(settings),
        keyword=lambda: KeywordIndexManager(sessions, stopwords=settings.BM25_STOPWORDS),
        rrf_k=settings.RRF_K,
        candidates=settings.RETRIEVAL_CANDIDATES,
        reranker=lambda: reranker_from_settings(settings),  # loaded only if reranking
        rerank_candidates=settings.RERANK_CANDIDATES,
    )


def _describe_mode(settings: Settings, mode: str) -> str:
    from importlib.metadata import version

    bm25 = (
        f"BM25 (rank_bm25 {version('rank_bm25')} BM25Okapi, k1=1.5, b=0.75, plain tokenizer "
        f"v{TOKENIZER_VERSION}) over the eligible chunks in Postgres"
    )
    dense = (
        f"dense `{settings.EMBEDDING_MODEL}` @ `{settings.EMBEDDING_MODEL_REVISION}`, "
        f"collection `{settings.QDRANT_COLLECTION}`"
    )
    if mode == "dense":
        return dense
    if mode == "bm25":
        return bm25
    return (
        f"hybrid RRF (k={settings.RRF_K}, {settings.RETRIEVAL_CANDIDATES} candidates per "
        f"retriever; fixed A2 defaults) of {dense} and {bm25}; scores are RRF fused scores"
    )


def _evaluate_modes(
    settings: Settings,
    cases: list[RetrievalCase],
    modes: list[str],
    *,
    top_k: int,
    dataset: Path,
    embedder: Embedder | None,
    store: ChunkStore | None,
    sessions: sessionmaker[Session],
    rerank: bool = False,
) -> list[EvalReport]:
    """Evaluate modes through one `Retriever` (uniform timing; degraded runs refused)."""
    retriever = _retriever(settings, embedder, store, sessions)
    reports = []
    for mode in modes:

        def run(q: str, f: SearchFilters, k: int, m: str = mode) -> RetrievalResult:
            return retriever.retrieve(q, mode=m, top_k=k, filters=f, rerank=rerank)

        description = _describe_mode(settings, mode)
        if rerank:
            description += (
                f"; reranked by `{settings.RERANK_MODEL}` @ `{settings.RERANK_MODEL_REVISION}` "
                f"from {settings.RERANK_CANDIDATES} candidates (raw cross-encoder scores)"
            )
        reports.append(
            evaluate_retrieval(cases, run, mode=mode + ("+rerank" if rerank else ""),
                               description=description, k=top_k, dataset=str(dataset),
                               timed_passes=TIMED_PASSES)
        )  # fmt: skip
    return reports


def _evaluate(
    settings: Settings,
    dataset: Path,
    *,
    top_k: int,
    report_path: Path | None,
    mode: str = "dense",
    compare: bool = False,
    rerank: bool = False,
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
    if compare or mode == "hybrid" or rerank:
        modes = [m.value for m in RetrievalMode] if compare else [mode]
        try:
            reports = _evaluate_modes(
                settings,
                cases,
                modes,
                top_k=top_k,
                dataset=dataset,
                embedder=embedder,
                store=store,
                sessions=sessions or _sessions(settings),
                rerank=rerank,
            )
        except (DegradedRunError, RetrievalError) as exc:
            print(f"evaluation refused: {type(exc).__name__}: {exc}")
            return 2
        if compare:
            note = ("fixed A2 defaults; nothing is selected or tuned here (selection "
                    "happens in M2 A3 on this development set)")  # fmt: skip
            return _emit(render_comparison(reports, note=note), report_path, REPO_ROOT)
        return _emit(render_markdown(reports[0]), report_path, REPO_ROOT)
    if mode == "bm25":
        manager = KeywordIndexManager(
            sessions or _sessions(settings), stopwords=settings.BM25_STOPWORDS
        )
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
    return _emit(render_markdown(report), report_path, REPO_ROOT)


def _select(
    settings: Settings,
    *,
    report_path: Path | None,
    record_path: Path | None,
    dataset: Path = DEFAULT_DATASET,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """M2 A3: evaluate the 7 registered configurations on the DEVELOPMENT set and apply the
    pre-registered rule (app/retrieval/selection.py). `dataset` is not exposed on the CLI;
    the frozen held-out set is refused like everywhere else."""
    import hashlib
    import json
    import subprocess

    from app.config import REPO_ROOT

    path = dataset if dataset.is_absolute() else REPO_ROOT / dataset
    if path.name == HELDOUT_DATASET_NAME:
        print("refused: selection runs on the development set only; the frozen held-out set "
              "is never used to choose (docs/10).")  # fmt: skip
        return 2
    try:
        cases = load_cases(path)
        sha = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except (OSError, ValueError) as exc:
        print(f"invalid dataset: {exc}")
        return 2
    sessions = sessions or _sessions(settings)
    managers = {flag: KeywordIndexManager(sessions, stopwords=flag) for flag in (False, True)}
    shared_embedder: list[Embedder] = []

    def get_embedder() -> Embedder:
        if not shared_embedder:  # load the model once for every dense / hybrid candidate
            shared_embedder.append(embedder or embedder_from_settings(settings))
        return shared_embedder[0]

    def retriever_for(c: Candidate) -> Retriever:
        return Retriever(
            embedder=get_embedder,
            store=lambda: store or store_from_settings(settings),
            keyword=lambda: managers[bool(c.stopwords)],
            rrf_k=c.rrf_k or settings.RRF_K,
            candidates=settings.RETRIEVAL_CANDIDATES,
        )

    outcomes = []
    for c in CANDIDATES:
        r = retriever_for(c)

        def run(
            q: str, f: SearchFilters, k: int, r: Retriever = r, m: str = c.mode
        ) -> RetrievalResult:
            return r.retrieve(q, mode=m, top_k=k, filters=f)

        print(f"  {c.id} {c.label} ...")
        try:
            report = evaluate_retrieval(
                cases,
                run,
                mode=c.mode,
                description=c.label,
                k=DEFAULT_K,
                dataset=str(dataset),
                timed_passes=TIMED_PASSES,
            )
        except (DegradedRunError, RetrievalError) as exc:
            print(f"selection refused ({c.id}): {type(exc).__name__}: {exc}; nothing recorded")
            return 2
        outcomes.append(outcome(c, report))
    decision = decide(outcomes)
    try:
        # Fixed argv, no shell; git from PATH; a read-only query.
        commit = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)
    _emit(render_selection(decision, dataset=rel, dataset_sha256=sha), report_path, REPO_ROOT)
    if record_path is not None:
        out = record_path if record_path.is_absolute() else REPO_ROOT / record_path
        out.parent.mkdir(parents=True, exist_ok=True)
        record = selection_record(decision, dataset=rel, dataset_sha256=sha, code_commit=commit)
        out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"decision record written to {out}")
    return 0


def _final_heldout(
    settings: Settings,
    *,
    report_path: Path | None,
    record_path: Path,
    dataset: Path = HELDOUT_DATASET,
    decision: Path = A3_DECISION_RECORD,
    expected_sha256: str = FROZEN_HELDOUT_SHA256,
    embedder: Embedder | None = None,
    store: ChunkStore | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    """The ONE-TIME held-out evaluation (app/retrieval/heldout.py): C0 and the configuration
    selected by the committed A3 decision record, one scored pass each. Refuses to run if
    results already exist, if the decision record is inconsistent, or if the held-out file
    does not match its frozen hash. A failed run records nothing and is not retried."""
    import json
    import subprocess
    from datetime import UTC, datetime

    from app.config import REPO_ROOT

    def resolve(p: Path) -> Path:
        return p if p.is_absolute() else REPO_ROOT / p

    out = resolve(record_path)
    if out.exists():
        print(f"refused: {out} exists; the held-out evaluation is one-time (docs/10)")
        return 2
    try:
        record = json.loads(resolve(decision).read_text(encoding="utf-8"))
        configs = configurations(record)
    except (OSError, ValueError, KeyError, HeldoutProtocolError) as exc:
        print(f"refused: decision record unusable: {type(exc).__name__}: {exc}")
        return 2
    path = resolve(dataset)
    sha = normalized_sha256(path)
    if sha != expected_sha256:
        print(f"refused: {path.name} SHA-256 {sha} does not match the frozen {expected_sha256}")
        return 2
    cases = load_cases(path)
    sessions = sessions or _sessions(settings)
    managers = {flag: KeywordIndexManager(sessions, stopwords=flag) for flag in (False, True)}
    model = embedder or embedder_from_settings(settings)
    chunk_store = store or store_from_settings(settings)
    # Warm the services WITHOUT any held-out query, so the single timed pass measures
    # retrieval rather than model loading or the first index build.
    _ = model.dimension
    model.embed_query("warm-up query for model initialisation")
    for flag in {bool(c.stopwords) for c in configs}:
        managers[flag].get()

    def runner(c: Candidate) -> Callable[[str, SearchFilters, int], RetrievalResult]:
        r = Retriever(
            embedder=lambda: model,
            store=lambda: chunk_store,
            keyword=lambda: managers[bool(c.stopwords)],
            rrf_k=c.rrf_k or settings.RRF_K,
            candidates=settings.RETRIEVAL_CANDIDATES,
        )

        def run(q: str, f: SearchFilters, k: int) -> RetrievalResult:
            return r.retrieve(q, mode=c.mode, top_k=k, filters=f)

        return run

    rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)
    try:
        runs = run_once(cases, {c: runner(c) for c in configs}, k=DEFAULT_K, dataset=rel)
    except (DegradedRunError, RetrievalError, HeldoutProtocolError) as exc:
        print(f"held-out run FAILED ({type(exc).__name__}: {exc}); nothing recorded. "
              "Do not re-run without the project owner's decision.")  # fmt: skip
        return 2
    created = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        commit = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    text = render_report(runs, record=record, dataset=rel, sha256=sha, created_at=created)
    _emit(text, report_path, REPO_ROOT)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = results_record(runs, record=record, dataset=rel, sha256=sha, code_commit=commit,
                            created_at=created)  # fmt: skip
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"held-out results record written to {out}")
    return 0


def _emit(text: str, report_path: Path | None, root: Path) -> int:
    print(text)
    if report_path is not None:
        out = report_path if report_path.is_absolute() else root / report_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"report written to {out}")
    return 0


def _forms_extract(
    settings: Settings, documents: list[str], sessions: sessionmaker[Session] | None = None
) -> int:
    """Extract and persist form schemas, one transaction per document. Exit 0 when every
    document is inserted or unchanged; 1 on a conflict or failure (nothing overwritten);
    2 when the database is not ready or a key is not an extractable document."""
    sessions = sessions or _sessions(settings)
    if not _check_database(sessions):
        return 2
    with sessions() as s:
        known = extractable_document_keys(s)
    keys = documents or known
    unknown = sorted(set(keys) - set(known))
    if unknown:
        print(f"not extractable (not a stored form or reference document): {', '.join(unknown)}")
        return 2
    failed = 0
    for key in keys:
        try:
            with sessions() as s:
                outcome = extract_and_save(s, key)
                s.commit()
            print(f"  {key:<38} {outcome.status:<9} v{outcome.extractor_version} "
                  f"schema {outcome.schema_sha256[:12]}")  # fmt: skip
        except FormSchemaConflict as exc:
            failed += 1
            print(f"  {key:<38} CONFLICT  {exc}")
        except (NotAFormError, LookupError, ValueError) as exc:
            failed += 1
            print(f"  {key:<38} FAILED    {type(exc).__name__}: {exc}")
    print(f"forms: {len(keys) - failed} ok, {failed} not saved")
    return 1 if failed else 0


def _forms_list(settings: Settings, sessions: sessionmaker[Session] | None = None) -> int:
    sessions = sessions or _sessions(settings)
    if not _check_database(sessions):
        return 2
    with sessions() as s:
        rows = list_form_schemas(s)
    if not rows:
        print("no form schemas stored; run `forms extract`")
    for r in rows:
        flags = ",".join(r.quality_flags) or "-"
        print(f"  {r.form_key:<38} {r.document_role:<18} {r.evidence_eligibility:<12} "
              f"v{r.extractor_version} sections {r.section_count:>3} fields {r.field_count:>3} "
              f"required-as-shown {r.required_as_shown_count:>3} flags {flags}")  # fmt: skip
    return 0


def _forms_show(
    settings: Settings,
    key: str,
    *,
    as_json: bool,
    version: int | None,
    sessions: sessionmaker[Session] | None = None,
) -> int:
    sessions = sessions or _sessions(settings)
    if not _check_database(sessions):
        return 2
    with sessions() as s:
        schema = load_form_schema(s, key, extractor_version=version)
    if schema is None:
        print(f"no stored form schema for {key!r}; run `forms extract --document {key}`")
        return 2
    if as_json:
        print(schema.model_dump_json(indent=2))
        return 0
    print(f"{schema.form_key}: {schema.title}")
    print(f"  role {schema.document_role}; eligibility {schema.evidence_eligibility}; "
          f"{schema.extraction_method} v{schema.extractor_version}; sha256 "
          f"{schema.document_sha256[:12]}; flags {', '.join(schema.flags) or '-'}")  # fmt: skip
    print("  Fields show what the form ASKS FOR (appears on form); they are not policy "
          "obligations. `*` = marked as required on the form itself.")  # fmt: skip
    for sec in schema.sections:
        pages = (f"p.{sec.page_start}" if sec.page_start == sec.page_end
                 else f"pp.{sec.page_start}-{sec.page_end}")  # fmt: skip
        tags = (["declaration"] if sec.is_declaration else []) + sec.flags
        print(f"  [{sec.ordinal}] {sec.name or '(no heading)'}  {pages}"
              + (f"  ({', '.join(tags)})" if tags else ""))  # fmt: skip
        for f in sec.fields:
            mark = {True: "*", False: " ", None: "?"}[f.required_as_shown]
            note = f"  [marker: {f.marker_issue}]" if f.marker_issue else ""
            print(f"      {mark} {f.printed_label}  (p.{f.page_number}, chunk "
                  f"{f.chunk_id[:8]}#{f.position}){note}")  # fmt: skip
        for b in sec.text_blocks:
            conf = f", OCR conf {b.ocr_confidence:.2f}" if b.ocr_confidence is not None else ""
            print(f"      - text (p.{b.page_number}, {b.verification_status}{conf}, chunk "
                  f"{b.chunk_id[:8]}): {_short(b.text, 120)}")  # fmt: skip
    for i in schema.issues:
        print(f"  issue: {i.issue} (p.{i.page_number}, chunk {i.chunk_id[:8]}): {i.detail}")
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

    fm = sub.add_parser("forms", help="form schemas from stored M1 chunks (M2 C2)")
    fsub = fm.add_subparsers(dest="forms_command", required=True)
    fx = fsub.add_parser("extract", help="extract and persist (idempotent; never overwrites)")
    _add_documents(fx, "extract")
    fsub.add_parser("list", help="stored form schemas")
    fs = fsub.add_parser("show", help="one stored form schema")
    fs.add_argument("key", metavar="KEY")
    fs.add_argument("--json", action="store_true", help="print the stored schema as JSON")
    fs.add_argument("--version", type=int, help="extractor version (default: latest)")

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
    se.add_argument("--mode", choices=MODES, default=None,
                    help="dense, bm25 or hybrid (default: RETRIEVAL_MODE)")  # fmt: skip
    se.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=None,
                    help="cross-encoder reranking (default: RERANK_ENABLED, i.e. off)")  # fmt: skip

    ev = sub.add_parser("evaluate", help="Recall@k on the labelled retrieval benchmark")
    ev.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ev.add_argument("--top-k", type=int, default=DEFAULT_K)
    ev.add_argument("--report", type=Path, help="also write the markdown report here")
    ev.add_argument("--mode", choices=MODES, default=None,
                    help="retrieval mode to evaluate (default: RETRIEVAL_MODE)")  # fmt: skip
    ev.add_argument("--compare", action="store_true",
                    help="compare dense, bm25 and hybrid at fixed A2 defaults")  # fmt: skip
    ev.add_argument("--select", action="store_true",
                    help="M2 A3: registered configurations on the dev set")  # fmt: skip
    ev.add_argument("--record", type=Path, help="with --select: write the decision record here")
    ev.add_argument("--final-heldout", action="store_true",
                    help="ONE-TIME held-out run: C0 + the A3 selection")  # fmt: skip
    ev.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=None,
                    help="with cross-encoder reranking (default: RERANK_ENABLED)")  # fmt: skip
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    rerank = bool(settings.RERANK_ENABLED if getattr(args, "rerank", None) is None
                  else args.rerank)  # fmt: skip
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
                                      report_path=args.report,
                                      mode=args.mode or settings.RETRIEVAL_MODE,
                                      compare=args.compare, rerank=rerank),
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
    if args.command == "evaluate" and args.rerank and (args.select or args.final_heldout
                                                        or args.compare):  # fmt: skip
        parser.error("--rerank is not part of the frozen --select / --final-heldout / "
                     "--compare protocols")  # fmt: skip
    if args.command == "evaluate" and args.final_heldout:
        if args.mode or args.compare or args.select or args.dataset != DEFAULT_DATASET:
            parser.error("--final-heldout runs the frozen protocol; it takes no other options")
        if args.record is None:
            parser.error("--final-heldout requires --record")
        return _final_heldout(settings, report_path=args.report, record_path=args.record)
    if args.command == "evaluate" and args.select:
        if args.mode or args.compare or args.dataset != DEFAULT_DATASET:
            parser.error("--select uses the registered configurations on the development set; "
                         "it takes no --mode, --compare or --dataset")  # fmt: skip
        return _select(settings, report_path=args.report, record_path=args.record)
    if args.command == "evaluate" and args.record:
        parser.error("--record requires --select")
    if args.command == "forms":
        if args.forms_command == "extract":
            return _forms_extract(settings, args.document)
        if args.forms_command == "list":
            return _forms_list(settings)
        return _forms_show(settings, args.key, as_json=args.json, version=args.version)
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
                       expand=args.expand, mode=args.mode or settings.RETRIEVAL_MODE,
                       rerank=rerank)  # fmt: skip
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
