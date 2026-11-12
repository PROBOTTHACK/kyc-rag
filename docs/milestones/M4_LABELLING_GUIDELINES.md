# M4 Labelling Guidelines: claim verification datasets

> Independent educational prototype. Labels describe what a stored passage **says**, not what the law requires. Every report requires human review.

**Status:** committed in M4 S3, before any NLI model has been run on these cases. **Label version:** 1. **Revised 2026-11-12** with the owner's rule clarifications (rules 1, 3, 10 and the modality example in rule 2) after the dev review. Decisions are recorded in `data/eval/m4_claims_dev_review.jsonl`.
**Datasets:**
- `data/eval/m4_claims_dev.jsonl` (development: model and threshold selection in S4);
- `data/eval/m4_claims_heldout.jsonl` (held-out: frozen, run once in S8, never used to choose or tune anything).

## 1. What a label answers
One question only: **does the cited stored text, read on its own, entail the claim, contradict it, or neither?**

| Gold label | Meaning | Rule of thumb |
|---|---|---|
| `entailed` | A careful reader of the quoted passage alone would agree the claim is true. | Paraphrase allowed; a plain expansion of an acronym that the text itself uses is allowed. |
| `contradicted` | The passage states something incompatible with the claim. | The claim **permits what the text forbids**, **forbids what the text permits**, negates what the text states, or changes a number so that it conflicts. |
| `insufficient` | The passage neither supports nor rules out the claim. | The claim **requires something the text does not require**, adds a document, widens the scope, drops a stated condition, or is about another entity, subject or topic. |

**Not part of the semantic label:** source eligibility, entity-scope eligibility, authority and legal obligation. They are recorded separately (`expected_obligation`, `match_scope`, evidence metadata), and the S2 decision table combines them. A form can entail "the form asks for X" and still say nothing about whether X is legally mandatory.

## 2. Rules
1. **No outside knowledge.** Use only the cited passage(s). Do not use general banking knowledge, other paragraphs, other documents or the taxonomy. Example: whether an LLP is a "legal person" is not stated in paragraph 38, so a claim about LLPs is `insufficient` against it (this is an `entity_group` case).
   - **Abbreviations (owner decision, 2026-11-12):** an abbreviation or expansion is allowed only when it is explicitly established by the source (e.g. "Self Help Groups (SHGs)", "Goods and Services Tax (GST)") or by an approved project terminology mapping. When the cited chunk does not establish it, cite the chunk of the same source that does (m4d-012 and m4d-029 cite the paragraph 49 heading "Foreign Portfolio Investors (FPIs)"). Never use assumed synonyms or outside knowledge to manufacture entailment.
   - **Approved project terminology mapping:** none yet. A mapping must be approved by the owner and listed here before any label relies on it.
2. **Polarity and modality matter.** "shall" ≠ "may"; "shall not" ≠ "need not" ≠ "may".
   - **`insufficient`:** a claim of "must" where the text only says the act "may" be done (the text neither requires nor excuses it; e.g. "FPIs may submit an undertaking").
   - **`contradicted`:**
     - a claim that turns a prohibition into a permission;
     - a claim of "must" where the text expressly allows *not* doing it ("may, at their option, not issue UCIC to all walk-in customers"; "shall not require CDD of all members"), or states that it is voluntary or not mandatory, **provided the subject and conditions match** (m4d-053).
3. **Numbers (owner decision, 2026-11-12).** A different number is `contradicted` only when the same quantity, entity, conditions and scope are established **and** the two requirements are mutually incompatible, i.e. no case can satisfy both.
   - **`contradicted`:**
     - "may pay cheques presented up to six months after their date" vs "shall not make payment … beyond the period of three months". A 4-month-old cheque is allowed by the claim and forbidden by the text.
     - "only once every five years" vs "at least once in every two years". A five-year cadence satisfies the claim and breaks the text.
   - **`insufficient`:**
     - a stricter number that can be satisfied together with the text's ("at least every 2 years" vs "at least once in every eight years"; "not more than two months old" vs "not older than 3 months");
     - a number for a quantity the text does not state (a completion window, a review cycle).
