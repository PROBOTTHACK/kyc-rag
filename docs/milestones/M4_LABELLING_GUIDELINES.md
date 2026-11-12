# M4 Labelling Guidelines: claim verification datasets

> Independent educational prototype. Labels describe what a stored passage **says**, not what the law requires. Every report requires human review.

**Status:** committed in M4 S3, before any NLI model has been run on these cases. **Label version:** 1.
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
1. **No outside knowledge.** Use only the quoted passage(s). Do not use general banking knowledge, other paragraphs, other documents or the taxonomy. Example: whether an LLP is a "legal person" is not stated in paragraph 38, so a claim about LLPs is `insufficient` against it (this is an `entity_group` case).
2. **Polarity and modality matter.** "shall" ≠ "may"; "shall not" ≠ "need not" ≠ "may".
   - **`insufficient`:** a claim of "must" where the text only says the act "may" be done (the text neither requires nor excuses it; e.g. "FPIs may submit an undertaking").
   - **`contradicted`:**
     - a claim that turns a prohibition into a permission;
     - a claim of "must" where the text expressly allows *not* doing it ("may, at their option, not issue UCIC to all walk-in customers"; "shall not require CDD of all members").
3. **Numbers.** When the text states a number for a quantity and the claim states a different number for **the same quantity** as the rule, the claim misstates the rule and is `contradicted`. That holds whether the claim's number is looser (6 months where the text says 3) or stricter (every 2 years where the text says at least every 8). A number for a quantity the text does not state (a completion window, a review cycle) is `insufficient`.
4. **Lists.** "The bank shall obtain each of the following: (1)…(7)" entails each listed item for that entity. A document not in the list is `insufficient`, not `contradicted`: the list does not say nothing else may be asked.
5. **Conditions.** A claim that drops a condition stated in the text (e.g. "if PAN is not provided", "where GST details are available") is `insufficient`.
6. **Entity and subject.** A claim about a different entity type (company vs trust) or a different person (employee vs designated partner) is `insufficient`, unless the text itself states the relationship (e.g. paragraph 36 states that unregistered trusts are included).
7. **Dated and non-primary sources.** Label the semantics exactly as for active sources. Their status is recorded as metadata and in the `source_status` perturbation tag. A dated source is never relabelled because a newer source says something else, and conflicts between sources are never resolved by date.
8. **OCR text.** Label what the stored OCR text visibly says. If garbling makes the meaning unclear, the label is `insufficient`. OCR-uncertain evidence never makes `expected_obligation` true.
9. **Forms.** A form passage can entail only claims about what the form shows or asks for (`form_request`). An obligation claim cited only to a form is never `entailed` as an obligation.
10. **`expected_obligation`** is true only when **all** of these hold: the gold label is `entailed`; the claim type is `policy_obligation`; every cited chunk is `primary`, active and not OCR-uncertain; and `match_scope` is `entity`. It records whether this evidence may support a MANDATORY item. It does not mean the item is legally mandatory for any real customer.

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
- **contradicted, across sources:** "A telephone bill used as proof of address must not be more than two months old." ← HDFC FAQ (dated): "Telephone bill (not older than 3 months)". The RBI FAQ says two months for utility bills (a separate dev case, `entailed`). Both stay visible; neither is resolved by date.
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
- **Small N:** 51 cases per split, 17 per class. One case moves a per-class rate by about 0.06, so confidence intervals are wide and "no measurable difference" is a valid result.
- **Corpus-bound:** only 9 public documents; just one is primary (the RBI Directions). LLP, NRI and entity-group cases are thin because the corpus is.
- **Claims are synthetic:** written by the annotator, not drawn from model outputs. Distribution shift to real model drafts is likely; S8 adds scripted end-to-end drafts.
- **Hard cases are judgement calls:** stricter-number claims, list exclusivity and OCR garbling follow the rules above, and the rationale is recorded per case.

## 8. Owner review (required before S4 uses the dev labels)
1. Read each case's `claim`, `quotes`, `gold` and `rationale`.
2. **Dev set:** set `owner_review` to `accepted` or `changed` (with `owner_note`) in place, before S4 runs.
3. **Held-out set:** the frozen file is never edited, so its `owner_review` stays `pending` in the file.
   - The owner's verdicts go into a separate ledger, `data/eval/m4_claims_heldout_review.jsonl` (`{id, owner_review, owner_note}`), committed before S8.
   - If a label must change, a new versioned file is made and its hash recorded in a reviewed commit **before** the S8 run, never after seeing results.
4. **Optional:** an independent second reviewer records a sample, in `second_review` for dev or in the ledger for held-out. Disagreements are recorded, never overwritten.
