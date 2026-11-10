# 04 — Ingestion Pipelines

Ingestion must be independently executable: `python -m app.ingestion.cli run --path data/raw_pdfs` (also via queue + `POST /ingestion/run`). Idempotent by file SHA-256.

## Step 0 — Classify documents
For each source PDF record (in `data/raw_pdfs/manifest.yaml`; schema and vocabularies in docs/15): document_key, file, sha256, `document_type` (`policy` | `regulatory` | `form` | `annexure` | `reference`), `source_authority` (null until verified) and `attribution_basis`, `source_status`, entity types covered, residency. Quality (text/scanned/mixed) is measured at ingestion. **Inspect real PDFs first.**

## Pipeline A — Policy & regulatory documents
PDF validation -> PyMuPDF extraction (text, page numbers, metadata, layout) -> text-quality check -> (poor/scanned -> OCR fallback) -> section & table extraction -> semantic chunking -> metadata assignment -> BGE embeddings -> Qdrant upsert + Postgres chunk rows (source of truth; BM25 index is rebuilt from them).

**Extraction and OCR (implemented, M1 Phase 3; `app/ingestion/{pdf,structure,quality,ocr,pages}.py`):**
- Every physical page gets a record; citations use the physical PDF page number. Bookmarks give section paths (with mid-page y-positions). Footnotes are moved out of the body only when a matching superscript marker exists, and are kept for the citing paragraph. Running page numbers and contents blocks are removed and counted by reason.
- OCR trigger: fewer than 50 non-whitespace text-layer characters **and** images covering more than 80% of the page. Native text is never replaced by OCR unless OCR yields more text.
- OCR: PyMuPDF renders the page at 300 dpi; the configured Tesseract executable returns TSV with per-word confidence (fixed PSM 3, `eng`, single thread, so results are deterministic). Pages wider than 1.5× their height (the two-up fold-out form) are OCR'd as separate left/right regions that are never mixed. Printed page numbers are taken from "Page N of M" only when OCR reads it cleanly; otherwise they stay unknown.
- Page status: `ok` (mean word confidence ≥ 70 for OCR pages), `ocr_low_confidence` (< 70), `low_quality` (no usable text, or OCR no better than a short native layer), `failed` (OCR error, no OCR output, or Tesseract unavailable; always with an error). Lines with mean word confidence < 40 are flagged.
- Successful OCR results are cached in `OCR_CACHE_DIR` (gitignored), keyed by PDF SHA-256, page, region, DPI, PSM, language and Tesseract version.

**Chunking rules:** don't cut blindly at N chars. A chunk should hold heading + complete statement + conditions + exceptions + source metadata. Use parent–child chunking when a requirement spans paragraphs (retrieve child, supply parent to synthesis). Keep page number on every chunk.

