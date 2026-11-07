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
```yaml
- file: regulatory/169MD.pdf
  bank: RBI
  category: regulatory
  source_authority: RBI
  title: "Reserve Bank of India (Commercial Banks - Know Your Customer) Directions, 2025"
  version: "updated 2026-10-01"
  status: active
  entity_types: [INDIVIDUAL, LEGAL_ENTITY]
- file: regulatory/rbi_kyc_faqs_2025-06-09.pdf
  bank: RBI
  category: regulatory
  source_authority: RBI
  title: "FAQs on Master Direction on KYC dated February 25, 2016"
  version: "2025-06-09"
  status: dated            # based on the 2016 Master Direction; 2025 Directions win on conflict
- file: policy/hdfc_kyc_norms_aml.pdf
  bank: HDFC
  category: policy
  source_authority: null   # leave null until verified
  title: "FAQ on KYC Norms and Anti Money Laundering"
  version: null
  status: dated            # based on a 2004 RBI circular
  entity_types: [INDIVIDUAL, COMPANY, PARTNERSHIP, TRUST]
- file: forms/hdfc_non_individual_account_opening.pdf
  bank: HDFC
  category: form
  title: "Account Opening Form - Sole Proprietorship/HUF/Trust/Firm/Corporate"
  version: null
  status: active
  entity_types: [PROPRIETORSHIP, PARTNERSHIP, LLP, COMPANY, HUF, TRUST, SOCIETY]
- file: forms/hdfc_nri_nre_nro_account_opening.pdf
  bank: HDFC
  category: form
  entity_types: [NRI]
- file: forms/hdfc_nri_rekyc.pdf
  bank: HDFC
  category: form
  entity_types: [NRI]
- file: forms/hdfc_resident_rekyc_dormant.pdf
  bank: HDFC
  category: form
  entity_types: [INDIVIDUAL]
- file: forms/hdfc_resident_to_nro_conversion.pdf
  bank: HDFC
  category: form
  entity_types: [NRI]
- file: reference/hdfc_netbanking_company_application.pdf
  bank: HDFC
  category: reference
  status: active           # no KYC content; optional
```
Rules: unknown values stay `null`. Verify each file's true origin before filling the manifest.

## Retrieval scope
- Sources are RBI and HDFC only. Every chunk carries a `bank` field (`RBI` or `HDFC`) so the source hierarchy (RBI > HDFC policy > HDFC forms) can be applied.

## Version handling
- The RBI FAQ is based on the 2016 Master Direction. Prefer the 2025 Directions where they differ.
- Prefer the current RBI direction. If an older direction or dated bank document disagrees, flag a conflict and route to human review; never merge silently.
- `status` values: `active`, `superseded`, `dated`. Dated/superseded documents may inform but cannot alone support a MANDATORY item.

## Entity coverage (decide after the corpus report)
Supported by the sources: resident individual, NRI, non-individual (the HDFC non-individual form lists proprietorship, partnership, LLP, public/private company, HUF, trust, society and others), re-KYC, dormant activation, resident-to-NRO conversion. For LLP, the HDFC form gives the form-side fields; the documents an LLP must provide come from the RBI Directions, because the HDFC FAQ covers only individuals, companies, partnership firms and trusts.

## Suggested demo questions
- Documents for resident individual re-KYC vs NRI re-KYC.
- Beneficial-owner requirements for a company.
- Dormant account activation steps.
- LLP onboarding: which documents and beneficial-owner details (RBI Directions + HDFC non-individual form).
- 2016-based FAQ vs 2025 Directions conflict test.

## Git
`.gitignore`: `.env`, `data/raw_pdfs/**/*.pdf`, `data/processed/`. Commit `manifest.yaml` (filenames and metadata only).

## Notes on specific files
- `hdfc_non_individual_account_opening.pdf` is a mostly visual, fold-out layout; plain text extraction is poor. Use it as the main test of the multimodal form pipeline.
- Do not keep duplicate copies of the same PDF; ingestion is idempotent by SHA-256 but the manifest should list each file once.
- `hdfc_kyc_norms_aml.pdf` is dated; it cannot alone support a MANDATORY requirement.
