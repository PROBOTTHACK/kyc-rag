# Corpus Report: M1 Phase 1

Status: **draft, awaiting review.** Inspected on 2026-11-07 on branch `feat/m1-document-ingestion`.
Method: PyMuPDF 2.1 in a throwaway scratchpad venv, so no project dependency was added. For every page I recorded the text layer, word count, image coverage, `find_tables()`, fonts, the TOC/bookmarks and the document metadata. The two image-only PDFs were rendered to PNG and **read visually**. No OCR engine is installed (see §6), so nothing in this report comes from OCR output.

Conventions: "page" means the **physical PDF page index (1-based)**. *Verified* means the text appears in the document. *Inferred* means it comes from the filename, PDF metadata or my reading, and it is marked as such. Unknown values stay `null`.

---

## 1. Inventory

9 PDFs. All are distinct by SHA-256: there are no byte-identical duplicates and no file is listed twice. None is encrypted.

| # | File (under `data/raw_pdfs/`) | Pages | Size | SHA-256 | Text layer | Producer (PDF metadata) |
|---|---|---|---|---|---|---|
| 1 | `regulatory/169MD.pdf` | 101 | 718 KB | `a2f5e724f1a21282fddd03e2bae1306eeef34663c953b9e111f8d12bf6bfdbdb` | full (205,411 chars) | Acrobat PDFMaker 26 for Word, created 2026-10-12 |
| 2 | `regulatory/rbi_kyc_faqs_2025-06-09.pdf` | 10 | 262 KB | `a0532fbb8b096053ba1cec6b5ac83393f59fa3f034e6c5f68f623b51e4baa001` | full (21,164) | Acrobat PDFMaker 25 for Word, created 2025-06-09 |
| 3 | `policy/hdfc_kyc_norms_aml.pdf` | 3 | 40 KB | `3ee6834f65604c4b22ea310545b96f4689e6c77544d6e73b891ab54c70b7e931` | full (7,485) | doPDF 5.3 from "Microsoft Word - Tutorial KYC.doc", created 2008-08-12 |
| 4 | `forms/hdfc_non_individual_account_opening.pdf` | 2 | 533 KB | `647610f40ea7c0524de66d22f8bac0a9f0c745f0cd6d8d39ab048465a89b6a8f` | **none (0 chars)**: scanned image | Acrobat 10.1.8 Image Conversion, created 2015-01-07 |
| 5 | `forms/hdfc_nri_nre_nro_account_opening.pdf` | 6 | 564 KB | `189e57a3a24fa34e75613d1300d70fafbd0e8529bc9ec896c40d1a228dae5636` | full (49,978) | Illustrator CS6, title `HDFC-Corp-Account-Cum-Term-Form-J3520`, created 2019-04-20 |
| 6 | `forms/hdfc_nri_rekyc.pdf` | 3 | 553 KB | `fbf2abdcede6d44a23f4ad543d55be6d0a6586961bb49ab608d3d08670730e61` | **none (0 chars)**: scanned image | Acrobat 20.9 Image Conversion, created 2020-08-06 |
| 7 | `forms/hdfc_resident_rekyc_dormant.pdf` | 2 | 255 KB | `d3927dabf8545830202be72e1d0cb6672e0de6c3430d12ab8332c7afa42f7906` | full (5,594) | CorelDRAW 2020, title `93317.cdr`, created 2022-04-27 |
| 8 | `forms/hdfc_resident_to_nro_conversion.pdf` | 6 | 226 KB | `682c52ddd25853701feb7d08878a6857b09612c5a4469acc8d7f424d2e3001a9` | full (35,667) | Illustrator CS6, title `HDFC-NRO-Account-Resident-account-to-Form-J2493-A1934`, created 2018-02-05 |
| 9 | `reference/hdfc_netbanking_company_application.pdf` | 2 | 305 KB | `fee23ba9ffc749bf349a52632bb05d7c26eea96854d1c2fe12aaa448f21a7fc8` | full (2,817) | CorelDRAW 11, title `Netbanking facility for corporate & club.cdr`, created 2006-12-14 |