**Chunking (implemented, M1 Phase 3 Stage C; `app/ingestion/{cleaning,chunking}.py`, `app/ingestion/chunkers/`). Embeddings and Qdrant indexing are Stage D; Postgres persistence of every chunk is Stage E (both below).** The manifest's `chunking_profile` selects the chunker.
- `rbi_directions`: a unit runs from a numbered paragraph ("33.", at the left margin) to the next one or the next bookmark section, so paragraphs crossing pages stay whole (`page_end`). Annex paragraphs are labelled by annex ("Annex – I 14"), because annexes restart their numbering. Bookmark headings become `section`, not content. Footnotes (amendment notes) are attached to the chunk that cites them (`footnotes`), never mixed into the text. Ruled tables become one chunk per row, with header rows as column names and the group label carried down. Entity types are narrowed only when a paragraph opens with "For opening an account … of a <legal form>".
- `rbi_faq`: one chunk per "Q n." and its answer; section = "Q<n>: <question>".
- `question_headings` (HDFC policy): one chunk per bold question heading and its answer. The two-column features/documents table becomes one chunk per row: document groups are paired with the feature they sit beside, a single document block belongs to the whole row, and the table note ("not an exhaustive list") is appended to every row.
- `form`: chosen per page by the Stage B extraction method. Native pages: blocks in reading order; lettered ("S) …"), larger-font and all-caps headings start sections (also when they sit inside a text block); prose becomes `form_text` chunks, short labels become one `form_fields` chunk per section; lines repeated on a page are kept once. OCR pages: each fold-out half is chunked on its own; lines with mean word confidence < 40 or no word-like text are left out of the content but stored with the chunk (`excluded_lines`). Form chunks show what a form requests, never a regulatory obligation (`supporting`/`context_only` eligibility).
- Every chunk carries the physical page(s), section, paragraph label (citation text, not a unique key), region, content kind, chunk role, the worst page status it spans and, for OCR text, the mean confidence of its lines. Raw pre-normalisation text is kept with each chunk (`raw_text`, stored in Postgres only); raw page OCR stays in the OCR cache and on the processed page. Normalisation: NFKC (ligatures), private-use bullets, letter-spaced words, whitespace, compound hyphens kept.
- Embedding candidates are at most 300 words. Longer paragraphs become a `parent` (never embedded; stored in Postgres only) plus `child` chunks split at sub-clause starts "(1)", "(i)", "(a)". An oversized clause falls back to whole lines, ending a child only after a line that closes a sentence or clause and never directly before a qualifier ("Provided that", "Explanation", "However", …). An open lead ("… shall ensure that:", "(2) V-CIP Procedure") moves into the child holding the list it introduces when that fits. Children reassemble their parent exactly. Every child after the first carries `context`: the paragraph's lead and, where the previous child ended on an open lead, that lead. `context` is outside `content`. Retrieval returns children; synthesis and citation expand a child to its parent (`ChunkingResult.parent_of`).
- OCR evidence policy (docs/15): every chunk has `text_reliability` (`native`, `ocr_reliable`, `ocr_uncertain`), `embedding_eligible` and a derived `can_support_mandatory`.
- Ids are deterministic: `uuid5(sha256, chunk_index)`, with indices in document order and a parent before its children. Lines not used as content are reported as exclusions with a reason (`section_heading`, `table_cell_line`, `duplicate_line`, `noise`, …).

**Chunk metadata** (= Qdrant payload; authoritative schema `ChunkMetadata` in `app/schemas/corpus.py`, illustrative values):
```json
{"chunk_id":"<uuid>","document_id":"<uuid>","document_key":"rbi_kyc_directions_2025",
 "document_name":"...","file":"regulatory/169MD.pdf","bank":"RBI","document_type":"regulatory",
 "source_status":"active","evidence_eligibility":"primary","source_authority":"RBI",
 "version":"Updated as on October 1, 2026","effective_from":null,"effective_until":null,
 "entity_types":["COMPANY"],"residency_statuses":["RESIDENT","NRI","PIO"],
 "section":"Chapter VI > C. CDD Measures for Legal Entities","page_number":37,"page_end":null,
 "chunk_index":0,"parent_chunk_id":null,"extraction_method":"text","content":"..."}
```
Never invent dates or authority; unknown = `null`.

## Pipeline B — Forms and applications
Form PDF -> page extraction -> page images (rendered) -> multimodal analysis -> structured JSON -> Pydantic schema validation -> (optional human review) -> approved schema -> reference store (Postgres `form_schemas`, cached in Redis).

Extract: sections, fields (name/type/required-as-shown/page), checkboxes, signature blocks, declarations, annexure references, entity applicability. Keep original page ref per field.

**Critical distinction:** a field on a blank form ≠ legally mandatory. Store `appears_on_form` separately from `mandatory_under_policy` (the latter only set via rules layer / verified policy).

**Versioning:** store form_id, version, file hash, extraction timestamp, review status, entity categories. A new form version never silently merges with an old one.

