# A2: Coverage Map and HDFC-only Retrieval Evaluation

Stage A2 of the HDFC Bank Account Opening Assistant (independent portfolio prototype; not an HDFC Bank or RBI product). Spec: `A1_PRODUCT_SPEC.md`. Corpus audit: `A0_AUDIT.md`.

**Scope of this stage:**
- **Built:** the machine-readable coverage map, the HDFC-only scoped retrieval, the evaluation set and its runner, and tests.
- **Not built:** no conversation logic, API, persistence or frontend (A3–A6).
- **Left untouched:** the legacy M0–M5 code and data.

## 1. Clarifications incorporated into A1 (owner, 2026-11-14)
- **R4, clarification UX (A1 §6):** every question offers:
  - "I'm not sure": the slot stays unknown, with an explanation;
  - "Skip": only for non-essential slots;
  - "What does this mean?": a fixed explanation, then the same question again.

  There are no forced repeat attempts and no loops. If an essential slot (account type or customer category) stays unknown, the assistant explains why a reliable checklist can't be generated.
- **Rule 3a, quotation quality (A1 §8.2):**
  - Only `native` / `ocr_reliable` text may be shown as an exact quotation.
  - `ocr_uncertain` text, and uncorroborated multimodal-schema labels, are never shown as a verified quotation. They carry the label *"Text read automatically from a scanned page; it may contain errors"*.
  - `AssistantHit.quotable` implements the retrieval side of this rule.

## 2. What was built
| File | Purpose |
|---|---|
| `backend/app/assistant/taxonomy.py` | Enums: customer category, account type, intent, facet, coverage state, source role, evidence scope; scope → allowed source roles. |
| `backend/app/assistant/coverage.yaml` | The coverage map (version `a2-1`): source documents with roles, 26 covered cells, the conversion entry, and the reasons cells are not established. Every covered cell cites stored chunk ids (88 evidence references). |
| `backend/app/assistant/coverage.py` | Pydantic models and loader. **Fails closed:** the map does not load if a covered cell would rest on context, legacy, conversion, unknown or non-HDFC sources, lacks evidence, or claims `COVERED` for a scanned source. |
| `backend/app/assistant/retrieval.py` | `AssistantRetriever`: `opening()`, `conversion()`, `context()`, `legacy()`. Each scope reads only its coverage-map documents. |
| `backend/app/assistant/evaluation.py`, `eval.py` | Evaluation set loader, scoring, Markdown and JSON reports; `make assistant-eval [MODE=dense|bm25|hybrid|all]` (read-only). |
| `data/eval/assistant_retrieval.jsonl` | 33 cases: 25 with gold evidence and 8 whose correct result is no evidence. |
| `data/eval/assistant_retrieval_a2_record.json` | Per-case record for all three modes (this run). |

### Coverage map (reproduces the A0 matrix)
| Category | Savings | Current | Salary | FD | RD | FCNR/RFC |
|---|---|---|---|---|---|---|
| NRI individual | COVERED | COVERED | – | COVERED | COVERED | COVERED |
| 7 organisation categories (sole proprietorship, partnership, LLP, company, trust, HUF, other) | – | COVERED_OCR | – | COVERED_OCR | COVERED_OCR | – |
| Resident individual | – | – | – | – | – | – |

- **–** = `NOT_ESTABLISHED`: no applicable HDFC opening document. Each such cell carries a stated reason, and no retrieval runs for it.
- **Material facets:** `nri_scheme`, `holding` and `minor` apply to NRI cells only, because only the NRI form has those sections.
- **Resident → NRO conversion** is `CONVERSION_ONLY` (the conversion form).
- **Context and legacy material:** re-KYC documents are `context`, and `hdfc_kyc_aml_faq` is `legacy_reference`. Both are reachable only through their own scopes, never through the opening scope.

### HDFC-only retrieval (two independent guards)
1. **Before the search:** the filters are always `banks=("HDFC",)` plus exactly the scope's document keys, applied in Qdrant and before BM25 scoring. If a scope has no documents (a `NOT_ESTABLISHED` cell), no search runs at all.
2. **After the search:** every hit is checked. If a hit has a non-HDFC bank, a document outside the scope, the wrong source role, or no page, the call raises `AssistantScopeError`. Such a hit is never passed on.

Every hit keeps its document key, title, type, source role, page / page end, section, extraction method, text reliability and OCR confidence. Scores stay internal.