4. **Lists.** "The bank shall obtain each of the following: (1)…(7)" entails each listed item for that entity. A document not in the list is `insufficient`, not `contradicted`: the list does not say nothing else may be asked.
5. **Conditions.** A claim that drops a condition stated in the text (e.g. "if PAN is not provided", "where GST details are available") is `insufficient`.
6. **Entity and subject.** A claim about a different entity type (company vs trust) or a different person (employee vs designated partner) is `insufficient`, unless the text itself states the relationship (e.g. paragraph 36 states that unregistered trusts are included).
7. **Dated and non-primary sources.** Label the semantics exactly as for active sources. Their status is recorded as metadata and in the `source_status` perturbation tag. A dated source is never relabelled because a newer source says something else, and conflicts between sources are never resolved by date.
8. **OCR text.** Label what the stored OCR text visibly says. If garbling makes the meaning unclear, the label is `insufficient`. OCR-uncertain evidence never makes `expected_obligation` true.
9. **Forms.** A form passage can entail only claims about what the form shows or asks for (`form_request`). An obligation claim cited only to a form is never `entailed` as an obligation.
10. **`expected_obligation`** is true only when **all** of these hold: the gold label is `entailed`; the claim type is `policy_obligation`; every cited chunk is `primary`, active and not OCR-uncertain; `match_scope` is `entity`; and **the entity scope is known**. It records whether this evidence may support a MANDATORY item. It does not mean the item is legally mandatory for any real customer.
    - **Entity scope (owner decision, 2026-11-12):** an entity-specific mandatory obligation cannot be established if the relevant entity scope is unknown. For a claim without an entity (`entity: null`), `expected_obligation` is false unless the claim and the evidence explicitly establish the relevant entity scope and applicability. Evidence about one class (FPIs, high-risk customers, the bank's own screening duties) is never generalised to all entities.

## 3. Fields
| Field | Content |
|---|---|
| `id` | `m4d-NNN` (dev) or `m4h-NNN` (held-out) |
| `split` | `dev` or `heldout` |
| `claim` | the hypothesis, written by the annotator (synthetic, never customer data) |
| `claim_type` | `policy_obligation`, `form_request`, `supporting_statement` or `explanatory_claim` (M4 plan §D) |
| `entity` | an `EntityType` value, or null when the claim is not about one customer type |
| `match_scope` | `entity` or `entity_group` (how the evidence relates to `entity`) |
| `evidence` | a list of `{chunk_id, document_key, page, quotes[]}`. Each quote is an exact substring of the stored chunk text (whitespace-normalised), checked against Postgres. |
| `gold` | `entailed`, `contradicted` or `insufficient` |
| `expected_obligation` | boolean, see rule 10 |
| `perturbations` | tags from the vocabulary in §4 |
| `rationale` | one or two sentences on why the label holds |
| `annotator` | `implementing_assistant` |
| `owner_review` | `pending` / `accepted` / `changed` |
| `owner_note` | null, or the owner's note |
| `second_review` | null, or `{reviewer, verdict, note}` from an independent reviewer |
| `label_version` | 1 |

## 4. Perturbation vocabulary
| Tag | What it tests |
|---|---|
| `paraphrase` | same meaning, different words |
| `verbatim_fact` | near-verbatim restatement |
| `negation` | a "not" / "no" / "need not" added or removed |
| `polarity_flip` | prohibition ↔ permission, or exempted ↔ mandatory |
| `modality_change` | shall ↔ may (requirement vs option) |
| `number_change` | a number, amount or period changed |
| `acronym_variant` | PAN / Form No. 60 / OVD / UCIC / GST / FPI / SHG / V-CIP, spelled out or abbreviated |
| `entity_swap` | the claim is about another entity type |
| `subject_swap` | the claim is about another person or party |
| `scope_widening` | the claim extends a statement beyond what the text covers |
| `condition_dropped` | a stated condition is left out |
| `document_swap` | a document the text does not list |
| `form_only` | all evidence is form text |
| `entity_group` | the evidence speaks of a group (legal persons, juridical persons), not the entity |
| `ocr_uncertain` | the evidence chunk is OCR-uncertain |
| `source_status` | the evidence is a dated source (RBI FAQ, HDFC FAQ) |

The integrity tests check the tags that can be checked against the database: `ocr_uncertain` (stored `text_reliability`), `source_status` (stored `source_status=dated`) and `form_only` (stored `document_type=form`).

## 5. Examples for every class (from the dev set)
- **entailed:** "The trust deed must be obtained for opening an account of a trust." ← RBI Directions 2025 ¶35: "For opening an account of a trust, the bank shall obtain the certified copies of each of the following documents … (2) Trust deed". `expected_obligation: true`.
- **entailed, not an obligation:** "The account opening form asks for Form 60/61 to be attached for existing customers." ← HDFC non-individual form p.1 (OCR-uncertain): "Form 60/61 attached for existing customers". `expected_obligation: false` (form, OCR).
- **contradicted (negation):** "Aadhaar number is mandatory for all customers for KYC purposes." ← ¶23: "Explanation 4: Aadhaar number is not mandatory for purposes of KYC."
- **contradicted (number):** "The bank may pay cheques presented up to six months after their date." ← ¶67: "beyond the period of three months".
- **contradicted (modality, same subject and condition):** "Customers who do not seek any benefit or subsidy must provide their Aadhaar number for KYC." ← ¶23: "Aadhaar number is not mandatory … In other cases, customers may provide the Aadhaar number voluntarily." (m4d-053)
- **insufficient, cross-source conflict kept visible:** "A telephone bill used as proof of address must not be more than two months old." ← HDFC FAQ (dated): "Telephone bill (not older than 3 months)". Both requirements can be met at once, so the case is `insufficient` under rule 3 (m4d-032). The RBI FAQ's two-month rule for utility bills is m4d-013 (`entailed`). Both stay visible as a pair, and neither source is selected, by date or otherwise.
- **insufficient (dropped proviso, claim kept as written):** m4d-010 and m4d-005 omit provisos stated in the same paragraph. The verifier must not repair them.
- **insufficient (entity swap):** "The trust deed must be obtained for opening an account of a company." ← ¶35 (trusts).
- **insufficient (entity group):** "For opening an account of an LLP, the bank shall identify its beneficial owners." ← ¶38 ("a Legal Person who is not a natural person"): LLP membership is not stated.
- **insufficient (modality):** "FPIs must submit an undertaking …" ← "FPIs concerned may submit an undertaking" (held-out case).

## 6. Splits and leakage controls
- **No shared evidence:** no stored chunk is cited by both splits. The splits use different paragraphs, FAQ questions, FAQ table rows and forms (dev: ¶17, 23, 33, 35, 38, 42(1), 43, 47, 58, 67, 69, FPI PAN and financial-data rows, RBI FAQ Q4/Q7/Q10/Q22, HDFC FAQ rows 1.0 and 4.0, the non-individual and resident re-KYC forms; held-out: ¶18, 19, 25, 28, 31, 32, 34, 36, 37, 42(6)(7), 44, 70, 71, 76, FPI SEBI row and note, RBI FAQ Q5/Q12/Q20/Q32, HDFC FAQ rows 2.0 and 3.0, the NRI re-KYC, NRI account-opening and resident-to-NRO forms).
- **No near-duplicate claims:** no pair of claims across the splits has token Jaccard similarity ≥ 0.8 (a test enforces this).
- **Shared phrasing patterns** across entity types ("X must be obtained for opening an account of Y") are intentional: they test the same skill on different, unseen evidence.
- **Frozen held-out set:** its SHA-256 (LF line endings) is recorded in `docs/10-evaluation-plan.md` and checked by `tests/unit/test_m4_datasets.py`. Label errors found later go into a new versioned file; the original is never edited and its results stay reported.

## 7. Limitations (stated, not hidden)
- **Single annotator:** every label was drafted by the implementing assistant, which also built the verifier. The owner reviews the labels (`owner_review`), but this is not independent annotation, and no inter-annotator agreement is claimed. Shared blind spots between labels and system are possible.
- **Small N:** 55 dev cases (15 / 15 / 25 after the owner's corrections) and 51 held-out cases (17 / 17 / 17). One case moves a per-class rate by about 0.06, so confidence intervals are wide and "no measurable difference" is a valid result.
- **Corpus-bound:** only 9 public documents; just one is primary (the RBI Directions). LLP, NRI and entity-group cases are thin because the corpus is.
- **Claims are synthetic:** written by the annotator, not drawn from model outputs. Distribution shift to real model drafts is likely; S8 adds scripted end-to-end drafts.
- **Hard cases are judgement calls:** stricter-number claims, list exclusivity and OCR garbling follow the rules above, and the rationale is recorded per case.
- **Rules revised after labelling:** rules 1, 3 and 10 were clarified on 2026-11-12.
  - The dev set was relabelled under the revised rules.
  - The frozen held-out set was labelled under the earlier wording. The cases the revisions would affect are listed in the M4 plan (S3 owner review).
  - Any correction goes into a new versioned held-out file before S8, never after results are seen.

## 8. Owner review (required before S4 uses the dev labels)
1. Read each case's `claim`, `quotes`, `gold` and `rationale`.
2. **Dev set:** set `owner_review` to `accepted` or `changed` (with `owner_note`) in place, before S4 runs. **`accepted` means the owner accepted the existing label after reviewing the generated report. It is not an independent re-annotation.** Every owner decision is also recorded in the dev ledger `data/eval/m4_claims_dev_review.jsonl`, one entry per decision: case changes with their previous and applied values, additions, and guideline decisions.
3. **Held-out set:** the frozen file is never edited, so its `owner_review` stays `pending` in the file. Issues found under later rule clarifications are logged in the same ledger, with status `deferred_to_S8_versioned_correction`.
   - The owner's verdicts go into a separate ledger, `data/eval/m4_claims_heldout_review.jsonl` (`{id, owner_review, owner_note}`), committed before S8.
   - If a label must change, a new versioned file is made and its hash recorded in a reviewed commit **before** the S8 run, never after seeing results.
4. **Optional:** an independent second reviewer records a sample, in `second_review` for dev or in the ledger for held-out. Disagreements are recorded, never overwritten.
