# A0: Corpus Audit and Coverage Matrix (HDFC Bank Account Opening Assistant)

> Independent prototype. Not affiliated with or endorsed by HDFC Bank or the RBI. Synthetic data only. The source documents are dated, and their current validity has not been independently verified.

**Status:** Stage 0 audit, approved by the owner on 2026-11-14 **with four amendments (§0)**. Baseline: `main` @ `3afac0d` (M5 merged). Branch: `feat/account-opening-assistant`.

**Method:** read-only inspection of `data/raw_pdfs/manifest.yaml`, `data/processed/corpus_report.md`, the stored `chunks` and `form_schemas` rows (`SELECT` only), and the backend code. No code, dependency, database row or Git history was changed by the audit.

## 0. Product direction and approved amendments
The product is now a **customer-facing HDFC Bank Account Opening Assistant**. It holds a natural conversation, asks only questions that change the answer, and produces a personalised checklist built **only from HDFC material in the corpus** (no RBI, external or internet sources). The previous compliance-engine scope (RBI retrieval, NLI, compliance scoring, review cases, KYC approval/rejection) is not part of this product. Its code is kept until removal is reviewed.

| # | Decision (owner, 2026-11-14) |
|---|---|
| D-A / **Amendment 1** | **`hdfc_kyc_aml_faq` is a legacy reference with unverified provenance**, never an HDFC policy. It does not mention HDFC, rests on a 2004 RBI circular, and says its lists are "only indicative". Nothing from it is ever an HDFC-confirmed requirement. **Default: excluded from customer-facing checklists.** It may be shown only where clearly useful, and then with its caveat, as an *unconfirmed reference*. |
| **Amendment 2** | **Re-KYC material is not account-opening material.** The NRI re-KYC document list (`hdfc_nri_rekyc` p3) and the resident re-KYC photograph requirement (`hdfc_resident_rekyc_dormant` p1) are **excluded from opening checklists** unless an applicable *opening* document independently corroborates them. If shown for context, they are labelled *unconfirmed references, not requirements*. |
| **Amendment 3** | **Coverage comes only from evidence present.** Customers may ask about any combination. For one the corpus cannot establish (e.g. resident current account, salary account), the assistant returns the prescribed fallback and never implies support. |
| **Amendment 4** | **Three customer-facing groups, each item keeping its source classification and citation:** (1) documents indicated by available source material; (2) information the customer needs to provide; (3) requirements that could not be confirmed. A form asking for a PAN *number* does not make a PAN card *copy* mandatory. A photograph field establishes no further specification. |
| **Additional** | The interface states that the available documents are **dated, their current validity has not been independently verified**, and the customer should **confirm requirements with HDFC Bank**. |
| D-B | Build on the existing corpus now; adding newer HDFC documents is a later, separate decision. |
| D-C | A plain per-turn service in FastAPI; the existing LangGraph workflow stays in the repository, unused by the new path (CLAUDE.md to be updated in A1). |
| D-D | Deterministic first, LLM optional. Choices and keyword/enum matching work without an LLM; the LLM (synthetic data only) adds free-text understanding and wording. Classification and citations are always deterministic. |

**Prescribed fallback (verbatim):** "I couldn't find this information in the available HDFC Bank documents. I don't want to give you an incorrect answer."

