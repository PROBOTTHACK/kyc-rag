# A1: Product Specification and Taxonomy (HDFC Bank Account Opening Assistant)

> Independent prototype. Not affiliated with or endorsed by HDFC Bank or the RBI. Synthetic data only. The source documents are dated, and their current validity has not been independently verified.

**Status:** A1. Documentation only, on branch `feat/account-opening-assistant`.
**Basis:** [`A0_AUDIT.md`](A0_AUDIT.md), the evidence-based corpus audit and coverage matrix approved on 2026-11-14 with four amendments.
**Supersedes:** the compliance-engine product scope (docs `00`–`15`, M0–M5) for all new work. The previous implementation is preserved, not deleted (§10).

Normative words: **must / must not** are requirements, **should** is the default unless the owner decides otherwise, and **may** is optional.

---

## 1. Product
A customer-facing conversational assistant that helps a customer understand **which documents and information** they will need to open an HDFC Bank account. Two things define it:
- **Personalisation:** the answer is built for the customer's account type and customer category.
- **Evidence:** every item comes from HDFC material in the project corpus, with its classification and a citation.

When the corpus cannot establish an answer, the assistant says so (§7) instead of guessing.

**Primary users:** a non-technical prospective customer, asking with synthetic data only in this prototype.

---

## 2. Source hierarchy
Sources are ranked; a lower tier can never strengthen an item to a higher classification.

| Tier | Source | Corpus today | Use |
|---|---|---|---|
| 1 | **Applicable, authoritative HDFC policy**: an HDFC document whose authorship is verifiable from its own text and which states requirements for the customer's category and account type | **None** | The only basis for `policy_supported` (§3). Reserved until such a document is added (decision D-B). |
| 2 | **Applicable HDFC account-opening form** for the customer's category and account type | `hdfc_nri_account_opening` (native text); `hdfc_non_individual_account_opening` (scanned, OCR-uncertain) | `form_indicated` documents and information fields |
| 2c | **Applicable HDFC conversion form** (resident → NRO only) | `hdfc_resident_to_nro_conversion` | `form_indicated` items, **only** for the conversion request |
| 3 | **HDFC material that is not about opening this account**: re-KYC / dormant forms, the opening form of another category, the conversion form for a non-conversion request | `hdfc_nri_rekyc`, `hdfc_resident_rekyc_dormant` p1 | At most `unconfirmed` context (§3), never a requirement (A0 amendment 2) |
| 4 | **Legacy reference, provenance unverified** | `hdfc_kyc_aml_faq` | `legacy_reference`: excluded from checklists (A0 amendment 1) |
| — | Context-only HDFC material | `hdfc_netbanking_corporate_application` | Not used |
| — | **Excluded entirely** | RBI documents (still stored), any non-HDFC, external or internet source, model knowledge | Never retrieved, cited or paraphrased |

**Special case: the Aadhaar e-KYC opening consent** (`hdfc_resident_rekyc_dormant` p2, "Application for account opening using Aadhaar / e-KYC") is an account-opening page inside a re-KYC form. It may answer the narrow question of whether Aadhaar submission is mandatory: it states that it is **not mandatory** and that alternatives exist. It does **not** establish any resident checklist.

---

## 3. Evidence classifications (exact codes)
Every checklist item carries exactly **one** classification. Classifications are assigned **deterministically** from the source tier and the source wording, **never by the LLM**.

