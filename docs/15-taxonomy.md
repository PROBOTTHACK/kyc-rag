# 15 — Canonical Vocabularies

Code: `backend/app/schemas/taxonomy.py`. The manifest, Postgres CHECK constraints and Qdrant payloads all use these values. Adding a value requires a new Alembic migration, because migrations snapshot the allowed values.

## Entity type: the customer's legal form
| Value | Meaning | Main corpus support |
|---|---|---|
| `INDIVIDUAL` | natural person | RBI Directions VI-A; FAQ; HDFC forms |
| `SOLE_PROPRIETORSHIP` | sole proprietary firm | RBI VI-B ¶30–32 |
| `PARTNERSHIP` | partnership firm | RBI ¶34 |
| `LLP` | limited liability partnership | **no RBI CDD paragraph**; HDFC non-individual form lists it |
| `COMPANY` | company (public/private) | RBI ¶33 |
| `TRUST` | trust | RBI ¶35 |
| `HUF` | Hindu undivided family | RBI definitions only; HDFC HUF declaration |
| `SOCIETY` | society (incl. co-operative) | RBI ¶37 (named) |
| `UNINCORPORATED_ASSOCIATION` | AOP / body of individuals / club | RBI ¶36 |
| `OTHER_JURIDICAL_PERSON` | other juridical persons (local bodies, universities, government bodies, …) | RBI ¶37 |

**Groups** are expansion sets for queries. They are never stored, and the manifest rejects them.
- `NON_INDIVIDUAL` = every type except `INDIVIDUAL`.
- `LEGAL_ENTITY` = `PARTNERSHIP, LLP, COMPANY, TRUST, SOCIETY, UNINCORPORATED_ASSOCIATION, OTHER_JURIDICAL_PERSON`.

Group membership is a retrieval convenience only. It does **not** mean an RBI paragraph applies. In particular, LLP membership says nothing about whether ¶34 or ¶37 governs, so that stays `UNCLEAR`/`CONDITIONAL`.

**Document-level rule:** a document's `entity_types` lists only the legal forms it names explicitly. Generic wording ("legal entity", "customer") adds nothing at document level. Chunk-level tagging (Phase 3) may narrow a document's types but never widen them.

## Account type (workflow intake, M3 S3)
`AccountType`: `SAVINGS_ACCOUNT`, `CURRENT_ACCOUNT`, `NRE_ACCOUNT`, `NRO_ACCOUNT`, `TERM_DEPOSIT`.
- **Why it is closed:** a closed vocabulary means intake never accepts a free-form model value.
- **Where it comes from:** docs/07 (current account), and the HDFC forms (savings/current, and the NRI "NRE/NRO Account-cum-Term Deposit" form).
- **Storage:** it is not stored in any database column, so adding a value needs no migration.
- **What it does not mean:** an account type selects nothing by itself, and it is not a claim about which rules apply.

**Which intake fields are required:** `entity_type` and `account_type` always; `residency` only for `INDIVIDUAL`.
- Residency is defined for individuals only, so for other entities it stays `unknown` and is never assumed.
- Each value records its source:
  - `user_stated`: read from the question by the intake model, with the exact supporting words in `evidence_span`. Those words must occur in the question and contain a keyword for that value (`VALUE_KEYWORDS` in `app/graph/intake.py`, S3.1); other wording fails safe to a clarification question;
  - `user_answered`: a structured clarification answer;
  - `unknown`.

## Residency status (separate axis)
`RESIDENT`, `NRI` (Non-Resident Indian), `PIO` (Person of Indian Origin; the HDFC forms group OCI cardholders here). An NRI customer is `entity_type=INDIVIDUAL` with `residency=NRI`, never an entity type of its own. An empty list on a document means "not specified".

## Two statuses, never merged
| Field | Applies to | Values |
|---|---|---|
| `source_status` | a source document | `active` (current per corpus), `dated`, `superseded`, `unknown` (currency cannot be verified, e.g. all HDFC forms) |
| `applicability` | a requirement (M3+; never on chunks) | `MANDATORY`, `CONDITIONAL`, `OPTIONAL`, `UNCLEAR` |

`source_status` keeps the docs/14 values and adds `unknown`. The requirement field keeps the docs/03 name `applicability`. `UNCLEAR` (CLAUDE.md) is the single term for "applicability cannot be established" across code, specs and docs.

## Evidence eligibility (derived, filter not rank)
| Condition | `evidence_eligibility` |
|---|---|
| `document_type = reference` | `context_only`: excluded from compliance retrieval by default |
| `regulatory`/`policy` and `source_status = active` | `primary`: may support MANDATORY |
| anything else (forms, annexures, dated/superseded/unknown sources) | `supporting`: evidence, never sufficient alone |

## Form requirements are not obligations
A form shows what the form *requests* (a field, a checkbox, an instruction such as "attach self-attested passport, visa & address proof"). That is evidence of the form's content. It is not proof of a current regulatory or bank-policy obligation (docs/05 states (a)–(c)).
- `document_type=form` is never `primary`, whatever its `source_status`. Forms whose current validity cannot be verified are `source_status=unknown`, and they stay retrievable as `supporting` historical or reference evidence.
- A requirement built only on form evidence can be `CONDITIONAL`/`UNCLEAR`, never `MANDATORY`. Reaching `MANDATORY` needs a `primary` source: active RBI regulatory text or verified active bank policy (enforced since M3 S5 by `app/graph/validation.py`; M4 additionally requires the primary citation itself to support the obligation, docs/milestones/M4_PLAN.md).