## 1. HDFC corpus inventory
| Document key | Kind (for this product) | Pages / chunks | Text quality | Date basis | Content relevant to account opening |
|---|---|---|---|---|---|
| `hdfc_nri_account_opening` | **Opening form** | 6 / 68 | native text | PDF 2019; no printed edition date | NRI/PIO individual account-cum-term-deposit opening. **NRE Savings / NRE Current** (p3 §H), **NRO Savings / NRO Current** (p3 §I), NRE/FCNR/RFC tick box (p1), **Recurring Deposit** (p3 §J), **Term Deposit** (p3 §K). Fields: "\*Passport No.", "%PAN No.", "%If PAN not available, kindly attach Form 60" (p1 §C), address-proof type options (passport, driving licence, UID, voter ID, NREGA card, others), contact details, minor/guardian and joint applicants (p1–2), "1st Applicant's latest passport size photo" (p5 §M). Account opening rules (p5): "All necessary documentation as mandated by the regulatory / bank authorities should be provided"; "\*Savings account can be opened only by individuals for non-business purposes". |
| `hdfc_non_individual_account_opening` | **Opening form**, scanned | 2 (fold-out) / 21 | **OCR, all chunks `ocr_uncertain`** | print code 92713/07.01.2015 | Sole Proprietor/HUF/Trust/Firm/Corporate opening. Account types **Savings, Current, Fixed Deposit, Recurring Deposit** (p1). Entity types incl. proprietorship, partnership, **Limited Liability Partnership**, public/private company, HUF, trust, society, association, club, government, bank, NGO (p1). PAN field and "Form 60/61 attached for existing customers", "All fields marked \* are MANDATORY", authorised-signatory photograph boxes, HUF / partnership / proprietor declarations (p2). **No list of required documents** ("staple the relevant documents"). |
| `hdfc_resident_to_nro_conversion` | Conversion form (resident → NRO) | 6 / 59 | native | PDF 2018 | Conversion of a resident account to NRO; PAN/Form 60, passport, photograph boxes, declarations. About 49% of its text duplicates the NRI opening form. |
| `hdfc_resident_rekyc_dormant` | **Re-KYC / dormant form** (Amendment 2); p2 is an Aadhaar e-KYC **opening** consent | 2 / 9 | native | print code 93317/03.02.2021 | p1 re-KYC: self-attested identity/address proof, "Please affix recent photo" → **excluded from opening checklists**. p2 "Application for account opening using Aadhaar / e-KYC": "submission of Aadhaar is not mandatory, and there are alternative options for KYC". |
| `hdfc_nri_rekyc` | **Re-KYC form** (Amendment 2) | 3 / 12 | OCR (p3 `ocr_reliable`) | print code 93316/05.08.2020 | p3 "Acceptable list of KYC documents for Non-Resident Indians" (PAN/Form 60; identity; NRI/PIO status; address; additional). **Excluded from opening checklists** unless corroborated by an opening document; at most an unconfirmed reference. |
| `hdfc_kyc_aml_faq` | **Legacy reference, provenance unverified** (Amendment 1) | 3 / 13 | native | 2004 RBI basis; PDF 2008 | Generic KYC FAQ; indicative lists for individuals (1.0), companies (2.0), partnership firms (3.0), trusts (4.0). No account types; no LLP, proprietorship, HUF or NRI. **Excluded from checklists by default.** |
| `hdfc_netbanking_corporate_application` | Context only | 2 / 8 | native | footer code Dec06 | NetBanking board-resolution template; not account opening. Not used. |

**Structured form schemas** (`form_schemas`, M2) exist for every form: deterministic (`m1_chunks`) and Gemini (`gemini_multimodal`), with sections, field labels and `required_as_shown` markers (e.g. NRI opening: 362 fields, 50 marked as required on the form). Quality flags include `ambiguous_required_markers` and `uncorroborated_fields`; the scanned form is `scanned_document`. These are the source for "information to provide".

## 2. Coverage matrix (evidence only; Amendments 1–3 applied)
Legend:
- **O** = an applicable **opening** form covers this combination (native text);
- **O~** = an applicable opening form covers it, but the text is **OCR-uncertain** (scanned) and must be labelled so;
- **C** = conversion form only (resident → NRO);
- **–** = **not established**, giving the prescribed fallback (Amendment 3).

Re-KYC material and the legacy FAQ create **no** cell.

| Customer category ↓ / Account → | Savings | Current | Salary | Fixed / Term Deposit | Recurring Deposit | NRE | NRO | FCNR / RFC |
|---|---|---|---|---|---|---|---|---|
| Resident individual | – ¹ | – | – | – | – | n/a | n/a | n/a |
| NRI / PIO / OCI individual | O (NRE/NRO Savings) | O (NRE/NRO Current) | – | O | O | O | O | O ² |
| Resident individual becoming NRI | – | – | – | – | – | – | C | – |
| Sole proprietorship | – ³ | O~ | – | O~ | O~ | – | – | – |
| Partnership firm | – ³ | O~ | – | O~ | O~ | – | – | – |
| LLP | – ³ | O~ | – | O~ | O~ | – | – | – |
| Company (public / private) | – ³ | O~ | – | O~ | O~ | – | – | – |
| Trust | – ³ | O~ | – | O~ | O~ | – | – | – |
| HUF | – ³ | O~ | – | O~ | O~ | – | – | – |
| Society / association / club / other entity on the form | – ³ | O~ | – | O~ | O~ | – | – | – |
| Minor (with guardian), NRI | O | – | – | O | O | O | O | – |
| Joint applicants, NRI | O | O | – | O | O | O | O | – |

