# 04 — Ingestion Pipelines

Ingestion must be independently executable: `python -m app.ingestion.cli run --path data/raw_pdfs` (also via queue + `POST /ingestion/run`). Idempotent by file SHA-256.

## Step 0 — Classify documents
For each source PDF record: filename, hash, category (`policy` | `regulatory` | `form` | `annexure` | `reference`), source authority (null until verified), entity types covered, quality (text/scanned/mixed). **Inspect real PDFs first.**

## Pipeline A — Policy & regulatory documents
PDF validation -> PyMuPDF extraction (text, page numbers, metadata, layout) -> text-quality check -> (poor/scanned -> OCR fallback) -> section & table extraction -> semantic chunking -> metadata assignment -> BGE embeddings -> Qdrant upsert + Postgres chunk rows (source of truth; BM25 index is rebuilt from them).

**Chunking rules:** don't cut blindly at N chars. A chunk should hold heading + complete statement + conditions + exceptions + source metadata. Use parent–child chunking when a requirement spans paragraphs (retrieve child, supply parent to synthesis). Keep page number on every chunk.

**Chunk metadata:**
```json
{"document_id":"doc_001","document_name":"...","bank":"HDFC","document_type":"policy",
 "source_authority":null,"entity_types":["LLP"],"section":"...","page_number":12,
 "effective_from":null,"effective_until":null,"version":"1.0","content":"..."}
```
Never invent dates or authority; unknown = `null`.

## Pipeline B — Forms and applications
Form PDF -> page extraction -> page images (rendered) -> multimodal analysis -> structured JSON -> Pydantic schema validation -> (optional human review) -> approved schema -> reference store (Postgres `form_schemas`, cached in Redis).

Extract: sections, fields (name/type/required-as-shown/page), checkboxes, signature blocks, declarations, annexure references, entity applicability. Keep original page ref per field.

**Critical distinction:** a field on a blank form ≠ legally mandatory. Store `appears_on_form` separately from `mandatory_under_policy` (the latter only set via rules layer / verified policy).

**Versioning:** store form_id, version, file hash, extraction timestamp, review status, entity categories. A new form version never silently merges with an old one.

## Milestone-1 minimal slice
Accept PDFs -> extract w/ page refs -> embed -> index in Qdrant -> query returns top-5 passages with citations. Measure Recall@5 on a small hand-labeled set.
