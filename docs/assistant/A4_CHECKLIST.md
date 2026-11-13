# A4: Evidence-Backed Checklist Generation

Stage A4 of the HDFC Bank Account Opening Assistant (independent portfolio prototype; not an HDFC Bank or RBI product). Spec: `A1_PRODUCT_SPEC.md` §3 (classes), §4 (documents vs information), §7 (fallback and notice) and §8 (output and citations), with the A2 rulings of §5.4.

**Scope of this stage:**
- **Built:** the backend checklist engine and its tests.
- **Not built:** no API, persistence or frontend (A5/A6). No LangGraph and no LLM.
- **Unchanged:** the coverage decisions (`coverage.yaml` a2-2), the retrieval default (dense) and the A3 dialogue.

## 1. Architecture and data flow
```
ReadyProfile (A3)
  │ 1 validate   ChecklistService.validate: complete? agrees with coverage.yaml (recomputed)?
  │              NOT_ESTABLISHED → prescribed fallback (no evidence is read at all)
  │ 2 select     checklist.yaml items for scope × category × account × facets (+ asked topics)
  │ 3 gather     each item's cited chunk ids
  │              + optional AssistantRetriever (HDFC-only, scoped, dense by default):
  │                only chunks containing one of the item's exact, distinctive quotes;
  │                retrieval adds references, never items
  │ 4 resolve    PostgresChunkSource → existing repository get_chunks (one query)
  │   validate   per citation: stored? HDFC? permitted source of this cell? same document and
  │              page? quote verbatim in stored text? → otherwise dropped (diagnostics)
  │ 5 classify   classify(source role): opening/conversion form → form_indicated (code only)
  │   assemble   A documents/declarations · B information · C could not be confirmed
  ▼
ChecklistAnswer (Pydantic; checklist | fallback | technical_error)
```

| File | Role |
|---|---|
| `backend/app/assistant/checklist.yaml` | **Item catalogue** (data, version `a4-1`): 47 items and 4 not-confirmed entries.<br>- Each item cites stored chunk ids with an exact quote, its document and page, and an optional verbatim condition, facet constraints (`when`) and topics.<br>- Also holds the fixed checklist wording: class labels, caveats, applies-if notes, unknown-topic titles, coverage note, and the fallback, notice and technical message verbatim. |
| `backend/app/assistant/catalogue.py` | Loads and validates the catalogue against the coverage map (details below). Fails closed. |
| `backend/app/assistant/chunks.py` | `StoredChunk` and `PostgresChunkSource`. Reuses `app.db.repository.get_chunks`. |
| `backend/app/assistant/checklist.py` | `ChecklistService` (validate → select → gather → resolve/validate → classify/assemble), `classify()`, and the answer models. |

**What the catalogue loader refuses:**
- an opening item citing anything but an `opening_form` that is a source of every covered cell the item claims;
- a conversion item citing anything but the conversion form;
- a re-KYC, legacy, not-used, RBI or unknown document;
- a condition that is not verbatim text of the item's quote;
- an item disagreeing with an A2 ruling (kind, categories, condition, cited chunks);
- a ruling that is unused, or used twice;
- any extra field, e.g. a `classification`.

## 2. Evidence rules as implemented
- **Classification** is assigned by `classify()` from the basis document's role only:
  - `opening_form` / `conversion_form` → `form_indicated`;
  - natural assumptions with no source (rulings, PAN card copy) → `unconfirmed`;
  - asked topics with nothing → `unknown`, with the fallback sentence.
  - `policy_supported` is reachable only from a `policy` role, which no corpus document has. `legacy_reference` is never emitted, because legacy documents can't be cited.
- **Citations come from Postgres:** document title, page, section, text reliability, extraction method and date basis are copied from the stored row and the coverage map, never from retrieval payloads.
  - `chunk_id` is internal and excluded from serialization (A1 §8.2.6).
  - Diagnostics (dropped items, retrieval status) are excluded too.
- **Quotations (rule 3a):**
  - `source_wording` (an exact quotation) is set only when every cited chunk is `native` / `ocr_reliable`.
  - Otherwise the item carries `approximate_reading` plus the warning *"Text read automatically from a scanned page; it may contain errors"* and the `scanned_text` caveat.
  - Every item on the scanned non-individual form is therefore an approximate reading.
- **Conditions stay conditional:** "If PAN not available" (Form 60), "for existing customers" (Form 60/61), "Mandate / POA", "Yes, I/ We wish to nominate", "only face to face" (FCNR) and others are carried verbatim, with a `conditional` caveat.
- **Facets:**
  - A known, different value removes a section (single holder: no second-applicant items).
  - An unknown, skipped or not-asked value keeps it, with an "Applies if…" note.