Notes:
1. **Resident individuals:**
   - **What exists:** no opening form and no opening document list. The only opening-related text is the Aadhaar e-KYC consent page (`hdfc_resident_rekyc_dormant` p2), and it establishes only that Aadhaar submission is **not mandatory**. It can answer "Is Aadhaar mandatory?", but it does not establish a savings-account checklist.
   - **Every resident account type is therefore "–".** That includes resident current and salary accounts (Amendment 3).
2. **FCNR / RFC:** only a tick box on the NRI opening form ("NRE/FCNR/RFC Account"). There is no FCNR-specific section, so the answer must stay at the level of the form's fields.
3. **Non-individual savings:** the scanned form has a "Savings" tick box, but the NRI opening form states "Savings account can be opened only by individuals for non-business purposes". The corpus does not settle whether a non-individual savings account is available, so it is **not established**. Current, Fixed and Recurring Deposit appear as account types on the same form; only the account type is established, not a document list.

**What each covered cell can and cannot produce:**
| Cell | Documents indicated by source material | Information to provide | Could not be confirmed |
|---|---|---|---|
| NRI individual (O) | Form-indicated items with explicit wording: Form 60 **if PAN is not available** (p1 §C); "latest passport size photo" (p5 §M; no further specification); a document supporting a mandate/POA where one is used (p1 §B, "please attach relevant document"); address proof of the type selected (p1 §C) | Name (as on ID proof), passport number and expiry, PAN number, tax residency/TIN, addresses, contact details, nominee, account selections (form fields, from `form_schemas`) | Any document list beyond the form's own wording; PAN card copy (the form asks for the PAN **number**); documents "as mandated by the regulatory / bank authorities" (not named) |
| Non-individual entity (O~) | Authorised-signatory photographs (photo boxes; OCR-uncertain); Form 60/61 for existing customers (OCR-uncertain) | Entity name, type of entity, PAN (number), addresses, contact details, authorised signatories, declarations (all OCR-uncertain) | **The document list** (the form says only "staple the relevant documents"); PAN card copy; incorporation, partnership deed, resolutions and similar (only the excluded legacy FAQ mentions them) |
| Resident → NRO (C) | Form-indicated items on the conversion form | Its fields | Everything not stated on that form |

**Worked examples:**
- **"Does an LLP need a PAN card?"** The non-individual opening form asks for the entity's **PAN (information to provide; OCR-uncertain)**. Whether a PAN card **copy** must be submitted **could not be confirmed**: no applicable HDFC document states it.
- **"Do I need to submit a photograph?"**
  - **NRI:** yes, the NRI opening form has an applicant photograph box ("latest passport size photo"). That is document-indicated, with no further specification.
  - **Non-individual entity:** the form has authorised-signatory photograph boxes (OCR-uncertain).
  - **Resident individual:** not established, so the fallback. The resident re-KYC photograph is excluded under Amendment 2.
- **"What documents do I need for a savings account?"** The assistant first asks who the account is for.
  - **NRI:** the NRI opening form items above.
  - **Resident individual:** the fallback.
  - **Non-individual:** the fallback (note 3).
- **"How do I open a current account for my LLP?"** Information fields and form-indicated items from the scanned non-individual form, labelled OCR-uncertain. The document list could not be confirmed.

## 3. Why the previous LLP query was DEGRADED and the LLM `not_configured`
1. **LLM off:** the local `.env` has `GROQ_LIVE_ENABLED=false`, although an API key, `GROQ_DATA_TERMS_VERIFIED_ON` and model ids are set.
   - `GroqProvider.from_settings` (`backend/app/llm/groq.py`) raises `ProviderUnavailable`, and `runtime.live_llm()` (`backend/app/workflow/runtime.py`) returns `None`.
   - `entity_intake` records `Degradation(llm, not_configured)` and asks every question.
   - `compliance_synthesis` returns `_degraded(state, "not_configured")`, so no checklist is drafted and the outcome is DEGRADED.
2. **Thin LLP evidence:** even with the LLM (the single M3 live run), only form evidence exists for LLPs, so the M3 validator lowered both proposed MANDATORY items.
3. **Degraded by design:** with NLI disabled (M4), `claim_verification` adds a `verification` degradation to every report with a checklist, so every such report is DEGRADED.
4. **RBI-dominated retrieval:** all 229 "primary" chunks are RBI, and the MANDATORY gate accepted only primary evidence, so HDFC material could never establish a requirement in that design.