**Embeddings and Qdrant indexing (implemented, M1 Stage D; `app/retrieval/{embeddings,qdrant_store,indexing,search}.py`):**
- Model: `BAAI/bge-small-en-v1.5` at the pinned revision `EMBEDDING_MODEL_REVISION`, on CPU. It produces 384-dimensional vectors, L2-normalised, compared by cosine. Queries get the BGE instruction ("Represent this sentence for searching relevant passages: "); passages don't.
- Embedding input = the child `context` (if any) + chunk `content`; title and section are not embedded. On a dev probe set of 8 queries (separate from the Stage G benchmark), adding title and section let short chunks rank on the title alone: MRR 0.67, against 0.81 for context + content. Every input fits the 512-token limit (max 476 tokens in the corpus). An input that would be truncated raises an error instead.
- Indexed: only `embedding_eligible` chunks (single and child), so 429 of the 456 chunks; the 27 parents are not indexed. Point id = `chunk_id`, and the payload is the chunk metadata plus an `index` object (model, revision, input version, input and payload hashes).
- Idempotent re-indexing, per document:
  - a changed embedding input, model or revision is re-embedded and upserted;
  - metadata-only changes overwrite the payload and keep the vector;
  - unchanged chunks are skipped;
  - points that are no longer current eligible chunks are deleted as stale, *after* the upserts.
  - Points of documents missing from the manifest are reported, and are deleted only with `--prune`.
  - An existing collection with the wrong vector size or distance is an error and is never recreated automatically.
- Command: `cd backend && ../.venv/Scripts/python -m app.ingestion.cli index [--document KEY]... [--dry-run] [--force] [--prune]` indexes straight from memory, without Postgres. Use `ingest` (Stage E, below) for the persisted, recorded path; Stage F folds it into `run` / `make ingest`.
- Retrieval (`search`): eligibility is a filter. By default only `primary` and `supporting` sources are searched; `include_context_only` adds NetBanking, and `mandatory_evidence_only` keeps only `can_support_mandatory` chunks. A retrieved child carries `parent_chunk_id`; `app.db.repository.evidence_context` expands it to the complete parent paragraph stored in Postgres.

**Postgres persistence, run tracking and reconciliation (implemented, M1 Stage E; `app/db/{persistence,repository,runs}.py`, `app/ingestion/pipeline.py`, `app/retrieval/reconcile.py`; migration `0002_m1_persistence`):**
- Postgres is the source of truth for documents, pages and chunks; Qdrant is a derived index. All 456 chunks are stored, the 27 parents included. Each chunk keeps its normalised `content` and its `raw_text`, plus all its provenance: physical pages, printed page, section, paragraph, region, content kind, footnotes, OCR reliability and confidence, page status, eligibility flags, and excluded OCR lines with their confidence. Each page keeps its Stage A/B record (`document_pages.metadata`). Lines that the chunkers left out of every chunk are stored with their reason in `document_exclusions`. The stored rows rebuild the exact Stage C `ChunkMetadata` and extras (`chunk_metadata_from_rows`).
- `ingest` runs per document, in this order. Postgres and Qdrant never share a transaction:
  1. An `ingestion_runs` row is written in its own transaction.
  2. Stages A–C run. An exception is attributed to `extraction`, `ocr` or `chunking`. A page that fails OCR does not raise: it is recorded with its error, and the document ends `partial`.
  3. One Postgres transaction writes the document, its pages, chunks and exclusions, and leaves the document `processing`.
  4. Qdrant is indexed from the committed rows, so every point references a stored chunk. Stage D's ordering applies: upserts first, stale deletes after.
  5. The document's point ids are checked against its eligible chunks.
  6. Only then is the document `completed` (or `partial`).
- If a stage fails, the run records the stage (`extraction`, `ocr`, `chunking`, `persistence`, `embedding`, `indexing`) and the error, and marks later stages `skipped`. An existing document is set to `failed`; its previous rows and vectors stay intact.
  - A failed first ingestion rolls its document row back, but the run row survives: it has no FK to `documents`. The CLI prints the failure, and `runs --failed` lists it.
- Idempotency is per document, with ids derived from the content:
  - an unchanged document writes nothing and re-embeds nothing;
  - changed rows are updated in place;
  - chunks that are no longer produced are deleted, parents together with their children;
  - a different file under an existing `document_key` is refused (one version per key in M1).
