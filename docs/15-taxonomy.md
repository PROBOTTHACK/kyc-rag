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
- A requirement built only on form evidence can be `CONDITIONAL`/`UNCLEAR`, never `MANDATORY`. Reaching `MANDATORY` needs a `primary` source: active RBI regulatory text or verified active bank policy (enforced in M4 output validation).

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

## Other vocabularies
`bank`: RBI | HDFC · `document_type`: regulatory | policy | form | annexure | reference · `attribution_basis`: document_text | document_visual | filename_only (a `source_authority` cannot be set from a filename) · `version_review_status`: not_required | needs_review | confirmed · `ingestion_status`: pending | processing | completed | partial | failed · `quality`: text | scanned | mixed · `extraction_method`: text | ocr · page `status`: ok | low_quality | ocr_low_confidence | failed.