## 4. Reuse, simplify, retire
- **Reuse:**
  - **Ingestion and storage:** PDF ingestion and OCR, chunking, Postgres, BGE embeddings, Qdrant and reconciliation (`backend/app/ingestion/`, `backend/app/retrieval/`).
  - **Retrieval:** `retriever_from_settings` with filters restricted to `bank='HDFC'` and to the coverage map's document keys.
  - **Corpus and forms:** `CorpusReader.evidence()` (stored text plus parent context); `form_schemas` and their cached loader.
  - **LLM layer:** `LLMProvider`, `generate_validated` and the Groq provider with its rate limiter.
  - **Safety:** PII screening (`screen_text`) and the codes-only error envelope.
  - **Frontend:** the typed client, `ApiError` messages, the idempotency tracker, Layout / Disclaimer / ErrorBoundary / focus helper, the `sessionStorage` store and the test helpers.
- **Simplify:** one plain service per conversation turn (understand → ask the next material question, or retrieve → assemble → validate → answer). There is no interrupt/resume, no checkpointer and no SSE.
- **Retire from the new path (kept, unused, until removal is approved):**
  - the LangGraph workflow, NLI claim verification and the conflict scan;
  - review cases and report v1/v2 with the acceptance checker;
  - RBI retrieval;
  - the M5 Dashboard / Assistant / Report screens (reusable parts are copied, not deleted).

## 5. Target architecture (summary)
```
Chat UI (free text + choice chips + editable profile + checklist with expandable cited passages)
  -> POST /api/v2/assistant/sessions, POST .../{id}/turns, GET .../{id}
  -> TurnService: screen input -> understand (choices > enum/keyword matcher > optional LLM)
     -> ask the next MATERIAL question (coverage map) or
     -> retrieve HDFC opening material for the cell -> assemble the three groups deterministically
     -> validate (every item cites a stored chunk or form field; class matches source type)
     -> answer, or the prescribed fallback
```
- **Item classes (internal; every item keeps one, with its citation):**
  - `form_indicated`: an applicable opening form explicitly asks to attach or provide it. Goes to group 1.
  - `information_field`: a field the form asks the customer to fill in. Goes to group 2.
  - `not_confirmed`: asked about or implied, but no applicable opening document establishes it. Goes to group 3. Legacy-FAQ or re-KYC items shown for context are `unconfirmed_reference`, also group 3, always with their caveat.
- **Reserved, never emitted with today's corpus:** `policy_supported` and `conditional`, for when a current HDFC policy document is added (decision D-B).
- **Coverage map:** versioned and data-driven, built from this matrix. Every cell cites stored chunk ids, and no requirement text is hard-coded.
- **Interface notice (Additional requirement):** shown with every answer: the sources are dated, their current validity is not independently verified, and the customer should confirm requirements with HDFC Bank. Each item also shows its source's date basis and, for the scanned form, that the text may be inaccurate.

## 6. Stages (one review and one commit each)
| Stage | Scope |
|---|---|
| **A0 (this)** | Branch, audit and coverage matrix (documentation only) |
| A1 | Product spec and taxonomy in `docs/assistant/`; CLAUDE.md updated for the new direction (HDFC-only, LangGraph not required, RBI excluded) |
| A2 | Coverage map (data plus tests, every cell cited) and HDFC-only retrieval scope; HDFC-only evaluation set (the example questions plus more) with Recall@5 |
| A3 | Understanding and dialogue policy (deterministic matcher, optional validated LLM extraction, material questions only, corrections) |
| A4 | Checklist assembly, classification, validation and fallback (deterministic; optional LLM wording from cited text only) |
| A5 | API v2 and persistence (new Alembic tables `assistant_sessions` / `assistant_turns`; existing tables untouched) |
| A6 | Frontend: one conversational assistant screen |
| A7 | Evaluation, opt-in live-LLM run on synthetic data, browser demo, documentation and closure |

## 7. Risks
1. **Thin, dated corpus:** many questions will return the fallback (for example every resident account type and every non-individual savings account). The answers that exist rest on 2015–2021 form copies whose currency cannot be verified.
2. **Scanned non-individual form:** every non-individual answer rests on OCR-uncertain text. Prefer corroborated schema fields, label the text as possibly inaccurate, and never quote garbled text.
3. **No HDFC-confirmed requirements exist** in today's corpus. The UI must make "the form asks for it" understandable without implying it is legally required.
4. **Duplicated form text** (the NRI opening and conversion forms) can crowd retrieval. Scope retrieval by the coverage map and measure it in A2.
5. **CLAUDE.md** still describes the previous product (RBI sources, LangGraph locked). It must be updated in A1, before any implementation, so later work follows the new direction.
