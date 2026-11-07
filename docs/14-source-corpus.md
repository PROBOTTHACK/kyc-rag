# 14 — Source Corpus & Data Layout

Bank: **HDFC Bank**. Regulator: **RBI**. Use only public / authorized documents. Do not redistribute PDFs; commit only the manifest.

## Folder layout
```
data/raw_pdfs/
├── regulatory/
│   ├── 169MD.pdf                                   # RBI (Commercial Banks - KYC) Directions, 2025
│   └── rbi_kyc_faqs_2025-06-09.pdf                 # RBI FAQs on the 2016 Master Direction on KYC
├── policy/
│   └── hdfc_kyc_norms_aml.pdf                      # HDFC KYC Norms and AML FAQ (based on 2004 circular)
├── forms/
│   ├── hdfc_non_individual_account_opening.pdf     # sole prop / HUF / trust / firm / corporate (incl. LLP option)
│   ├── hdfc_nri_nre_nro_account_opening.pdf        # NRE/NRO account-cum-term deposit opening
│   ├── hdfc_nri_rekyc.pdf                          # NRI re-KYC + acceptable document list
│   ├── hdfc_resident_rekyc_dormant.pdf             # resident re-KYC + dormant activation
│   └── hdfc_resident_to_nro_conversion.pdf         # resident -> NRO conversion
├── reference/
│   └── hdfc_netbanking_company_application.pdf     # low priority, no KYC content (optional)
└── manifest.yaml
```

## Planned corpus
| Document | Folder | Answers |
|---|---|---|
| RBI KYC Directions 2025 (169MD.pdf, updated 1 Oct 2026) | regulatory | Current consolidated RBI KYC rules: CDD triggers, individuals, legal entities, beneficial owners, risk categorisation, EDD, PEPs, record retention, re-KYC. |
| RBI FAQs on KYC (dated 9 June 2025; based on the 2016 Master Direction) | regulatory | Interpretations: self-declaration cases, re-KYC channels (Aadhaar OTP e-KYC, V-CIP, branch, digital, email, ATM), re-KYC frequency by risk. |
| HDFC KYC Norms and AML | policy | What the bank requires at opening/update; accepted ID/address documents; KYC triggers. Based on an older RBI circular: label `dated`. |
| HDFC Account Opening Form for Sole Proprietorship / HUF / Trust / Firm / Corporate (`app_trust.pdf`) | forms | Fields and declarations for non-individuals; authorised signatories; HUF declarations. |
| HDFC Resident Individual Re-KYC / Dormant Activation form | forms | Resident ID/address documents; residential, tax and contact details; dormant activation. |
| HDFC NRI Re-KYC form | forms | NRI/PIO/foreign-national proof; Indian and overseas address proofs; change vs no-change flows. |
| HDFC NRE/NRO Account-cum-Term Deposit opening form | forms | NRE/NRO onboarding data, KYC, FATCA/CRS, nominee, operating instructions. |
| HDFC Resident-to-NRO Conversion form | forms | Documents needed when residency status changes. |
| HDFC NetBanking application for companies/trusts (optional) | reference | Board resolution template only; no KYC requirements. Low priority, may be left out. |

Note: the 2016 Master Direction text itself is not in the corpus. The RBI FAQ cites its paragraph numbers, so treat the FAQ as based on the 2016 direction and let the 2025 Directions win on any difference.

## Manifest (`data/raw_pdfs/manifest.yaml`)
The committed manifest is the source of truth (9 entries, verified against the PDFs, see `data/processed/corpus_report.md`). Schema: `app/schemas/corpus.py::DocumentMetadata`. Vocabularies: docs/15. Validate with `python -m app.ingestion.cli validate-manifest` (run in `backend/`).
```yaml
- document_key: hdfc_kyc_aml_faq           # logical identity (stable across versions)
  file: policy/hdfc_kyc_norms_aml.pdf
  sha256: 3ee6834f…                         # pins exact content; a mismatch fails validation, never auto-versioned
  bank: HDFC
  document_type: policy
  source_authority: null                   # the text never names HDFC
  attribution_basis: filename_only
  title: "Frequently Asked Questions on KYC Norms and Anti Money Laundering"
  version: null
  issue_date: null
  effective_from: null
  effective_until: null
  source_status: dated
  status_reason: "States it is based on an RBI circular dated 29/11/2004; ..."
  supersedes: []
  entity_types: [INDIVIDUAL, COMPANY, PARTNERSHIP, TRUST]
  residency_statuses: []
  coverage_notes: ["Document table is indicative only ('not an exhaustive list')."]
  notes: "..."
```
Rules: unknown values stay `null`. `evidence_eligibility` is derived, never written. Entity groups such as `LEGAL_ENTITY` and residency values such as `NRI` are not valid `entity_types`. `supersedes` is only ever set explicitly; a changed file hash never supersedes anything by itself.

## Retrieval scope
- Sources are RBI and HDFC only. Every chunk carries a `bank` field (`RBI` or `HDFC`) so the source hierarchy (RBI > HDFC policy > HDFC forms) can be applied. `bank=HDFC` means the target bank / project context, not a verified publisher; the publisher is `source_authority` (null unless verified), and `attribution_basis` records how it was established (docs/15).
- HDFC forms are `source_status: unknown` (their current validity cannot be verified) and `evidence_eligibility: supporting`. They show what a form requests, not a current obligation.

## Version handling
- The RBI FAQ is based on the 2016 Master Direction. Prefer the 2025 Directions where they differ.
- Prefer the current RBI direction. If an older direction or dated bank document disagrees, flag a conflict and route to human review; never merge silently.
- `source_status` values: `active`, `superseded`, `dated`, `unknown` (docs/15). Dated/superseded/unknown documents may inform but cannot alone support a MANDATORY item (`evidence_eligibility=supporting`).

## Entity coverage (decide after the corpus report)
Supported by the sources: resident individual, NRI, non-individual (the HDFC non-individual form lists proprietorship, partnership, LLP, public/private company, HUF, trust, society and others), re-KYC, dormant activation, resident-to-NRO conversion. For LLP, the HDFC form gives the form-side fields. **Correction (M1 corpus review):** the RBI Directions contain no LLP-specific CDD paragraph ("Limited Liability Partnership" appears only in the sanctions annexes), and the HDFC FAQ covers only individuals, companies, partnership firms and trusts. LLP document requirements therefore stay `UNCLEAR`/`CONDITIONAL`, and which RBI paragraph applies (¶34 or ¶37) is not settled by the corpus. Dormant/inoperative activation is likewise absent from the 2025 Directions; it is covered only by the dated FAQ and an HDFC form.

## Suggested demo questions
- Documents for resident individual re-KYC vs NRI re-KYC.
- Beneficial-owner requirements for a company.
- Dormant account activation steps.
- LLP onboarding: expected answer is `UNCLEAR`/`CONDITIONAL` with the closest RBI provisions cited (¶34/¶37/¶38) plus the HDFC non-individual form; never MANDATORY on this corpus.
- 2016-based FAQ vs 2025 Directions conflict test.

## Git
`.gitignore`: `.env`, `data/raw_pdfs/**/*.pdf`, `data/processed/`. Commit `manifest.yaml` (filenames and metadata only).

## Notes on specific files
- `hdfc_non_individual_account_opening.pdf` is a mostly visual, fold-out layout; plain text extraction is poor. Use it as the main test of the multimodal form pipeline.
- Do not keep duplicate copies of the same PDF; ingestion is idempotent by SHA-256 but the manifest should list each file once.
- `hdfc_kyc_norms_aml.pdf` is dated; it cannot alone support a MANDATORY requirement.
