# M1 Plan: Document Ingestion (Pipeline A)

> **Status: complete.** Phases 3–5 were delivered as Stages A–F. What was built and measured is in [`M1_HANDOFF.md`](M1_HANDOFF.md); this plan is kept as the record of the decisions.

Phase 1 (corpus inspection) is approved: see `data/processed/corpus_report.md`.
This plan records the decisions from that review. Phase 2 is detailed below. Phases 3–5 are only outlined and get their own review.

## Decisions carried into M1
| # | Decision | Where it lands |
|---|---|---|
| 1 | Tesseract is the OCR engine, installed as a system binary (`winget install UB-Mannheim.TesseractOCR`), and called through PyMuPDF's built-in integration. No Python binding. | Phase 2 install check, Phase 3 `ocr.py` |
| 2 | Add `pymupdf`, `sentence-transformers` and `pyyaml` to `pyproject.toml`. Reuse the existing venv; torch comes as a transitive CPU wheel. | Phase 2 |
| 3 | The RBI FAQ is `dated` supporting evidence and is not automatically superseded. | manifest |
| 4 | LLP and dormant-account requirements stay `CONDITIONAL`/`UNCLEAR` when the corpus doesn't establish them. | manifest `coverage_notes`; enforced by the rules layer (M4) |
| 5 | Index all 9 PDFs, each with a `document_type`. | manifest |
| 6/7 | `source_status`, `document_type` and `bank` are carried identically by the manifest, `documents` and `chunks` rows, and the Qdrant payload. | `app/schemas/corpus.py` |
| 8 | Commit `corpus_report.md` through a single narrow `.gitignore` exception. | `.gitignore` |

## Refinements (A–G)
- **A. Two separate statuses.**
  - `source_status` (a property of a document) takes the docs/14 values `active | dated | superseded` plus `unknown`. `active` is the spec's word for "current".
  - Requirement status follows the spec's field name, `applicability`, with values `MANDATORY | CONDITIONAL | OPTIONAL | UNCLEAR`. The enum is defined now and used from M3. Chunks never carry it, because they are evidence, not requirements.
  - `UNCLEAR` is the only term (it replaced `UNDETERMINED` in docs/00, docs/03, docs/13 and the README).
- **B. Migrations.** M0 has no revisions; its database holds only an empty `alembic_version`. So the M1 revision `0001_m1_corpus` is the first one, and no M0 file is edited. It is verified both on a fresh database and on the running M0 database, including `downgrade base` and a second `upgrade`.
- **C. Identity ≠ content.**
  - `document_id` is the logical identity: a stable slug set in the manifest. `sha256` identifies the file content. A `documents` row is one (document_id, sha256) pair.
  - The manifest pins `sha256`. If the hash on disk differs, manifest validation fails and the file is not ingested, neither as a new version nor as a replacement, until a person updates the manifest.
  - `supersedes` relationships are only taken from explicit manifest entries.
  - M1 limitation: one version per `document_key`, enforced by the validator and `uq_documents_document_key` (docs/15).
- **D. Taxonomy.** `EntityType` is the customer's legal form; group names such as `LEGAL_ENTITY` are expansion sets, not stored values. `ResidencyStatus` is a separate axis. See `docs/15-taxonomy.md`.
- **E. Pages.** A new `document_pages` table records extraction method, quality metrics, OCR confidence and failure status for every page. The table is populated in Phase 3.
- **F. Eligibility.**
  - `evidence_eligibility` (`primary | supporting | context_only`) is derived deterministically from `document_type` and `source_status`. It is stored and indexed, so retrieval filters on it rather than re-ranking.
  - `reference` documents are `context_only` and are excluded from compliance retrieval by default.
- **G. Git.** Add only `!data/processed/corpus_report.md`.

## Phase 2 deliverables (this phase)
1. `app/schemas/taxonomy.py`: the enums for entity types and groups, residency, document type, bank, source status, applicability, evidence eligibility and attribution basis.
2. `app/schemas/corpus.py`: Pydantic models `ManifestEntry`, `Manifest`, `DocumentMetadata`, `ChunkMetadata`, plus the eligibility rule.
3. `app/ingestion/manifest.py`: YAML loading and validation, including file existence, SHA-256 match, uniqueness, unlisted PDFs and cross-field rules. `app/ingestion/cli.py validate-manifest` runs the check.
4. `data/raw_pdfs/manifest.yaml`: 9 entries, with unknown values left `null`.
5. ORM models plus Alembic `0001_m1_corpus`: `documents`, `document_pages`, `chunks`, check constraints and indexes.
6. Settings `CORPUS_DIR` and `CORPUS_MANIFEST`; the dependency updates; the Tesseract install.
7. Tests: unit tests for taxonomy, manifest and schemas; integration tests for the migration on fresh and M0 databases.
8. Docs: `docs/15-taxonomy.md`, and updates to docs/08 and docs/14 (LLP correction, field names).

## Later phases (outline only; separate review)
- **Phase 3:** Pipeline A (validate → extract → quality → OCR → clean → chunk → enrich → embed → Qdrant + Postgres, idempotent).
- **Phase 4:** ingest and search CLIs with the eligibility filter.
- **Phase 5:** tests, ≥15 labelled queries, Recall@5, and the M1 handoff.