## 3. Evaluation set
- **25 evidence cases:**
  - NRI: NRE/NRO savings and current, FD, RD, FCNR, joint, minor, passport, PAN / Form 60, photograph, savings for business use, Aadhaar;
  - organisations: LLP (2), company (2), partnership, HUF, sole proprietorship, trust;
  - 1 conversion case and 2 context (re-KYC) cases.
- **8 no-evidence cases:**
  - **`absent_in_scope` (4):** the cell is covered but no stored text answers the question:
    - a2-06: PAN card **copy**, NRI;
    - a2-15: NRE vs NRO difference;
    - a2-19: LLP PAN card **copy**;
    - a2-25: company signatory photographs.
  - **`not_established` (4):** the combination isn't covered, so no search may run:
    - a2-27: resident savings;
    - a2-28: salary;
    - a2-29: resident current;
    - a2-30: LLP savings.
- **Gold chunks only where the corpus has them.** No case has manufactured gold. The dev-stack test checks that every gold chunk and coverage-map evidence chunk exists in Postgres, is HDFC, and is in the claimed document and on the claimed page. It also checks schema-field evidence against `form_schemas`, including its corroboration flag.
- **Limits:**
  - One annotator; development set only; no held-out split.
  - 25 scored cases, so one case moves overall Recall@5 by up to 0.04.
  - Per-category groups with 1–2 cases are anecdotal.

## 4. Results (k = 5, real BGE-small model, development stack)
Command: `make assistant-eval MODE=all`.

### Overall (25 cases with gold evidence)
| Mode | Recall@5 | Hit@5 | MRR@5 |
|---|---|---|---|
| **dense (default)** | **0.733** | 0.880 | 0.599 |
| bm25 | 0.613 | 0.800 | 0.653 |
| hybrid (RRF) | 0.793 | 0.920 | 0.727 |

### Recall@5 by source type
| Source type | Cases | dense | bm25 | hybrid |
|---|---|---|---|---|
| opening_form (NRI, native text) | 14 | 0.631 | 0.488 | 0.702 |
| scanned_opening_form (non-individual, OCR) | 8 | 0.812 | 0.750 | 0.875 |
| conversion_form | 1 | 1.000 | 0.500 | 1.000 |
| context (re-KYC) | 2 | 1.000 | 1.000 | 1.000 |

### Recall@5 by customer category and account type
| Group | Cases | dense | bm25 | hybrid |
|---|---|---|---|---|
| NRI_INDIVIDUAL | 15 | 0.656 | 0.522 | 0.722 |
| LLP | 2 | 0.833 | 0.333 | 0.833 |
| COMPANY | 2 | 0.667 | 0.667 | 0.667 |
| PARTNERSHIP_FIRM / HUF / SOLE_PROPRIETORSHIP | 1 each | 1.000 | 1.000 | 1.000 |
| TRUST | 1 | 0.500 | 1.000 | 1.000 |
| RESIDENT_INDIVIDUAL (context only) | 1 | 1.000 | 1.000 | 1.000 |
| (conversion) | 1 | 1.000 | 0.500 | 1.000 |
| SAVINGS | 10 | 0.583 | 0.433 | 0.683 |
| CURRENT | 8 | 0.812 | 0.688 | 0.875 |
| FIXED_DEPOSIT | 2 | 0.750 | 0.750 | 0.750 |
| RECURRING_DEPOSIT | 1 | 1.000 | 1.000 | 1.000 |
| FCNR_RFC | 1 | 0.500 | 0.500 | 0.500 |
| (none: conversion, context) | 3 | 1.000 | 0.833 | 1.000 |

### Dense misses (Recall@5 < 1)
**No gold retrieved:**
- **a2-03, NRI photograph:** the gold is p5 §M ("latest passport size photo"). The top 5 are p3/p4 account-selection chunks.
- **a2-04, no PAN:** the gold is p1 "If PAN not available, kindly attach Form 60".
- **a2-08, joint NRE:** the gold is the operating instruction and the second-applicant details.

**Partial:**

| Case | Recall@5 | Question |
|---|---|---|
| a2-09 | 0.33 | minor |
| a2-10 | 0.50 | FCNR |
| a2-12 | 0.50 | NRE FD |
| a2-14 | 0.50 | passport |
| a2-18 | 0.67 | LLP PAN |
| a2-21 | 0.33 | company signatories |
| a2-26 | 0.50 | trust current |

**Pattern:** within the 68-chunk NRI form, generic account-selection chunks on p3/p4 (d4f03f81, bab254ef, fa2aed72) rank high for almost any NRI question. Specific fields on p1 and p5 lose to them. The misses all concern finding the right field inside an applicable form; no miss left the scope.

