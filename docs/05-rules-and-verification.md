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