- **Duplicates:**
  - A chunk cited twice becomes one citation.
  - Retrieval adds a chunk only if it holds an item quote of at least 24 characters (`basis: false`), so short labels like "Date of Birth" never pick up unrelated sections (found and fixed while testing on real data).
  - Identical wording in another section (e.g. the second applicant's Form 60 line) is kept as an additional reference.
- **Failures:**
  - An item whose cited evidence fails validation is dropped. If the customer asked about it, it becomes `unknown` with the fallback.
  - If nothing survives, the answer is the fallback (A1 §7 case 3).
  - A storage failure gives the technical message (A1 §7), never the fallback.
  - A retrieval failure keeps the cited evidence and marks retrieval degraded.
  - An out-of-scope retrieval result (`AssistantScopeError`) disables retrieval for that build.
- **Notices:** every answer carries the dated-document notice. Checklists also carry the coverage note: built only from the form copies held here; not a confirmed statement of HDFC Bank's current requirements, not an official document, not a KYC approval. Each item carries the `dated_source` caveat from the coverage map's date basis.

## 3. A2 rulings in the checklist (regression-tested for every organisation × CURRENT/FD/RD)
| Ruling | Checklist outcome |
|---|---|
| `org_signatory_photograph` | Never in Group A or B. Group C `unconfirmed` "Photographs of authorised signatories", no citation. |
| `org_entity_pan_card_copy` | Group C `unconfirmed`, no basis citation; the PAN No. field is shown only as related context. |
| `org_signatory_pan_number` | Group B `form_indicated` information, citing `f51508ae…` and `600f4dd7…` (OCR-uncertain). |
| `org_form_60_61` | Group A `form_indicated` document, condition "for existing customers", citing `1c8150b4…` (OCR-uncertain). |

The NRI and conversion PAN-card-copy assumptions follow the same pattern (A1 §4 rule 1).

## 4. Example output (real database; Company × Current, the customer asked about photographs and a PAN card copy)
| Group | Class | Item | Basis |
|---|---|---|---|
| A | form_indicated | Form 60/61, for existing customers | non-individual form p1, OCR-uncertain; condition "for existing customers" |
| B | form_indicated | Type of entity | p1, OCR-uncertain |
| B | form_indicated | Details of each authorised signatory | p2, OCR-uncertain |
| B | form_indicated | PAN number of each authorised signatory or partner | p2 (two chunks), OCR-uncertain |
| B | form_indicated | Mobile number | p1, OCR-uncertain |
| C | unconfirmed | Photographs of authorised signatories (asked) | none: A2 ruling |
| C | unconfirmed | A copy of the organisation's PAN card (asked) | none: A2 ruling; PAN No. shown as related context |

Resident individual × Savings returns `kind: fallback` with:
- the fallback verbatim;
- the coverage map's reason;
- the notice;
- no groups and no sources.

## 5. Tests
| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_assistant_checklist.py` | 113 | **Sections and combinations:** every section; all 26 covered cells plus the conversion; 5 unsupported combinations (no evidence read).<br>**Classification:** follows the source role only; never policy-supported or legacy; no class in the catalogue.<br>**PAN and Form 60:** PAN number vs card copy; Form 60/61 and Form 60 conditions.<br>**OCR:** no quotations; unreliable text downgraded.<br>**Exclusions:** catalogue refuses re-KYC, legacy, not-used, RBI and conversion sources and another category's form; conversion and opening never mix; forbidden, non-HDFC and leaked chunks refused at runtime.<br>**Evidence handling:** duplicates and distinctive-quote retrieval; missing, moved and unquoted evidence dropped; asked item becomes `unknown`; insufficient evidence gives the fallback; storage failure gives the technical message; retrieval failure and scope errors.<br>**Profile and topics:** facets and applies-if; scheme sections; on-request items; asked and unknown topics; invalid profiles (9 kinds).<br>**Rulings and output:** the A2 rulings for 21 organisation cells and 7 ruling-contradiction mutations; determinism without any LLM; serialization hides ids and diagnostics; no approval language. |
| `tests/integration/test_assistant_checklist_dev.py` | 2 | Every catalogue quote is verbatim stored HDFC text on the claimed document and page, with OCR status matching the form. Every covered cell and the conversion build on real Postgres and the real dense retriever with nothing dropped, only permitted sources, and no quotable scanned text. |

A guard check during development: removing the quote check or the permitted-source check makes these tests fail. The leaked-chunk test was added because the source check was not yet covered on its own.

## 6. Checks run for this stage
| Check | Result |
|---|---|
| `make lint` (ruff, black, mypy on 140 files) | clean |
| `mypy --strict app/assistant` | clean (14 files) |
| `make test` | **1865 passed, 1 failed: K7** (`test_groq_provider::test_live_calls_and_cache_are_off_by_default`; local `.env`). Known baseline; no new failures. |
| `make check-schema` | no new upgrade operations (no schema change) |
| Integration: `test_assistant_checklist_dev.py`, `test_assistant_retrieval_dev.py`, `test_hybrid_dev.py` | 7 passed |
| Integration: `test_dense_golden.py` | failed at m1-03: **K8**, the known tied-score baseline |
| `make assistant-eval MODE=all` | unchanged: dense 0.733, bm25 0.613, hybrid 0.793 (the 25-case, single-annotator, development-only set) |

## 7. For owner review
1. **The item catalogue is new, evidence-level content.**
   - Its 47 items were chosen from the forms' stored text and checked verbatim against Postgres.
   - Titles are plain-language descriptions; notes say what a field does not establish.
   - It changes no coverage cell, but it is what customers will read, so it needs your review like the coverage map.
2. **Group C shows the not-established rulings proactively** (signatory photographs, PAN card copy) for the categories they apply to, whether or not the customer asked. A1 allows this ("requirements that could not be confirmed"); the alternative is to show them only when asked.
3. **Re-KYC context is not shown at all.** A1 permits it as Group C `unconfirmed` context; A4 omits it, following this stage's instruction never to use re-KYC material for account opening.
4. **The scanned form yields few items** (5–7 per organisation checklist), because most of its OCR text is too garbled to quote. That is the honest result of "evidence or nothing".
5. **`reason` on a fallback is the coverage map's internal wording.** It mentions "corpus" and "A0 amendment 2", so A5/A6 should decide whether to show it.
6. **No defects were found in A2 or A3.**

## 8. Demo path
```
cd backend
../.venv/Scripts/python -m pytest tests/unit/test_assistant_checklist.py
../.venv/Scripts/python -m pytest -m integration tests/integration/test_assistant_checklist_dev.py
```