### No-evidence cases (all 8 pass in every mode)
- **`not_established` (a2-27 to a2-30):** no search ran, so no hits.
- **`absent_in_scope` (a2-06, a2-15, a2-19, a2-25):** in-scope HDFC results only.
  - None of them states a PAN card copy requirement, an NRE/NRO definition, or a signatory photograph.
  - Turning these into the prescribed fallback is checklist logic (A4), not retrieval.

### Metadata and scope
- In every mode, every returned result carries document key, page, source role, text reliability and extraction method.
- No RBI, external, legacy or out-of-scope chunk was returned in any case.

## 5. Mode comparison and decision
Paired bootstrap over the 25 scored cases (10,000 resamples, seed 20261114, from the committed record):

| Comparison | Mean ΔRecall@5 | 95% CI | Better | Worse |
|---|---|---|---|---|
| hybrid − dense | +0.060 | [+0.000, +0.160] | a2-04, a2-26 | none |
| bm25 − dense | −0.120 | [−0.300, +0.040] | a2-04, a2-26 | a2-01, a2-02, a2-13, a2-16, a2-17, a2-31 |

**Decision: the default stays dense.**
- **Hybrid** is better on 2 of 25 cases and worse on none. That is not enough to justify a change:
  - the interval touches zero;
  - the set is small, single-annotator and development-only, with no held-out split;
  - in M2, hybrid also won on development data and then failed on the frozen held-out set (`docs/10`).
- **BM25 alone** is clearly worse.

Hybrid is recorded as **the candidate for a later measured re-evaluation**, for example once A4 can score checklist outcomes and a held-out split exists. `AssistantRetriever(mode=…)` already supports it; the default is unchanged.

**Better levers than the mode, for later stages:**
- A4 assembles the checklist from the coverage map's cited evidence and `form_schemas`, so finding a specific field does not depend on top-5 ranking.
- Retrieval is mostly for explanations and free-text questions.

## 6. Corrections to A0 found during A2
1. **Non-individual signatory photographs (A0 §1 row `hdfc_non_individual_account_opening`, §2 worked example):**
   - No stored text or schema field of the scanned form mentions a photograph. The A0 "authorised-signatory photograph boxes" came from looking at the page image.
   - By "evidence or nothing", it **cannot be cited**. The coverage map does not use it, and a2-25 is a no-evidence case. Organisation photographs are therefore "could not be confirmed" in A4.
2. **Entity PAN field (A0 §2 worked example "Does an LLP need a PAN card?"):**
   - The entity-level PAN field in the multimodal schema is **uncorroborated**.
   - What stored text does support: "PAN No." for signatories/partners (chunks f51508ae, 600f4dd7), and a corroborated "Form 60/61 attached" (chunk 1c8150b4).
   - Under A1 rule 3a, uncorroborated labels are shown as uncertain. The A0 conclusion is unchanged: PAN number is information to provide (OCR-uncertain), and a PAN card copy can't be confirmed.

A0 itself is left as approved. These corrections are recorded here for the owner's review.

## 7. Checks run for this stage
| Check | Result |
|---|---|
| `make lint` (ruff, black, mypy on 132 files) | clean |
| `make test` | **1564 passed, 1 failed:** K7 (`test_groq_provider::test_live_calls_and_cache_are_off_by_default`, local `.env` sets `GROQ_DATA_TERMS_VERIFIED_ON`); known baseline |
| A2 unit tests (`test_assistant_coverage`, `_retrieval`, `_eval_dataset`) | 59 passed (included above) |
| `make check-schema` | no new upgrade operations (no schema change in A2) |
| `tests/integration/test_assistant_retrieval_dev.py` | 4 passed (evidence and gold resolve in Postgres; real retrieval in dense, BM25 and hybrid returns only in-scope HDFC chunks, even for RBI-aimed queries) |
| `tests/integration/test_hybrid_dev.py` | passed |
| `tests/integration/test_dense_golden.py` | failed at m1-03: **K8**, known tied-score baseline |
| `make assistant-eval MODE=all` | figures above (reproduced twice) |

## 8. Demo path
```
make up                      # Postgres, Redis, Qdrant
make assistant-eval          # dense (default); MODE=bm25|hybrid|all to compare
cd backend && ../.venv/Scripts/python -m pytest tests/unit/test_assistant_*.py
cd backend && ../.venv/Scripts/python -m pytest -m integration tests/integration/test_assistant_retrieval_dev.py
```