- Parent/child integrity is enforced twice. Before any write, `validate_prepared` refuses:
  - a child without a parent in the same document;
  - a parent without children;
  - an embeddable parent;
  - a chunk on a page that wasn't processed.

  In the database: `(document_id, parent_chunk_id) → chunks(document_id, id)` is checked at commit with no delete action, so deleting a parent that still has children fails. CHECKs require that exactly the children have a parent, that parents are never embedded, and that `can_support_mandatory` matches its rule. `family_integrity` also checks that the children reassemble the parent (Stage C invariant).
- `reconcile` is read-only. It reports:
  - eligible chunks missing from Qdrant;
  - points with no Postgres chunk;
  - points of chunks that are not embedding-eligible;
  - points whose payload, embedding input, model or revision is out of date.

  `reconcile --repair` re-indexes the affected documents from Postgres without re-reading any PDF. Points of documents unknown to Postgres are deleted only with `--delete-unknown`.
- Commands (`cd backend && ../.venv/Scripts/python -m app.ingestion.cli …`): `ingest` (since Stage F an alias of `run`, below), `reconcile [--repair [--delete-unknown]]`, `runs [--document KEY] [--failed] [--limit N]`.
- Reuse: the functions take a sync SQLAlchemy `Session` (`app.db.session.create_sync_sessionmaker`). Async FastAPI code can call them through `AsyncSession.run_sync`. `ingest_document(..., prepared=...)` accepts an already-prepared document.
- Measured on the corpus (2026-11-08): 9 documents and 135 pages (5 OCR) stored. 456 chunks: 332 single, 27 parent, 97 child; 429 eligible. 774 document exclusions. All 27 families intact. Postgres and Qdrant both hold 429 eligible chunks/points, consistent in every direction. The first `ingest` on the existing Stage D collection re-embedded 0 points and overwrote 0 payloads, because the rows rebuild byte-identical payloads. A second `ingest` wrote 0 rows.

**Unified workflow, recovery, search and benchmark (implemented, M1 Stage F; `app/ingestion/{cli,pipeline}.py`, `app/retrieval/{evaluation,indexing}.py`, `app/core/retry.py`, `Makefile`):**
- `run` (alias `ingest`; `make ingest`) is the M1 entry point named at the top of this page. The queue + `POST /ingestion/run` path is M3. It runs in four steps:
  1. **Manifest and PDFs**: schema rules, every file exists, SHA-256 matches. Any error stops the run before any write.
  2. **Services**: Postgres reachable and at the Alembic head (otherwise "run `make migrate`"); Qdrant collection created or validated; embedding model and revision; Tesseract version, or a warning that scanned pages will fail.
  3. **Documents**, one at a time and isolated from each other: the Stage E sequence (A–C → Postgres → Qdrant from committed rows → per-document point check → status), timed per document.
  4. **Verification**: parent/child integrity and bidirectional reconciliation over the whole store.

  Exit codes: `0` everything ok; `1` a document failed or Postgres/Qdrant are inconsistent; `2` invalid input or a service not ready (nothing was written).