| Code | Customer-facing label | Definition | Allowed sources | Shown in |
|---|---|---|---|---|
| `policy_supported` | "Required by HDFC Bank policy" | A tier-1 policy explicitly requires the item for this category and account type. A stated condition goes in `condition` (§5). | Tier 1 only | Group 1 (documents) or Group 2 (information) |
| `form_indicated` | "Asked for on the HDFC account opening form" | An applicable tier-2 (or, for conversions, 2c) form explicitly asks for the item. For a document, the form says to attach or provide it, or has a dedicated box for it (e.g. a photograph box). For information, the form has a field for it. The form's own condition ("if PAN not available") goes in `condition`. A form's "mandatory" marker is reported as the form's marking, **not** as a confirmed requirement. | Tier 2 / 2c | Group 1 or Group 2 |
| `unconfirmed` | "Could not be confirmed" | Either: (a) the item appears only in tier-3 material (re-KYC lists, another category's form, the conversion form outside a conversion), labelled an *unconfirmed reference, not a requirement*; or (b) the customer asked about it, or it is a natural assumption from a field (e.g. a **PAN card copy** inferred from a PAN-number field), and no applicable source establishes it. | Tier 3, or no source | Group 3 only |
| `legacy_reference` | "Older reference, not confirmed by HDFC Bank" | The item appears only in `hdfc_kyc_aml_faq`. | Tier 4 | **Not shown in the MVP.** Enabling it needs owner approval; if enabled, Group 3 only, with the caveat "An older, undated KYC reference of unverified origin lists this. It is not an HDFC Bank requirement." |
| `unknown` | "Not found in the available documents" | Nothing in the corpus addresses the item or combination. | None | Group 3, with the fallback sentence (§7) |

**Rules:**
1. **Never strengthen.** No code path upgrades an item; e.g. `form_indicated` never becomes `policy_supported`, and `unconfirmed` never becomes `form_indicated`.
2. **One item, its strongest applicable basis.** If both a tier-2 form and tier-3 material mention an item, it is `form_indicated` and cites the tier-2 form. The tier-3 mention may be listed as a secondary citation, never as the basis.
3. **"Inferred" is never a requirement.** The MVP has no inference rules that add requirements. Anything inferred is `unconfirmed` (Group 3).
4. **`policy_supported` is not emitted with today's corpus.** A test enforces this until a tier-1 document is ingested and approved.
5. **No "mandatory / optional / conditional" verdicts.** The assistant states what each source says and how strong that basis is. It does not decide what HDFC Bank legally requires.

---

## 4. Information fields versus documents
| Kind | Meaning | Examples (from the A0 evidence) | Classification it can have |
|---|---|---|---|
| `information` | Data the customer **writes or states** on the form | Name (as on ID proof), date of birth, gender, mobile number, e-mail, residential/overseas/Indian address, **PAN number**, passport number and expiry, tax residency / TIN, nationality, occupation, nominee details, account options selected | `form_indicated`, or `policy_supported` (tier 1 only) |
| `document` | Something the customer **submits or attaches** | Form 60 ("if PAN not available, kindly attach Form 60"), an address proof of the type ticked on the form ("Attached Address Proof of …"), a mandate / power-of-attorney document where one is used ("please attach relevant document"), a **photograph** where the form has a photo box | `form_indicated`, `policy_supported` (tier 1), `unconfirmed`, `unknown` |
| `declaration` | A statement on the form the customer **reads and signs** | PIO declaration, close-relative address declaration, minor declaration, HUF / partnership / proprietor declarations | `form_indicated` |

**Rules:**
1. **An identifier field is information, never a document.** A field for a PAN number, passport number or Aadhaar number does not create a "PAN card copy", "passport copy" or "Aadhaar copy" item. If the customer asks about the copy, it is `unconfirmed`, or `unknown` when nothing mentions it.
2. **A document item needs explicit form wording** to attach, provide, staple or affix it, or a dedicated box for it. "Name (same as ID proof)" is information, and does not by itself require an ID document.
3. **A photograph box establishes a photograph, nothing more.** The item quotes the form's words (e.g. "latest passport size photo"). No size, background, count or attestation is added beyond the source wording.
4. **Conditional form wording stays conditional.** "If PAN not available, kindly attach Form 60" becomes the document **Form 60** with condition "If PAN is not available". It is not "Form 60 required".
5. **The source of truth for fields is the stored form schema** (`form_schemas`, per form; the Gemini v2 schema where its fields are corroborated by stored text, otherwise the deterministic schema), cross-checked against the stored chunk. Fields flagged `uncorroborated_fields` or from scanned pages carry the caveat `scanned_text` (§5).

---

## 5. Taxonomy (from the A0 coverage matrix only)
### 5.1 Customer categories
| Code | Customer wording (choice chip) | Coverage basis |
|---|---|---|
| `RESIDENT_INDIVIDUAL` | "An individual living in India" | **No opening form.** All account types are `NOT_ESTABLISHED`. |
| `NRI_INDIVIDUAL` | "An individual living outside India (NRI / PIO / OCI)" | NRI opening form (native) |
| `SOLE_PROPRIETORSHIP` | "A sole proprietorship" | Non-individual opening form (scanned) |
| `PARTNERSHIP_FIRM` | "A partnership firm" | same |
| `LLP` | "A limited liability partnership (LLP)" | same |
| `COMPANY` | "A company (private or public limited)" | same |
| `TRUST` | "A trust" | same |
| `HUF` | "A Hindu Undivided Family (HUF)" | same |
| `OTHER_ORGANISATION` | "A society, association, club or other organisation" | same (entity types listed on the form) |

### 5.2 Account types
| Code | Customer wording | Notes |
|---|---|---|
| `SAVINGS` | "Savings account" | |
| `CURRENT` | "Current account" | |
| `SALARY` | "Salary account" | **Not established for any category** (the only mention is an NRI-form rule about salary-account conversion). |
| `FIXED_DEPOSIT` | "Fixed / term deposit" | |
| `RECURRING_DEPOSIT` | "Recurring deposit" | |
| `FCNR_RFC` | "FCNR / RFC deposit" | NRI only; a tick box on the NRI form, with no dedicated section |

Further facets are asked **only when material** (§6):
- `nri_scheme` ∈ {`NRE`, `NRO`}, for NRI savings / current / RD / FD;
- `holding` ∈ {`SINGLE`, `JOINT`};
- `minor` ∈ {`YES`, `NO`}, NRI only.

**Separate intent:** `RESIDENT_TO_NRO_CONVERSION` ("Convert my resident account to NRO"), answered from the conversion form only.

### 5.3 Coverage states (one per category × account cell; source: A0 §2)
| State | Meaning | Answer behaviour |
|---|---|---|
| `COVERED` | An applicable opening form with native text | Full three-group checklist |
| `COVERED_OCR` | An applicable opening form whose text is scanned / OCR-uncertain | Three-group checklist; every item carries `scanned_text`, and the answer says the form text may be inaccurate |
| `CONVERSION_ONLY` | Only the conversion form applies | Checklist for the conversion request only |
| `NOT_ESTABLISHED` | No applicable opening material | The prescribed fallback (§7). The assistant **must not** imply that the combination is supported. |

**Cells (A0 §2):**
- `NRI_INDIVIDUAL`: SAVINGS, CURRENT, FIXED_DEPOSIT, RECURRING_DEPOSIT and FCNR_RFC are `COVERED`; SALARY is `NOT_ESTABLISHED`.
- Each non-individual category: CURRENT, FIXED_DEPOSIT and RECURRING_DEPOSIT are `COVERED_OCR`; SAVINGS and SALARY are `NOT_ESTABLISHED`.
- `RESIDENT_INDIVIDUAL`: every account type is `NOT_ESTABLISHED`.
- `RESIDENT_TO_NRO_CONVERSION`: `CONVERSION_ONLY`.

The coverage map (A2) is the machine-readable form of this table. Every cell cites stored chunk ids, and changing a cell requires corpus evidence and owner review.

### 5.4 Current interpretation (owner decisions at the A2 review, 2026-11-14)
This section is the **current** interpretation. It supersedes `A0_AUDIT.md` where they differ; A0 is kept unchanged as the historical audit. The coverage map records these rulings as `item_rulings` (`backend/app/assistant/coverage.yaml`, version `a2-2`), validated on load and tested.

**Item rulings (non-individual opening form):**

| Item | Ruling | Basis |
|---|---|---|
| Photographs of authorised signatories | **Not established.** No photograph requirement exists for organisations; it must not appear as a checklist item. If a customer asks, the answer is "could not be confirmed". | No stored text or form-schema field of the form mentions a photograph. A0's "authorised-signatory photograph boxes" came from looking at the page image and cannot be cited. |
| PAN No. of authorised signatories / partners | **Evidenced information** (something the customer writes on the form; OCR-uncertain). | Stored text "PAN No." in the Authorized Signatory Details blocks (p2; chunks `f51508ae…`, `600f4dd7…`). |
| Form 60/61 | **Evidenced**, with the form's own condition "for existing customers" (OCR-uncertain). | Stored text "Form 60/61 attached for existing customers" (p1; chunk `1c8150b4…`). |
| A copy of the entity's PAN card | **Not established.** It is never inferred, and the PAN No. field does not create it (§4 rule 1). | No applicable HDFC document asks for it. The entity-level PAN field exists only in the uncorroborated multimodal schema. |

Rulings record evidence, never an evidence classification. Classes are assigned deterministically in A4 (§3).

**Standing rules (restated by the owner at the A2 review):**
1. **The 26 covered combinations are corpus evidence coverage, not confirmed HDFC policy coverage.** They mean that an applicable HDFC opening form exists in the corpus, not that HDFC Bank's current requirements are known.
2. **No HDFC policy-supported requirement exists in the present corpus.** `policy_supported` is never emitted (§3, §9 item 6).
3. **Semantic (dense) retrieval remains the default.**
4. **Hybrid retrieval is an experimental candidate**, not the production default, until a measured re-evaluation justifies a change (`A2_RETRIEVAL_EVAL.md` §5).
5. **The A2 Recall@5 figures keep their limitations** wherever they are quoted: a 25-case, single-annotator, development-only set with no held-out split.

---

## 6. Conversation rules
**Profile slots:** `account_type`, `customer_category`, the facets of §5.2 (`nri_scheme`, `holding`, `minor`), and the intent (open / convert). Each slot records its value and its **origin**: `chosen` (a chip), `stated` (explicit words in the customer's message), or `corrected`.

| # | Rule |
|---|---|
| R1 | **Ask the account type when it is missing**: "Which account would you like to open?", with chips for §5.2 plus "Not sure". |
| R2 | **Ask the customer category when it is missing**: "Who is the account for?", with chips for §5.1. If both are missing, ask the account type first, then the category. If the customer's message already names one, ask only the other. |
| R3 | **Ask only material follow-ups.** A facet is asked only if, according to the coverage map, its value changes the applicable sources or the resulting items. Examples: `nri_scheme` for NRI accounts; `holding` and `minor` only where the applicable form has joint, guardian or minor sections. Nothing is asked "just in case". |
| R4 | **One question per turn**, with choice chips plus a free-text box. *(Clarified 2026-11-14, owner, A2.)* Every question also offers:<br>- **"I'm not sure"**: the slot stays unknown, and the assistant explains in one sentence what the choice changes;<br>- **"Skip"**, where the slot is not essential: the assistant continues without it;<br>- **"What does this mean?"**: a short explanation from fixed text, then the same question again.<br>The assistant never forces repeated attempts and never loops. If an **essential** slot (account type or customer category) stays unknown, it explains why a reliable checklist cannot be generated without it, and answers any question that doesn't depend on it. Corrections remain possible at any time (R6). |
| R5 | **Free text is always allowed.** Understanding order: explicit chip selection > deterministic matching of the category / account vocabulary (§5) > optional LLM extraction (Pydantic-validated, enum values only). Item questions ("Do I need a photograph?") are answered within the current profile, after the missing slots are asked. |
| R6 | **Corrections.** The customer may change any slot at any time, through an editable profile chip or in words ("Actually it's for my company"). The slot becomes `corrected`; facets that no longer apply are cleared; the checklist is rebuilt from scratch. Earlier answers stay in the history, marked as superseded. |
| R7 | **Never invent missing information.** No slot is filled from a guess, a default or model knowledge. "My business" does not mean proprietorship, "I live in Dubai" does not mean NRI (ask to confirm), and silence does not mean single holding. Unknown stays unknown and is asked when material. |
| R8 | **Unsupported combinations.** The customer may select or ask about any combination. For a `NOT_ESTABLISHED` cell the answer is the prescribed fallback, without suggesting that the assistant supports it. Out-of-scope topics (loans, cards, charges, interest rates, eligibility decisions) also get the fallback. |
| R9 | **Input screening** (existing `screen_text`) runs on every message before anything is stored or sent to an LLM. Identifiers such as PAN or Aadhaar numbers are rejected with a fixed message, and the text is never echoed. |
| R10 | **No approval language.** The assistant never says that a customer is eligible, approved, rejected or "compliant". It describes what the sources say. |

---

## 7. Fallback and dated-document notice (exact text)
**Prescribed fallback (verbatim, never paraphrased by the LLM):**
> I couldn't find this information in the available HDFC Bank documents. I don't want to give you an incorrect answer.

**When it is used:**
1. The profile's cell is `NOT_ESTABLISHED` (§5.3). It replaces the whole checklist.
2. The cell is covered, but the specific item asked about has no evidence in the applicable sources. That item goes to Group 3 as `unknown` with this sentence; the rest of the checklist is still shown.
3. After validation (§8) no item remains.
4. An out-of-scope question (R8).

**A technical failure is not "no evidence".** If the documents cannot be searched (store unavailable, timeout), the answer is:
> I couldn't check the HDFC Bank documents just now. Please try again in a moment.

The fallback is never used for a technical failure, and the technical message is never used for missing evidence.

**Dated-document notice:** shown with every checklist and every fallback, verbatim:
> These answers are based on HDFC Bank documents held in this prototype. The documents are dated, and their current validity has not been independently verified. Please confirm the requirements with HDFC Bank before you apply.

**Per-item caveats** (shown in plain words next to the item):
- `dated_source`: "Source: a form copy from {year}; the current version has not been verified." Uses only the manifest's date basis, never an invented date.
- `scanned_text`: "From a scanned form; the text may be inaccurate."
- `not_opening_document`: "From a re-KYC form, not an account opening form." Group 3 only.
- `conditional`: the source's own condition, verbatim.

**Persistent disclaimer** (layout banner, unchanged): "Independent prototype. Not an official bank system. Not a compliance approval. Not affiliated with or endorsed by HDFC Bank or the RBI. Synthetic data only. Every result requires human review." It may be reworded in A6 for customers, without removing any of its claims.

---

## 8. Checklist output structure and citations
### 8.1 Answer object (API → UI; the A5 contract implements it)
```text
Answer
  kind:            "question" | "checklist" | "fallback" | "technical_error"
  question?:       { slot, text, choices[] }                       (kind = question)
  profile:         { account_type, customer_category, facets…, each with origin }
  coverage_state:  COVERED | COVERED_OCR | CONVERSION_ONLY | NOT_ESTABLISHED
  notice:          the dated-document notice (§7, verbatim)
  groups:                                                      (kind = checklist)
    documents:     Item[]   — kind document | declaration;  classification policy_supported | form_indicated
    information:   Item[]   — kind information;             classification policy_supported | form_indicated
    not_confirmed: Item[]   — classification unconfirmed | unknown (| legacy_reference only if enabled)
  fallback?:       the prescribed fallback (verbatim)          (kind = fallback, or per item)
Item
  title:           customer wording (plain language; LLM may phrase it, from cited text only)
  kind:            document | information | declaration
  classification:  §3 code
  condition?:      verbatim condition from the source
  source_wording:  exact quote from the stored text (substring of the cited chunk, or the form field's label)
  citations[]:     ≥ 1 (except `unknown`)
    document_title, document_key, page, page_end?, section?, text_reliability,
    source_date_basis (from the manifest), passage (stored text shown on expand)
  caveats[]:       dated_source | scanned_text | not_opening_document | conditional
```

### 8.2 Citation requirements
1. **Every item except `unknown` has at least one citation** to a stored chunk, or a stored form-schema field together with its page and stored chunk.
2. **Citations are copied from Postgres, never from LLM output.** The document key, title, page, page range, section and text reliability all come from the stored rows. Before the answer is returned, the validator re-resolves every cited chunk id.
3. **`source_wording` must be an exact substring of the cited chunk's stored text**, or the exact stored field label. If it isn't, the item is dropped (or moved to `unknown` if the customer asked about it).
3a. **Quotation quality (clarified 2026-11-14, owner, A2).** The rule depends on the cited chunk's `text_reliability`:
   - **`native` or `ocr_reliable`:** the text may be displayed as an exact quotation (quotation marks, "the form says: …").
   - **`ocr_uncertain`:** the text must never be displayed as a verified quotation. The UI shows a clear uncertainty label, *"Text read automatically from a scanned page; it may contain errors"*, and presents the evidence as an approximate reading, not in quotation marks.
   - **Multimodal-schema field labels** (Gemini) that are not corroborated by stored text are treated like `ocr_uncertain`.

   The item's title and classification are unaffected; only the presentation of the source text changes.
4. **The classification must agree with the source tier** (§2–§3): e.g. a `form_indicated` item must cite a tier-2/2c form applicable to the profile, and `policy_supported` must cite tier 1.
5. **Only applicable sources:** the cited document must be in the coverage map's source set for the profile's cell, except Group 3 tier-3 context, which carries `not_opening_document`.
6. **The UI shows the document title, page and expandable passage**, never chunk ids, scores, embeddings or internal stage names.
7. **Group ordering:** within Group 1, `policy_supported` before `form_indicated`. Items keep the form's own order where known; otherwise they are ordered alphabetically by title. Duplicate items from near-identical forms are merged, with all citations kept.

### 8.3 Role of the LLM (optional, D-D)
- **Allowed:** extracting slot values from free text (enum values only, validated); rephrasing an item's `title` or a short explanation **from the cited text only**.
- **Not allowed:** choosing items, classifications, citations, pages, coverage states or the fallback; adding any fact not in the cited text.
- **Validation:** LLM output is checked with Pydantic, then the deterministic validator runs. On any failure the deterministic wording is used; one retry at most (CLAUDE.md bounded loops).

---

## 9. What the MVP must not do
1. Use, retrieve, cite or paraphrase RBI documents, any non-HDFC source, the internet, or model knowledge as evidence.
2. Present `hdfc_kyc_aml_faq` as HDFC policy, or show it in checklists (A0 amendment 1).
3. Present re-KYC or dormant-activation material as an account-opening requirement (A0 amendment 2).
4. Imply support for a `NOT_ESTABLISHED` combination, including resident savings, current or salary accounts and non-individual savings (A0 amendment 3).
5. Turn an identifier field into a document requirement (a PAN number is not a PAN card copy), or add specifications to a photograph (§4).
6. Emit `policy_supported` with today's corpus, upgrade any classification, or declare items mandatory, optional or conditional on its own authority.
7. Fabricate a requirement, citation, document, page, date, condition or quotation, or use the LLM to decide anything in §8.3's "not allowed" list.
8. Approve, reject, score or assess a customer's eligibility or KYC, or use compliance or approval language.
9. Run NLI claim verification, compliance scoring, conflict scans, review cases, compliance dashboards or multi-agent orchestration.
10. Show internal workflow stages, database ids, retrieval scores, model names, raw errors or stack traces.
11. Accept real customer data: PII screening stays, and identifiers are rejected, never echoed or logged.
12. Delete or rewrite the previous implementation, its tests or its database records before a reviewed removal stage.
13. Call a live LLM in tests, or without the existing opt-in gate (`GROQ_LIVE_ENABLED` and the data-terms date).

---

## 10. Relationship to the previous implementation
- **Preserved and unused by the new path:** the LangGraph workflow (`backend/app/graph/`), NLI verification and the conflict scan (`backend/app/verification/`), review cases, report v1/v2, the M3–M5 API (`/api/v1`) and the M5 frontend screens. Their tests keep running. Nothing is deleted until a removal stage is reviewed and approved.
- **Reused** (A0 §4): ingestion, OCR, chunking, Postgres, embeddings, Qdrant, retrieval with filters, `CorpusReader`, `form_schemas`, the LLM provider layer, PII screening, the error envelope, and frontend infrastructure.
- **New code** lives under `backend/app/assistant/` and `/api/v2/assistant/…`, plus `frontend/src/assistant/`, with new Alembic tables only (A5). Existing tables are untouched.

## 11. Stage plan (unchanged from A0)
| Stage | Scope |
|---|---|
| A2 | Coverage map (data plus tests, every cell cited); HDFC-only retrieval scope; HDFC-only evaluation set with Recall@5 |
| A3 | Understanding and dialogue policy (§6) |
| A4 | Checklist assembly, classification, validation, fallback (§3, §4, §7, §8) |
| A5 | API v2 and persistence |
| A6 | Frontend: one conversational assistant screen |
| A7 | Evaluation, opt-in live-LLM run (synthetic), browser demo, documentation, closure |