Total: 135 physical pages. 130 have a usable text layer and **5 are image-only** (#4 p1–2, #6 p1–3).
All eight HDFC files share a `modDate` of 2022-07-23/24. This is circumstantial evidence that they came from the same download or processing batch. It is not proof of origin.

---

## 2. Document descriptions

### 2.1 `169MD.pdf`: RBI KYC Directions 2025 (primary regulatory source)
- **Verified (p1):** "Reserve Bank of India (Commercial Banks – Know Your Customer) Directions, 2025 (Updated as on October 1, 2026)"; reference `RBI/DOR/2025-26/169`, `DOR.AML.REC.No.88/14.01.002/2025-26`, dated **November 28, 2025**. Signed by a Chief General Manager (p74).
- **Applicability (p4, ¶3):** commercial banks, meaning banking companies other than SFBs, Payments Banks and LABs, plus corresponding new banks and SBI. HDFC Bank is a banking company, so it falls in scope (*inferred*: the document does not name HDFC).
- **Effective date (¶2):** "on the day they are placed on the official website of the RBI". **That date is not stated in the PDF**, so `effective_from = null`. 2025-11-28 is the *issue* date, not a verified effective date.
- **Amendments:** incorporated as footnotes. Seen: an insertion w.e.f. 2025-12-29 (KYC Amendment Directions 2025; p19), a substitution w.e.f. 2026-10-01 (Governance Amendment Directions 2026; p18), and an insertion w.e.f. 2026-09-18 (KYC Amendment Directions 2026; p6). The footnote numbers appear inline in the body text (e.g. `{FEMA 5(R)}, 1and Foreign`, `[3Explanation`).
- **Repeal (p73, ¶80):** "the existing Directions, instructions, and guidelines relating to Know Your Customer as applicable to Commercial Banks stand repealed, as communicated vide circular DOR.RRC.REC.302/33-01-010/2025-26 dated November 28, 2025." The repeal circular itself is **not in the corpus**, so I cannot confirm here which instruments it lists (see §7).
- **Structure:** a 29-entry bookmark tree (Chapters I–XI, sub-parts A–F, F.1/F.2, Annex I–II) whose pages match the printed TOC. There are 113 numbered paragraphs (`NN. …`) with `(1)`, `(i)`, `(a)` sub-levels. **Printed page number = physical page on every page except p1**, which carries no number.
- Note: the printed TOC on p1 **omits Chapter III** (Customer Acceptance Policy, p20), but the bookmarks include it. Use bookmarks, not the printed TOC.

| Chapter / part (bookmark) | Pages | KYC relevance |
|---|---|---|
| I Preliminary: title, applicability, **definitions** (BO, CDD, OVD, certified copy, PEP, periodic updation, V-CIP …) | 4–15 | high |
| II General: Board/policy, Designated Director, other general guidelines | 16–19 | medium |
| III Customer Acceptance Policy | 20–21 | high |
| IV Risk Management | 22 | high |
| V Customer Identification Procedure | 23–24 | high |
| VI-A CDD for individuals (OVDs, e-KYC, digital KYC, V-CIP, small accounts) | 25–35 | **very high** |
| VI-B Sole proprietary firms | 36 | very high |
| VI-C Legal entities: ¶33 company (p37), ¶34 partnership (p37–38), ¶35 trust, ¶36 unincorporated association/BOI (p38), ¶37 other juridical persons (p39) | 37–39 | very high |
| VI-D Identification of beneficial owner (¶38) | 39 | very high |
| VI-E On-going due diligence, incl. **¶42 updation/periodic updation (re-KYC)** | 40–44 | very high |
| VI-F.1 EDD (incl. PEPs, non-face-to-face) / F.2 SDD (incl. FPI document table) | 45–51 | high |
| VII Record management | 52–53 | medium |
| VIII Reporting to FIU-IND | 54–55 | low–medium |
| IX International agreements / sanctions lists (UAPA, WMD) | 56–59 | medium |
| X Other instructions (CKYCR, FATCA/CRS, etc.) | 60–72 | medium |
| XI Repeal, other laws, interpretation | 73–74 | metadata |
| Annex I: MHA UAPA s.51A order (2021, corrigenda 2023/2024) | 75–88 | low (procedural, government text) |
| Annex II | 89–101 | low (bookmark title only, not inspected in detail) |

### 2.2 `rbi_kyc_faqs_2025-06-09.pdf`: RBI FAQs
- **Verified (p1):** "FAQs on Master Direction on KYC dated February 25, 2016 (June 9, 2025)". 37 Q&As, with an asterisk marking updated items (Q24, Q36).
- It cites 2016 MD paragraph numbers (¶16, ¶18, ¶23, ¶56(j)). These do **not** map onto the 2025 Directions' numbering (for example, the FAQ's ¶23 small-account reference corresponds to ¶28 in the 2025 Directions). Citations must point to the FAQ itself and never be translated into 2025 paragraph numbers.
- It was issued before the 2025 Directions (2025-06-09 vs 2025-11-28).
- Useful content: KYC triggers (Q2), individual documents (Q5), small accounts (Q6), deemed OVDs (Q7), name change (Q9), Aadhaar not mandatory (Q10), joint accounts (Q12), onboarding modes (Q13), CKYC identifier (Q14–18), V-CIP (Q19–21), **re-KYC periodicity 2/8/10 years** (Q22), risk parameters (Q23), **re-KYC modes and documents** (Q24–25), non-compliance (Q28), PwD (Q33–35), **inoperative account reactivation** (Q36–37, footnote citing circular DOR.SOG(LEG).REC/64/09.08.024/2023-24 dated 2024-01-01).