## `bank`, `source_authority`, `attribution_basis`: three different questions
| Field | Question it answers | Example |
|---|---|---|
| `bank` | Which corpus does this belong to for the source hierarchy? `RBI` = regulator corpus; `HDFC` = the **target bank / project context**. It is **not** a claim about who published the file. | `hdfc_kyc_aml_faq` is `bank: HDFC` because it was collected as HDFC material |
| `source_authority` | Who published it, **verified**? `null` until the document itself shows it. | `null` for `hdfc_kyc_aml_faq`, whose text never names HDFC |
| `attribution_basis` | How the publisher was established: `document_text`, `document_visual`, or `filename_only` | `filename_only` → `source_authority` must stay `null` |

Rule: when `source_authority` is known it must equal `bank`. The corpus is restricted to RBI and HDFC sources (CLAUDE.md), so a verified publisher outside the document's corpus means the document is misfiled; it is rejected rather than stored inconsistently.

## Document identity and versions (M1 limitation)
- `document_key` = what the document is; `sha256` = the exact bytes. Row ids derive from `sha256`.
- **M1 holds one version per `document_key`.** This is enforced in three places: the manifest validator (duplicate key = error), Postgres (`uq_documents_document_key`), and the SHA-256 check (changed bytes under an existing key = error).
- Consequences: a new edition is **never** silently overwritten, merged or assumed to supersede the old one. Re-ingesting identical bytes is idempotent (same row and chunk ids). Changed bytes stop validation until a person records the relationship.
- To add a new edition today: give it its own `document_key`, mark the old entry `superseded` with a `status_reason`, and list it in the new entry's `supersedes`. True multi-version history under one key needs a later migration that relaxes the unique constraint.

## Chunking vocabularies (Stage C)
| Field | Values |
|---|---|
| `chunking_profile` (manifest) | `rbi_directions`, `rbi_faq`, `question_headings`, `form` (default by `document_type` when unset) |
| `chunk_role` | `single` and `child` (embedded, indexed), `parent` (full long paragraph; Postgres only) |
| `content_kind` | `paragraph`, `preamble`, `qa`, `table_row`, `form_text`, `form_fields` |
| `region` | `left` / `right` half of a fold-out page; null = whole page |

| `text_reliability` | `native` (PDF text layer), `ocr_reliable`, `ocr_uncertain` |
| `embedding_eligible` | `false` for parents and for OCR text a document's policy excludes; else `true` |
| `can_support_mandatory` | derived: `evidence_eligibility = primary` **and** `text_reliability != ocr_uncertain` |

## OCR evidence policy (Stage C)
Three separate questions, never merged:
1. **Preservation.** All OCR output is kept: the raw recognition result (OCR cache, processed page), each chunk's `raw_text`, and the lines left out of its content (`excluded_lines`, with confidence). Nothing is overwritten or discarded.
2. **Embedding eligibility.** Parents are never embedded. Uncertain OCR chunks are embedded by default, so a question about, for example, an HUF declaration can still find them; set `OCR_EMBED_UNCERTAIN=false` or a manifest entry's `embed_uncertain_ocr: false` to keep them out of the vector index.
3. **Evidence.** Uncertain OCR text can never back a MANDATORY requirement (`can_support_mandatory = false`), whatever the document's eligibility. Downstream retrieval and verification must check `text_reliability` and `can_support_mandatory`, not only `evidence_eligibility`.

`ocr_reliable` requires **both**: every page the chunk spans has status `ok` (Stage B page mean word confidence ≥ 70), **and** the chunk's own mean line confidence ≥ `OCR_RELIABLE_MIN_CONFIDENCE` (default 0.80; a manifest entry may set `ocr_reliable_min_confidence`). The 0.80 default is a setting, not a universal truth. It was chosen from the measured corpus, and per-document overrides exist for when a source needs a different bar:

| Document / page | Page status | Chunk confidence | Result |
|---|---|---|---|
| NRI re-KYC p3 (acceptable-document list) | ok (0.93) | 0.92–0.96 | `ocr_reliable` |
| NRI re-KYC p2 (address change, notes) | ok (0.78) | 0.84–0.85 | `ocr_reliable` |
| NRI re-KYC p1 (field labels) | ok (0.75) | 0.78 | `ocr_uncertain` |
| Non-individual fold-out p1–p2 | `ocr_low_confidence` (0.60–0.64) | 0.55–0.83 | `ocr_uncertain` (page flagged) |

In this corpus no OCR chunk can support MANDATORY in any case: both scanned documents are forms (`supporting`). The policy still matters, for any future scanned regulatory text and for ranking and display.

## Other vocabularies
`bank`: RBI | HDFC · `document_type`: regulatory | policy | form | annexure | reference · `attribution_basis`: document_text | document_visual | filename_only (a `source_authority` cannot be set from a filename) · `version_review_status`: not_required | needs_review | confirmed · `ingestion_status` (document and run): pending | processing | completed | partial | failed (`pending` = stored in Postgres but not yet indexed; `completed` only when every stage succeeded; `partial` = all stages ran but some pages failed and are recorded) · `quality` (derived at ingestion): text (all pages native) | scanned (all read pages OCR) | mixed · `extraction_method`: text | ocr · page `status`: ok | low_quality | ocr_low_confidence | failed · ingestion stage (`ingestion_runs.failed_stage`, Stage E): extraction | ocr | chunking | persistence | embedding | indexing · stage status (`ingestion_runs.stages`): ok | failed | skipped.

Postgres enforces the chunk vocabularies above (`chunk_role`, `text_reliability`) with CHECKs since migration `0002_m1_persistence`. It also enforces the cross-field rules: exactly the children have a parent, parents are never embedding-eligible, `text` extraction ⇔ `native`, and the `can_support_mandatory` rule.