- Options: `--document KEY` (repeatable; default all), `--path DIR` (a corpus directory holding `manifest.yaml` and the PDFs), `--force` (re-embed every eligible chunk), `--dry-run`.
- `--dry-run` runs Stages A–C and validation, then executes the Postgres write inside a transaction that is rolled back. It compares with Qdrant read-only (`plan_index`, the read-only half of Stage D's `index_document`). It reports per document what would be inserted, updated or deleted and what would be embedded, overwritten or deleted. It records no run, loads no model and writes nothing.
- Independent steps reuse the same stage methods (`pipeline.Mode`):
  - `persist` (`make persist`): Stages A–C + Postgres only. The document is left `pending` ("stored, not fully ingested"), the run ends `pending`, and embedding/indexing are recorded as skipped.
  - `index --from-db` (`make index`): embeds and indexes from committed Postgres rows (no PDF is read), checks the points, then sets the document `completed`, or `partial` if a stored page failed.
  - `index` without `--from-db` is still the Stage D in-memory indexer (no Postgres).
- `retry` (`make retry`) makes one bounded recovery attempt per document. A document that is `completed` or `partial` is skipped. A document that was only persisted, or whose last run failed at `embedding`/`indexing`, is indexed from Postgres, reusing the committed rows. Anything else (never ingested, failed at extraction/OCR/chunking/persistence, stuck in `processing`) runs again in full. There is no automatic retry loop.
- Transient Qdrant failures (connection errors, timeouts, HTTP 429/5xx) are retried at most twice, with 0.5 s and 1 s backoff, and only for idempotent calls (upsert by id, delete by id, payload overwrite, reads; not `create_collection`). This implements CLAUDE.md principle 8. Postgres has a 5 s connect timeout. A failure that persists becomes a document-level failure, recovered with `retry`.
- `search QUERY` (`make search Q=…`) returns the top 5 passages with document + page citations (plus region/paragraph), eligibility, reliability and whether the chunk can support MANDATORY. It applies the default eligibility filter (primary + supporting). Options: `--entity` (types or groups), `--bank`, `--document`, `--include-context-only`, `--mandatory-only`, `--top-k`. `--expand` adds the complete parent paragraph of child hits from Postgres.
- `evaluate` (`make eval`): Recall@5 on `data/eval/m1_retrieval.jsonl` (docs/10, "M1 retrieval benchmark"). It prints, and optionally writes, a markdown report (`data/processed/m1_retrieval_report.md` by default via Make; gitignored).
- Measured end to end on a fresh database and a fresh collection (2026-11-08, Windows 11, CPU, OCR cache warm):
  - Cold `run`: 9/9 `completed`, 135 pages, 456 chunks (27 parents), 429 vectors, consistent. 64.0 s in the pipeline (69.9 s wall clock; 169MD 19.8 s).
  - Second `run`: 0 rows written, 0 embeddings, consistent, 27.9 s (mostly model load and verification).
  - Failure recovery: see `docs/milestones/M1_HANDOFF.md` §12.

**BM25 keyword retrieval (implemented, M2 A1; `app/retrieval/keyword.py`, `SearchFilters.matches` in `app/retrieval/search.py`, `eligible_chunk_payloads` / `chunk_corpus_fingerprint` in `app/db/repository.py`):**
- **Corpus:** every embedding-eligible chunk read from Postgres: the same 429 single and child chunks Qdrant holds, every eligibility value. Parents are never indexed; a child hit expands to its parent paragraph through `evidence_context`, exactly as a dense hit does.
- **Payloads:** built by the same row → payload mapping as Qdrant (`chunk_metadata_from_rows`), so citations and provenance are identical. A test compares all 429 payloads with Qdrant's (minus the `index` bookkeeping).
- **Indexed text:** `embedding_input` (the child's `context` + `content`), the same text dense retrieval embeds.
- **Tokenizer** (plain, `TOKENIZER_VERSION = 1`): NFKC, lowercase, alphanumeric runs joined by internal `.`, `/` or `-`, so "v-cip", "51a" and "1718" stay whole. No stopwords and no stemming. The stopword variant is added in M2 A3, for the development-set comparison only.
- **Scoring:** `rank_bm25` 0.2.2 `BM25Okapi` with library defaults (k1 = 1.5, b = 0.75), not tuned. IDF and average length are global over the 429 chunks.
- **Filtering before scoring:** metadata filters are applied first. Only chunks that pass `SearchFilters.matches` are scored at all, so a filtered-out chunk can never occupy a rank. Filters restrict the candidates; they don't change the corpus statistics.
  - `SearchFilters.matches` is the in-process equivalent of the Qdrant filter (`build_filter`, unchanged).
  - Parity is tested on the Qdrant server over the full cartesian product of representative values of every filter: 256 combinations covering eligibility × mandatory-only × bank × document × entity terms and groups.
  - Eligibility and authority are filters, never ranking signals.
- **Ranking:** zero-score chunks are dropped, and ties are ordered by `chunk_id`.
- **Freshness:** `KeywordIndexManager.get()` re-reads a fingerprint on every call and rebuilds the index when it changes. The fingerprint and the rows are read in one REPEATABLE READ transaction, so an index always matches the snapshot its fingerprint describes.
  - **Coverage:** the fingerprint is a SHA-256 over every stored chunk row (parents included; only timestamps left out) plus the document columns that reach payloads (title, file, authority, version, effective dates). It therefore covers indexed text, payloads and citations, filter fields and parent/child links.
  - **Read failure:** if the database read fails, the manager drops its index and raises `KeywordIndexUnavailable`. A stale index is never served.
  - **Pipeline hook:** an `invalidate()` hook in the ingestion pipeline is deferred to M3. CLI processes always build fresh, and the per-query check covers long-lived processes.
- **CLI:**
  - `search QUERY --mode bm25` (also `make search Q=… MODE=bm25`) loads no embedding model and does not contact Qdrant; `--expand` works as in dense mode. An unavailable database exits with code 2.
  - `evaluate --mode bm25` (`make eval MODE=bm25`) runs the development set and reports latency.
  - `--mode` defaults to `dense`, which is the unchanged M1 path. `make check-dense-golden` verifies it against the M1 golden snapshot.
  - `evaluate` refuses the frozen held-out set (`m2_heldout.jsonl`) in every mode (docs/10).
- **Measured in A1** (2026-11-10, Windows laptop, local Docker Postgres, indicative): index build over 429 chunks 0.05 s (target < 1 s; a measured target, not a gate). Query latency p50 ~110 ms, dominated by the per-query content-hash check (~104 ms, hashing about 0.9 MB of row text); BM25 scoring itself ~4 ms. A2 replaced that check (below).

**Freshness check: `xmin` version (M2 A2; `chunk_corpus_version` in `app/db/repository.py`):**
- The manager compares, on every query, a SHA-256 over the row count and the ordered `(id, xmin)` pairs of `chunks` and of `documents`. These are read in one statement, inside the same REPEATABLE READ snapshot as the rows the index is built from.
- **What it detects:** Postgres stamps every row version written by INSERT or UPDATE with the writing transaction's id (`xmin`). So any committed insert, update (even a no-op UPDATE that writes identical values) or delete of a chunk or document row changes the value. It detects everything the A1 content hash detects, plus harmless no-op writes, which only cause an extra rebuild.
- **Tests:** the eight A1 freshness cases pass unchanged, plus INSERT, UPDATE, no-op UPDATE and DELETE of chunk and document rows. A read failure still drops the index and raises `KeywordIndexUnavailable`; a stale index is never served.
- **Limitations:**
  - `xmin` identifies row *versions*, not content, so it is a **runtime freshness check only**, never a persistent version identifier. Don't store or compare it across databases.
  - It is a 32-bit transaction id. In theory, a row rewritten exactly ~2^32 transactions later, with everything else unchanged, could repeat its pair. The row counts narrow this further, and it is unreachable at this project's scale.
  - Freezing (VACUUM) keeps the stored `xmin` (PostgreSQL ≥ 9.4). A dump/restore or replication assigns new values, which only causes a rebuild.
- **Measured:** check p50 6.1 ms / p95 7.1 ms (A1 content hash: ~103 / 116 ms); BM25 search end to end p50 ~11–13 ms.
- **Not changed:** no migration. A trigger-maintained version counter (≈1–2 ms; needs a table and triggers via Alembic) remains a possible later optimisation. `max(updated_at)` was rejected, because raw-SQL updates don't touch it.

**Hybrid retrieval: dense + BM25 with Reciprocal Rank Fusion (M2 A2; `app/retrieval/{fusion,retrieve}.py`):**
- **Modes** (`RetrievalMode`): `dense` (M1 `search()`, unchanged), `bm25` (A1 keyword index, unchanged), `hybrid`. The default comes from `RETRIEVAL_MODE` (default **`dense`** until M2 A3 selects on the development set); `--mode` overrides it.
- **Hybrid:**
  - both retrievers run **sequentially**, each returning `RETRIEVAL_CANDIDATES` (50) hits under the same `SearchFilters`;
  - the two rankings are fused with RRF, `score = Σ 1/(RRF_K + rank)` with `RRF_K` = 60, using ranks only (dense cosine and BM25 scores are not comparable);
  - ties: fused score descending, then best single-retriever rank, then `chunk_id`;
  - these are fixed A2 defaults, not tuned.
- **Score semantics** (`RetrievalResult.score_kind`):
  - `SearchHit.score` is the original **dense cosine** score in `dense` mode, the original **BM25** score in `bm25` mode, and the **RRF fused** score in `hybrid` mode;
  - in a degraded hybrid result, it is the surviving retriever's original score;
  - `RetrievalResult.provenance[chunk_id]` always keeps the original dense rank and score, the original BM25 rank and score (`None` when that retriever didn't return the chunk) and the fused score;
  - `SearchHit` itself is unchanged.
- **Payloads:** hybrid hits carry the Postgres-built payload (no Qdrant `index` field), identical whichever retriever found the chunk. A dense point with no stored chunk is not cited (`RetrievalResult.dropped`). Dense-only mode keeps M1's payloads exactly. Citations, filters (`SearchFilters`, the same for both retrievers) and parent expansion (`--expand`, `evidence_context`) are unchanged.
- **Degraded results:**
  - **both succeed:** a normal fused result;
  - **one fails:** the other's own ranking, `degraded = ("dense",)` or `("bm25",)`, with the error in `failures`;
  - **both fail:** `RetrievalError`.
  - The CLI prints `DEGRADED: <retriever> retrieval failed (...); showing <score kind> results only` and exits `0` for a degraded but valid result, or `2` when every retriever failed. M1 exit-code meanings are unchanged.
  - Single modes never fall back.
  - Evaluations refuse degraded runs (exit 2) rather than score them.
- **CLI and Make:**
  - `search --mode hybrid` prints `scores: rrf` and a `via: dense #r (score) · bm25 #r (score)` line per hit;
  - `evaluate --mode hybrid`; `evaluate --compare` / `make eval-compare` compares dense, BM25 and hybrid on the development set with per-stage latency (freshness, BM25 scoring, query embedding, Qdrant, fusion) and selects nothing;
  - `make search` / `make eval` take `MODE=dense|bm25|hybrid` (empty = `RETRIEVAL_MODE`);
  - `reconcile` adds one informational line (`keyword index (informational): N chunks; Postgres eligible M; Qdrant P`, or `unavailable: …`), which never affects its verdict, repair or exit code.
- **Settings** (`infra/.env.example`): `RETRIEVAL_MODE`, `RRF_K`, `RETRIEVAL_CANDIDATES`, `BM25_STOPWORDS` (M2 A3).

**BM25 tokenizer variant (M2 A3; `STOPWORDS_LUCENE_EN` in `app/retrieval/keyword.py`):**
- **Setting:** `BM25_STOPWORDS=true` keeps the plain v1 tokenizer and then drops the Lucene/Elasticsearch default English stopword list, verbatim (33 words): `a an and are as at be but by for if in into is it no not of on or such that the their then there these they this to was will with`.
- **Registered, not tuned:** the list was fixed before any A3 result was seen and is never edited, because editing it would be tuning.
- **Same tokens for corpus and queries:** the variant applies to both the indexed text and the queries. The default (`false`) is the unchanged plain tokenizer. Each variant has its own index, and the fingerprint names it (`t1s:` against the plain `t1:`).
- **Negation limitation:** the list removes **"no" and "not"**. With it, a query such as "KYC is not mandatory" matches lexically like "KYC mandatory". This affects BM25 ranking only: evidence text, payloads and citations are untouched, and claims are checked later (M4). Whether the variant is used at all is decided by the A3 selection (docs/10).
- **Selection harness:** `evaluate --select` / `make eval-select` evaluates the seven registered configurations on the development set and applies the pre-registered rule (`app/retrieval/selection.py`, docs/10).
- **Measured on the development set:** see docs/10 (A2 comparison).

## Milestone-1 minimal slice
Accept PDFs -> extract w/ page refs -> embed -> index in Qdrant -> query returns top-5 passages with citations. Measure Recall@5 on a small hand-labeled set. **Done (M1 Stage F):** `make ingest`, `make search`, `make eval` (Recall@5 0.825 on 20 queries; docs/10).