### 2.3 `hdfc_kyc_norms_aml.pdf`: "FAQ on KYC Norms and Anti Money Laundering"
- **Verified:** the title "Frequently Asked Questions on KYC Norms and Anti Money Laundering", and its basis: "As per RBI guidelines issued vide their circular dated 29/11/2004".
- **The word "HDFC" never appears in the text.** Attributing it to HDFC rests only on the filename and the source of the download. `bank: HDFC` is therefore *inferred*, and `source_authority` must stay `null`.
- PDF created 2008-08-12 from a Word document titled "Tutorial KYC.doc". No version or date is printed.
- Content: definitions (ML, KYC, KYC policy, customer), CAP/CIP, and a **features → documents table** for individuals, companies, partnership firms and trusts/foundations, followed by "When does KYC apply?". It ends with: "this is not an exhaustive list and is only indicative".
- Several requirements are now **stale against the 2025 Directions**. For example, it accepts a PAN card as identity proof and a ration card or bank statement as address proof, with utility bills "not older than 3 months". The RBI FAQ Q7 gives 2 months.

### 2.4 `hdfc_non_individual_account_opening.pdf`: non-individual account opening form (scanned)
Read visually from renders, *not OCR*.
- Title on the page: "HDFC BANK, ACCOUNT OPENING FORM, FOR SOLE PROPRIETOR/HUF/TRUST/FIRM/CORPORATE (To be filled by applicant only)".
- **Fold-out layout:** each physical page (1432×894 pt, landscape) carries two logical pages. Physical p1 = printed "Page 4 of 4" (nomination DA1 for sole proprietorship, authorised-signatory photos, bank-use block) + "Page 1 of 4" (account type, PAN/Form 60, mailing and registered address, contact, business details). Physical p2 = "Page 2 of 4" (operating instruction, credit facilities, FD/RD instructions, TDS, **declarations for HUF, partnership firm and sole proprietorship**) + "Page 3 of 4" (up to 3 authorised signatories' details, DNC, introducer).
- Visible vertical code: **`92713/07.01.2015`**. Read visually; it needs OCR confirmation.
- "Type of Entity" checklist on the form: Proprietorship, Partnership, **Limited Liability Partnership**, Public/Private Limited Company, Government, Bank, Societies, Insurance, Self Help Group, HUF, Foreign Bodies, Non Government Organizations, Mutual Fund, Association, Clubs, Trust, plus subcategories.
- **It contains no list of required KYC documents**; it only says "Please staple the relevant documents in the box provided". It shows what is captured, not what is legally required (docs/04 `appears_on_form` ≠ `mandatory_under_policy`).
- Scan quality: an embedded JPEG of about 1989×1242 px, roughly **100 dpi** at page size. Pink background, light-blue boxes, small print (the TDS notes and the rotated "Instructions" column on the p2 left margin are about 5–6 pt), rotated (90°) margin text and handwriting-style box grids. **OCR of the small print is expected to be poor** (not yet measured).

### 2.5 `hdfc_nri_nre_nro_account_opening.pdf`: NRE/NRO account-cum-term-deposit opening
- Title on the page (largest font, p1): "ACCOUNT CUM TERM DEPOSIT OPENING FORM, FOR NON RESIDENT - INDIVIDUAL CUSTOMERS". PDF title code `J3520`.
- A dense 6-page multi-column form. It has a text layer, but reading order interleaves columns, and some sentences appear twice (the same declaration printed for the 1st and 2nd applicant).
- KYC-relevant prose: PEP definition (p2); FATCA/CRS self-certification, **documents to cure FATCA/CRS indicia** and documents establishing tax residence (p4); seafarer/contract-worker documents (passport exit/entry stamps, contract, visa, CDC); declarations A–Y including FEMA/residency undertakings and voluntary Aadhaar (p4); **PIO declaration** (p5); **close-relative address-proof declaration**, non-English document declaration, Saudi IQAMA declaration, minor declaration (p6). Notes such as "All necessary documentation as mandated by the regulatory / bank authorities should be provided" (p5).
- `find_tables()` detects 1–3 grid tables per page. These are form grids, not data tables.

### 2.6 `hdfc_nri_rekyc.pdf`: NRI re-KYC (scanned)
Read visually, *not OCR*.
- p1–2: "NRI CUSTOMER UPDATION FORM FOR RE-KYC". Fields: customer ID, PAN, occupation and income, change-of-address options (none / Indian / overseas / contact), overseas and Indian addresses, documents submitted (Passport, Visa/Resident Card, OCI/PIO, Address Proof). **Notes (p2):** "You have to attach self attested Indian passport, visa & any of the address proof either Indian or Overseas" and "Please provide self attested passport, visa & address proof even when there are no changes of your addresses."
- **p3: "NON RESIDENT INDIVIDUALS: Comprehensive list of acceptable documents for proof of identity & proof of address".** Sections: Mandatory document (PAN / Form 60); Proof of Identity (A); Proof of NRI/PIO status (B); Proof of Address (C), overseas and Indian; Additional Document (D). This is the **single most useful form page in the corpus for NRI questions**, and it is image-only.
- Code `93316/05.08.2020` (read visually). Scan: 1241×1753 px, about **150 dpi**, clean white background and clear print. OCR should work well (not yet measured).

### 2.7 `hdfc_resident_rekyc_dormant.pdf`: resident re-KYC + dormant activation
- Title on the page: "Re-KYC and Dormant Activation form for Resident Individuals". Code `93317/03.02.2021` (text layer, p1).
- p1: declaration ("information … is up to date"; self-attested address and identity proof; photo; "There is no change in mailing & permanent address"), personal and occupation details, and **"DORMANT ACTIVATION (for dormant account activation branch visit is mandatory)"**.
- p2: "APPLICATION FOR ACCOUNT OPENING USING AADHAAR / E-KYC FOR CUSTOMERS NOT OPTING FOR DBT": Aadhaar consent, and "submission of Aadhaar is not mandatory".
- Text layer: field labels are fragmented, with vertical lettering extracted one character per line (`S U R N A M E`, `N U M B E R`) and typographic ligatures (`ﬁ`, `ﬄ`).

### 2.8 `hdfc_resident_to_nro_conversion.pdf`: resident → NRO conversion
- Title on the page: "RESIDENT ACCOUNT TO NRO ACCOUNT CONVERSION FORM, FOR NON RESIDENT - INDIVIDUAL CUSTOMERS". PDF title code `J2493-A1934`.
- "I/we hereby request you to convert my resident a\c to NRO a\c as I have become NRI" (p1); redesignated-NRO and debit-card options; PEP definition; PIO declaration; close-relative, non-English and Saudi declarations; seafarer documents; FATCA/CKYC consent.
- **There is no explicit list of documents required for conversion.** Documents appear only inside the declarations (passport, contract, visa, CDC).

### 2.9 `hdfc_netbanking_company_application.pdf`: NetBanking for company/club (reference)
- NetBanking application and a board-resolution template for Company / Trust / Society / AOP / Club (footer "Netbanking facility for company-clubDec06"). **No KYC requirements.** Matches docs/14 ("low priority, may be left out").

---

## 3. Text extraction quality

| Doc | Quality class | Issues found | Needs OCR |
|---|---|---|---|
| 169MD | **text, good** | running page number at top of each page; garbled Devanagari header on p1 (`भारतीय įरज़वर् बैंक`); inline footnote markers and footnote text in the body flow; TOC dot leaders (p1–2); signature/blank pages (p55, p74 are legitimately short, 150–160 chars) | no |
| RBI FAQ | **text, good** | page number line; one footnote (p9); `₹` symbol preserved | no |
| HDFC KYC norms | **text, good; table degraded** | 2-column features/documents table: plain text keeps row order but separates "features" from "documents"; `find_tables()` detects it but **drops spaces in the left column** (`10Accountsof . individuals`), so it is unusable; the documents list for individuals mixes identity and address proofs without the 1.1/1.2 split | no |
| Non-individual form | **scanned, poor-res** | 0 chars; ~100 dpi; fold-out 2-up pages; rotated text; coloured background | **yes (both pages)** |
| NRE/NRO form | **text, layout-noisy** | multi-column interleaving; duplicated lines; field-label fragments; `find_tables` hits are form grids | no |
| NRI re-KYC | **scanned, good-res** | 0 chars; ~150 dpi; clean | **yes (all 3 pages)** |
| Resident re-KYC/dormant | **text, layout-noisy** | one-char-per-line vertical labels; ligatures | no |
| Resident→NRO | **text, layout-noisy** | as NRE/NRO | no |
| NetBanking | text, fair | template blanks (`____`) | no |

Quality signal: **character count alone is not a safe OCR trigger.** 169MD p55/p74 have fewer than 200 characters but are genuine text pages. The scanned pages are distinguished by **0 text chars and one image covering 100% of the page** (169MD's pages carry only a small logo image covering 0.3%). Recommended trigger: `chars < 50 AND image_cover > 0.8`, with an alphabetic-ratio check for garbage text layers. None of the text layers here contain garbage: replacement-character count is 0 everywhere and the printable ratio is ≥ 0.997.

---

## 4. Entity coverage (by source, verified from text)

| Entity / scenario | RBI Directions 2025 | RBI FAQ (2016-based) | HDFC KYC norms (2004-based) | HDFC forms |
|---|---|---|---|---|
| Resident individual onboarding | ✔ VI-A (p25–35) | ✔ Q5–13 | ✔ table 1.0 | non-individual form n/a; resident re-KYC form |
| Re-KYC / periodic updation | ✔ ¶42 (p41–44): 2/8/10 years | ✔ Q22–28 | "after periodic intervals" only | ✔ resident re-KYC; ✔ NRI re-KYC |
| Dormant / inoperative activation | ✘ **term not found** ("dormant" and "inoperative" both 0 hits) | ✔ Q36–37 | ✘ | ✔ resident form p1 ("branch visit is mandatory") |
| NRI / PIO | ✔ certified-copy rules for NRIs/PIOs (¶5 definitions, p6) | ✔ Q13 (NRI/PIO certified copies) | ✘ | ✔ NRE/NRO, NRI re-KYC (doc list p3), resident→NRO |
| Sole proprietorship | ✔ VI-B ¶30–32 (p36–37) | V-CIP mention (Q21) | ✘ | ✔ non-individual form (declaration, nomination) |
| Company | ✔ ¶33 (p37) | BO via V-CIP (Q21) | ✔ table 2.0 | ✔ non-individual form |
| Partnership firm | ✔ ¶34 (p37–38); BO definition (p5) | — | ✔ table 3.0 | ✔ non-individual form (declaration) |
| **LLP** | ⚠ **no LLP-specific CDD paragraph.** "Limited Liability Partnership" appears only in Annex I (UAPA order, p82) and Annex II (p96), both sanctions-procedure text | — | ✘ | ✔ listed as entity type on non-individual form |
| Trust | ✔ ¶35 (p38) + ¶37 proviso (p39) | — | ✔ table 4.0 | ✔ non-individual form |
| HUF | ⚠ only in the definition list (p9, "a Hindu undivided family") | — | ✘ | ✔ HUF declaration on non-individual form |
| Society / association / club / juridical person | ✔ ¶36–37 (p38–39) | — | (foundation/association in 4.0) | ✔ non-individual form; NetBanking resolution |
| Beneficial owner | ✔ definition (p5) + ¶38 (p39) | — | listed under "customer" | BO fields not visible on non-individual form |
| PEP / EDD | ✔ VI-F.1 (p45+; PEPs p46–47) | — | ✘ | ✔ PEP definition on NRI forms |
| FPI | ✔ SDD table (p49–51), extracts cleanly with `find_tables()` | — | ✘ | ✘ |

---

## 5. Duplicates and version concerns
1. **No byte duplicates** (9 unique SHA-256 values).
2. **Near-duplicate content:** 85 of 172 long prose lines (~49%) in the resident→NRO form (J2493, created 2018) also appear in the NRE/NRO form (J3520, created 2019): PEP definition, PIO declaration, close-relative declaration, Saudi declaration and seafarer documents. Their section letters differ (e.g. close-relative is "S)" in one and "P)" in the other). Both are separate, legitimate documents, so keep both. Near-identical chunks will crowd top-5 results and should be measured in eval. Cross-document deduplication is not proposed for M1.
3. **RBI FAQ vs 2025 Directions:** the FAQ explains the 2016 MD, which ¶80 of the 2025 Directions appears to repeal for commercial banks, but the list of repealed instruments is not in the corpus. Keep `status: dated` per docs/14. Paragraph references differ between the two instruments.
4. **HDFC KYC norms (2004 basis) vs 2025 Directions:** conflicting specifics (PAN card as identity proof, ration card, 3-month utility bills vs 2 months in FAQ Q7). This is a good conflict test case (docs/14 demo question 5). `status: dated`.
5. **Form currency:** all forms carry 2015–2022 print codes or creation dates. HDFC may have issued newer versions, and the corpus cannot verify that these are current. *Decision (Phase 2 review):* all forms and the NetBanking application are `source_status: unknown`. They remain retrievable as supporting (NetBanking: context-only) evidence of what a form requests, not of a current obligation.
6. **Re-KYC periodicity:** the FAQ (Q22) and the Directions (¶42, p41) agree on 2/8/10 years, so there is no conflict there.

---

## 6. Missing documents, inputs and metadata
**Missing inputs**
- **Tesseract OCR binary is not installed** (`tesseract` is not on PATH). *Resolved in M1 Phase 2 (2026-11-07): Tesseract 5.4.0 (UB Mannheim build, `eng` + `osd`) installed via winget.* docs/02 names "Tesseract OCR fallback". It is needed for 5 pages, including NRI re-KYC p3.
- PyMuPDF, sentence-transformers, BGE-small weights and a YAML parser are not in the project venv (M0 deferred them).

**Missing source documents** (not required, but they explain the gaps above)
- The repeal circular `DOR.RRC.REC.302/33-01-010/2025-26` (would confirm the FAQ/2016 MD status).
- The 2016 Master Direction itself (docs/14 already notes this).
- An RBI or HDFC source on **LLP**-specific KYC documents and on **dormant/inoperative** accounts. The inoperative-accounts circular dated 2024-01-01 is cited in FAQ Q36 but not included.
- A **current** HDFC KYC policy or acceptable-documents list for resident individuals. Only the NRI list (re-KYC p3) exists.

**Metadata that must stay `null`**
| Doc | Field | Why |
|---|---|---|
| 169MD | `effective_from` | ¶2: effective on placement on the RBI website; date not stated |
| RBI FAQ | `effective_from` | not stated; 2025-06-09 is the publication date in the title |
| HDFC KYC norms | `source_authority`, `version`, `effective_from` | no issuer, version or date printed; "HDFC" not in the text |
| All forms | `effective_from`, `effective_until` | not printed; print codes (`92713/07.01.2015`, `93316/05.08.2020`, `93317/03.02.2021`, `J3520`, `J2493-A1934`) are form identifiers, not effective dates |
| All | `effective_until` | nothing in the corpus states an expiry |

---

## 7. Risks and ambiguities (need a decision)
1. **LLP documents.** docs/14 says the LLP document list comes from the RBI Directions. The Directions have **no LLP paragraph**. Whether ¶34 (partnership firm) or ¶37 (juridical persons "not specifically covered") applies is a legal interpretation the corpus does not settle. Per CLAUDE.md principle 1, LLP document requirements must come out as `UNCLEAR`/`CONDITIONAL`, not MANDATORY. docs/14's demo question 4 and the "Entity coverage" note should be corrected.
2. **Dormant activation** has no 2025-Directions support. Only the FAQ (`dated`) and an HDFC form cover it, so on this corpus nothing about it can be MANDATORY.
3. **FAQ status `dated` vs `superseded`.** ¶80 suggests repeal, but the corpus can't prove the 2016 MD is in the repeal list. I recommend keeping `dated` (spec value) and recording the ¶80 note in the manifest.
4. **HDFC attribution of `hdfc_kyc_norms_aml.pdf`** is inferred from the filename only.
5. **OCR quality on the non-individual form** (~100 dpi, coloured background, small print) is unknown until measured, and may be poor for the fine print.
6. **Fold-out pagination:** citing "page 1" of the non-individual form covers printed pages 4 and 1. Citations remain correct as physical pages, but may confuse a human reviewer.
7. **Footnotes in 169MD** carry amendment dates. If they stay inline, they split sentences and attach "w.e.f." dates to the wrong text. If they are dropped, provenance of amended clauses is lost.
8. **Entity-type vocabulary** in docs/14 is inconsistent (`LEGAL_ENTITY` vs `COMPANY`/`PARTNERSHIP`/…, `NRI` vs `INDIVIDUAL`). A canonical enum is needed before the manifest is validated.
9. `data/processed/*` is gitignored, so **this report will not be committed** unless an exception is added.

---

## 8. Recommendations for ingestion
**Scope:** run all 9 PDFs through Pipeline A text indexing, with `document_type` distinguishing `regulatory | policy | form | reference`. Forms are still indexed as passages (so the NRI document list and dormant-activation text are retrievable, and OCR is exercised). Structured form extraction remains Pipeline B (M2). The NetBanking reference is optional, and I would include it as `reference` so filters can exclude it.

**Validation:** check the PDF header and that the file opens; reject encrypted files; SHA-256 on bytes; manifest entry required (an unknown file is skipped and logged); page count > 0.

**Quality and OCR:** per page, `chars < 50 AND image_cover > 0.8` (or alpha ratio < 0.4) → OCR via PyMuPDF's built-in Tesseract integration (`page.get_textpage_ocr(dpi=300, full=True)`), which needs only the Tesseract binary and no extra Python binding. Record `extraction_method` (`text | ocr`) and an OCR confidence per page. A failed or low-confidence page is **kept and flagged** (`quality: ocr_low_confidence` / `failed`), never silently dropped.

**Cleaning:** NFKC (fixes ligatures); strip the running page-number line and TOC dot-leader pages; drop the garbled Devanagari header line; collapse one-char-per-line vertical labels; remove exact duplicate lines within a page (form multi-column repeats); preserve `₹`.

**Footnotes (169MD):** detect them by font size and position (bottom block, small font, leading number), remove them from the body flow, and attach each to the chunks on that page as `metadata.footnotes`. Amendment provenance is preserved without corrupting sentences.

**Chunking (section-aware, per document type):**
- *169MD:* section = bookmark path (e.g. `Chapter VI > C. CDD Measures for Legal Entities`); chunk = one numbered paragraph (`NN.`) with its sub-clauses. A parent–child split happens only when a paragraph exceeds the token budget (children at `(1)/(i)` boundaries, parent = whole paragraph). Paragraphs that cross pages keep `page_number` = start page plus `page_end`. Annex tables (FPI, p49–51) go in as table chunks via `find_tables()` rendered to Markdown.
- *RBI FAQ:* one chunk per `Q n` (question + answer), section = `Q n`.
- *HDFC KYC norms:* one chunk per question heading. The documents table is chunked per row (1.0/2.0/3.0/4.0) from plain-text order, **not** from `find_tables()`.
- *Forms:* section = largest-font headings and lettered declaration headers (`S) CLOSE RELATIVE DECLARATION`); prose blocks of 40 characters or more are kept, and runs of bare field labels are merged into a single "fields on this page" chunk. NRI re-KYC p3 is chunked by its (A)–(D) headings after OCR.

**Metadata (chunk = Qdrant payload = Postgres row):** `document_id, document_name, file, sha256, bank, document_type, status, source_authority, version, effective_from, effective_until, entity_types, section, page_number, page_end, extraction_method, chunk_index, parent_chunk_id, content`. Unknown = `null`. `status` and `document_type` are not columns in docs/08 `chunks`; I'd add them via the first Alembic migration (alongside `documents` and `chunks`) and add a `status` payload index in Qdrant.

**Embeddings:** `BAAI/bge-small-en-v1.5` (384-d, cosine) via `EMBEDDING_MODEL`. Passages are embedded without a prefix, and queries use the BGE retrieval instruction prefix. Normalise embeddings and batch deterministically (sorted by chunk id).

**Idempotency:** if the file SHA-256 has already been ingested, skip it. Chunk IDs are deterministic (`uuid5(sha256 + chunk_index)`), so re-runs upsert the same points/rows. A changed file means a new document row; the old one is marked `superseded`, never merged.

**Evaluation:** 15+ hand-labelled queries grounded in pages verified above (e.g. company documents → 169MD p37; re-KYC periodicity → 169MD p41 + FAQ p6; NRI address proof → NRI re-KYC p3; dormant activation → resident form p1 + FAQ p9–10; FPI documents → 169MD p49–50; LLP → expect UNCLEAR-type evidence). The query set will be drafted after approval and is not part of this report.
