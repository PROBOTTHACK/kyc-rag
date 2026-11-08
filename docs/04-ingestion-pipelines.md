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
- Commands (`cd backend && ../.venv/Scripts/python -m app.ingestion.cli …`): `ingest [--document KEY]... [--force]`, `reconcile [--repair [--delete-unknown]]`, `runs [--document KEY] [--failed] [--limit N]`.
- Reuse: the functions take a sync SQLAlchemy `Session` (`app.db.session.create_sync_sessionmaker`). Async FastAPI code can call them through `AsyncSession.run_sync`. `ingest_document(..., prepared=...)` accepts an already-prepared document.
- Measured on the corpus (2026-11-08): 9 documents and 135 pages (5 OCR) stored. 456 chunks: 332 single, 27 parent, 97 child; 429 eligible. 774 document exclusions. All 27 families intact. Postgres and Qdrant both hold 429 eligible chunks/points, consistent in every direction. The first `ingest` on the existing Stage D collection re-embedded 0 points and overwrote 0 payloads, because the rows rebuild byte-identical payloads. A second `ingest` wrote 0 rows.

## Milestone-1 minimal slice
Accept PDFs -> extract w/ page refs -> embed -> index in Qdrant -> query returns top-5 passages with citations. Measure Recall@5 on a small hand-labeled set.
