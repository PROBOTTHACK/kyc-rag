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

## Verification pipeline (M4; full design in `docs/milestones/M4_PLAN.md`, which supersedes the earlier sketch)
1. **Typed claim extraction** — deterministic. The asserted proposition comes from the proposed applicability (MANDATORY = unconditional `policy_obligation`, CONDITIONAL = conditional one); explanation sentences are checked verbatim. Form chunks are only ever tested as `form_request`, never as an obligation.
2. **Premises** — the cited chunks' stored text from Postgres (not the truncated state copy), split into sentence windows with character offsets.
3. **Statement analysis** — deterministic modality, polarity/negation, subject, item match and entity scope. A necessary condition only: no keyword ever establishes MANDATORY.
4. **NLI** — local DeBERTa-v3 MNLI cross-encoder, (premise = evidence window, hypothesis = claim), label map read from the model config, raw logits softmaxed by the code and stored as uncalibrated signals.
5. **Decision table** — supported / insufficient / contradicted / conflicting / not_verified, with dev-selected thresholds; no combined confidence score.
6. **Effects** — downgrade only. Unsupported items leave the checklist for `unverified_items`; contradictions and source conflicts become open review cases. No regeneration loop in M4.

Example failure to catch: evidence "designated partners must be verified" vs claim "every employee must submit ID" -> neutral/unsupported -> removed.

**Know the limit:** NLI is an entailment classifier. It can't tell if a source is authoritative, current, or applicable. Hence the combined checks + human review. Thresholds are configurable and tuned on the benchmark, not hardcoded by guess.

**Implementation status:**
- Steps 1–3 and 5–6 are implemented deterministically in `backend/app/verification/` (M4 S2).
- The local NLI scorer (`CrossEncoderNLI`) is implemented in S4.1: pinned revision, label map read from the model config, sanity triplet at load, no truncation.
- The checkpoint and thresholds were to be chosen on the dev set by the pre-registered rule in S4.2. **Result (`m4-s4-v1`): no configuration qualified (0/140). NLI is disabled by default**, so claims are `not_verified` (docs/10, S4.2 result).
- **S5:** the `claim_verification` graph node runs after `output_validation` for a non-empty checklist, with NLI disabled. Every claim is `not_verified` (`nli_not_configured`): nothing is scored and no rule-based contradiction is applied. Applicability is capped at CONDITIONAL (never upgraded; MANDATORY needs a supported claim), each item is flagged `not_verified`, and the run is DEGRADED (`verification` degradation). The report is v2: M3 citations, gaps and form links are unchanged, and the M3 applicability is kept as `applicability_before`.
- **S6:** the bounded source-conflict scan (`app/verification/conflicts.py`) and the supersession rule (D10). It runs only with a scorer, so **with NLI disabled it is `not_run`**: nothing is scored, there are no conflict records or flags, and the report says no automated conflict check was performed (never "no conflict found"). With a scorer, it compares at most `MAX_CONFLICT_PAIRS` unique passage pairs per run, each scored in both directions. A pair is two windows from different documents that both name the item, at least one cited, taken from evidence already retrieved. A conflict adds `conflicting_sources` and keeps both sides visible; applicability never changes. A conflict is resolved only by a recorded, active, confirmed supersession. A failed or incomplete lookup leaves it unresolved with a reason code, and dates are never used.

## Human review workflow
M4 creates **open** review cases (insert-only `review_cases`) for contradicted claims, conflicting sources, unsupported claims, mandatory claims not established, explanations that overstate their source, and items that could not be verified. Conflicting sources are never resolved by publication date: supersession needs a manifest `supersedes` record, an active source and `version_review_status=confirmed`.
Reviewer decisions (approve / reject / mark unresolved, with a reason) need authentication and arrive in a later milestone (M4 decision D5); until then cases are read-only and reports stay immutable. AI assists; human decides.
