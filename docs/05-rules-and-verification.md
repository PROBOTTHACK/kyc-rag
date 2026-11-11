# 05 — Rules Layer, Source Hierarchy & Claim Verification

## Deterministic rules layer
Don't rely only on vector retrieval for applicability. Maintain small structured rules (Postgres `rules` table, seeded from JSON):
```json
{"rule_id":"RULE_001","entity_type":"LLP","topic":"entity_verification",
 "source_document":"doc_001","source_page":12,"conditions":[],
 "applicability":"CONDITIONAL","verification_status":"human_verified"}
```
Distinguish four states: (a) field exists on form, (b) field applies to a specific entity, (c) field is mandatory under a verified policy, (d) applicability can't be established. Only (c) may yield MANDATORY. Form instructions are (a)/(b) evidence of what the form requests, never (c) on their own (docs/15, "Form requirements are not obligations").

## Source hierarchy
1. Applicable official regulatory directions & amendments
2. Official bank policies & procedures
3. Official bank forms & declarations
4. Supporting reference material
Newer ≠ automatically overriding; consider issuing authority, scope, amendment relationships. **Conflicts -> flag + human review.**

## Evidence, gaps and associations (M3 S4)
- **Evidence status:** retrieval reports `found` / `partial` / `none` / `retrieval_failed` (`retrieval_meta.evidence_status`).
  - `found` means eligible, entity-scope, primary-capable evidence was **retrieved**, not that it is relevant or correct. Retrieval scores are uncalibrated, and the synthesis and verification stages judge support.
- **Absence is not a negative finding.** A requirement with no retrieved evidence is unknown, never "not required" or "not applicable".
- **Entity-group evidence** (found only through a taxonomy group, docs/15) can never on its own establish MANDATORY, and neither can form, supporting or OCR-uncertain evidence.
- **Form/evidence links are relationships, not obligations.** The links (`same_chunk`, `same_section`, `topic_match`) only say that a passage is related to a field. They are not citations, never establish an obligation, and never set `mandatory_under_policy`.

## Output validation (implemented, M3 S5; `app/graph/validation.py`)
- **The draft is a proposal.** The synthesis model drafts a checklist; the deterministic validator decides what survives it.
- **Citations:** a cited id counts only if it was supplied to the model AND re-resolves to a stored chunk. Everything shown for a citation is copied from Postgres.
- **Uncited requirements are dropped,** not kept as UNCLEAR (decision S5-D1). Only a count is kept.
- **MANDATORY** needs a citation that is all of: primary, `can_support_mandatory`, from an active source, and `match_scope="entity"`.
  - Otherwise the item is downgraded to CONDITIONAL, with a finding: `only_entity_group`, `only_ocr_uncertain`, `not_active` or `only_form_or_supporting`.
  - An applicability is never upgraded.
  - Association links never count as citations.
- **If nothing survives:** the outcome is `INSUFFICIENT_EVIDENCE` with gap `no_supported_requirements`, and the user sees the fallback text (decision S5-D2).
- **Claim-level verification** of the explanations, by NLI, is M4. Every item is labelled `not_claim_verified`, and its explanation `model_drafted_not_verified`.

## Verification pipeline
1. **Claim extraction** — split draft into atomic claims `{claim_id, claim}`.
2. **Evidence matching** — best supporting passages per claim (reuse retrieval).
3. **NLI** — local DeBERTa-v3 MNLI: entailment / contradiction / neutral for (evidence, claim).
4. **Confidence** — combine NLI probs + retrieval relevance + evidence coverage + source applicability + rule check. Never use NLI probability alone as truth.
5. **Routing** — supported proceed; unsupported -> regenerate or remove (max 2); contradicted -> review; unresolved -> Unverified.
6. **Final report** — only claims passing configured thresholds; uncertainty and sources preserved.

Example failure to catch: evidence "designated partners must be verified" vs claim "every employee must submit ID" -> neutral/unsupported -> removed.

**Know the limit:** NLI is an entailment classifier. It can't tell if a source is authoritative, current, or applicable. Hence the combined checks + human review. Thresholds are configurable and tuned on the benchmark, not hardcoded by guess.

## Human review workflow
Review case created for: conflicting sources, missing amendments, unclear applicability, contradictions, low-confidence form extraction, repeatedly unsupported claims.
Reviewer can: view claim, open source doc, inspect page, see evaluator class, **approve / reject / mark unresolved**, record a reason (stored in `review_cases`). AI assists; human decides.
