/**
 * Display text for report fields (M5 S5). Rules (M5_PLAN §E):
 * - nothing approves, rejects or certifies a KYC case;
 * - "supported" wording appears only for a verdict that says so, always marked as an
 *   automated check and never as a legal determination; `not_verified` and
 *   `not_claim_verified` never read as verified;
 * - missing evidence never reads as "not required".
 * Tables are typed by the generated enums, so a new backend value fails the type-check
 * until it is labelled. Unknown free-form codes are shown raw, never guessed.
 */
import type { components } from "../../api/schema";

type S = components["schemas"];
type Item = S["ValidatedRequirement"];

export const APPLICABILITY_ORDER: readonly S["Applicability"][] = [
  "MANDATORY",
  "CONDITIONAL",
  "OPTIONAL",
  "UNCLEAR",
];

export const APPLICABILITY: Record<S["Applicability"], { label: string; detail: string }> = {
  MANDATORY: {
    label: "Mandatory",
    detail:
      "Shown as mandatory by this report: a cited primary source states it for this customer type.",
  },
  CONDITIONAL: {
    label: "Conditional",
    detail:
      "Supported by the cited sources but not established as mandatory; it may depend on circumstances.",
  },
  OPTIONAL: { label: "Optional", detail: "The cited sources present this as optional." },
  UNCLEAR: {
    label: "Unclear",
    detail:
      "The sources do not settle whether this applies. This does not mean it is not required.",
  },
};

export const VERDICT: Record<
  NonNullable<Item["verification_status"]>,
  { label: string; detail: string }
> = {
  not_claim_verified: {
    label: "Not claim-verified",
    detail: "Claims in this item were not checked against the cited text.",
  },
  not_verified: {
    label: "Not verified",
    detail:
      "No automated check of this item's claims was completed. This is not a finding that the item is wrong or unsupported.",
  },
  supported: {
    label: "Supported by the cited text (automated check)",
    detail:
      "An automated model found that the cited text supports the item. This is not a legal determination.",
  },
  supported_weaker_than_asserted: {
    label: "Only weaker support found (automated check)",
    detail:
      "The sources support a weaker statement than the item makes (for example, a form asks for it); no policy obligation was confirmed.",
  },
  partially_supported: {
    label: "Partly supported (automated check)",
    detail: "Some of the item's claims were supported by the cited text and some were not.",
  },
  unsupported: {
    label: "Not supported by the cited text (automated check)",
    detail: "The cited text did not support the item. This does not mean it is not required.",
  },
  contradicted: {
    label: "Contradicted by the cited text (automated check)",
    detail: "The cited text appears to say otherwise. Needs human review.",
  },
  conflicting: {
    label: "Conflicting evidence (automated check)",
    detail: "Some cited text supports the item and some contradicts it. Needs human review.",
  },
};

export const EXPLANATION_STATUS: Record<NonNullable<Item["explanation_status"]>, string> = {
  model_drafted_not_verified: "Explanation drafted by a language model; not verified.",
  verified_supported:
    "Each explanation sentence was supported by the cited text (automated check, not a legal determination).",
  partially_verified:
    "Some explanation sentences were supported by the cited text (automated check); others were not.",
  unverified: "The explanation sentences were not supported by the cited text (automated check).",
};

export const ELIGIBILITY: Record<S["EvidenceEligibility"], string> = {
  primary: "primary source",
  supporting: "supporting source",
  context_only: "context only",
};

export const RELIABILITY: Record<S["TextReliability"], string> = {
  native: "digital text",
  ocr_reliable: "scanned text (OCR, reliable)",
  ocr_uncertain: "scanned text (OCR, uncertain)",
};

export const SOURCE_STATUS: Record<S["SourceStatus"], string> = {
  active: "current",
  dated: "dated",
  superseded: "superseded",
  unknown: "status unknown",
};

export const LINK_KIND: Record<S["LinkKind"], string> = {
  same_chunk: "same passage",
  same_section: "same section",
  topic_match: "topic match",
};

export const REVIEW_REASON: Record<S["ReviewCase"]["reason"], string> = {
  not_verified: "Claims not verified",
  unsupported: "Not supported by the cited text",
  contradicted: "Contradicted by the cited text",
  conflicting_sources: "Sources may conflict",
  mandatory_not_established: "Mandatory status not established",
  explanation_overstates_source: "Explanation goes beyond its source",
};

export const EVIDENCE_STATUS: Record<
  NonNullable<S["ComplianceReport"]["evidence_status"]>,
  string
> = {
  found: "Evidence found in the knowledge base.",
  partial:
    "Evidence found only in part: some points could not be tied to primary sources for this customer type.",
  none: "No eligible evidence was found. This does not mean that nothing is required.",
  retrieval_failed:
    "The knowledge-base search failed (a technical problem), so no conclusion was drawn.",
};

export const VERIFICATION: Record<S["ComplianceReport"]["verification"] & string, string> = {
  not_claim_verified: "Claims were not checked against the cited text.",
  claim_check_ran:
    "Claims were checked against the cited text by an automated model. This is not a legal determination.",
  claim_check_partial:
    "Automated checking did not complete for every item; claims that were not checked are marked not verified.",
  claim_check_failed: "Automated checking failed; every claim is marked not verified.",
};

/** Exactly the backend's wording (app/graph/report.py); a test compares them. */
export const CONFLICT_SCAN_OFF = "No automated check for conflicting sources was performed.";
export const CONFLICT_SCAN_FAILED =
  "The automated check for conflicting sources did not complete; no conclusion is drawn about conflicts.";
export function conflictScanBounded(pairs: number, skipped: number): string {
  return (
    `An automated check compared ${pairs} pair(s) of retrieved passages from different sources ` +
    `(${skipped} not compared). Not finding a conflict does not prove that none exists.`
  );
}

export const RESOLUTION_REASON: Record<S["SourceConflict"]["resolution_reason"], string> = {
  no_supersession_relation: "no recorded relation says one source replaces the other",
  superseding_not_active: "the replacing source is not marked current",
  version_review_not_confirmed: "the version review is not confirmed",
  relation_lookup_failed: "the source relation could not be read",
  relation_incomplete: "the recorded relation is incomplete",
  supersession_confirmed: "a confirmed, current source replaces the other",
};

export const DEGRADATION_COMPONENT: Record<S["Degradation"]["component"], string> = {
  retrieval: "Evidence retrieval",
  rerank: "Re-ranking",
  forms: "Form analysis",
  llm: "Language model",
  rate_limiter: "Rate limiter",
  events: "Progress events",
  verification: "Claim verification",
};

export const FORM_METHOD: Record<S["FormSummary"]["method"], string> = {
  m1_chunks: "deterministic extraction from stored text",
  gemini_multimodal: "multimodal model extraction",
};

const FORM_FLAGS: Record<string, string> = {
  fields_capped: "only some fields were analysed",
  scanned_document: "scanned document",
  multimodal_model_output: "extracted by a multimodal model",
  uncorroborated_fields: "some fields not corroborated by the stored text",
  repeated_labels: "repeated field labels",
  ambiguous_required_markers: "unclear required-field markers",
};

export function formFlag(flag: string): string {
  return FORM_FLAGS[flag] ?? flag;
}

export const PROFILE_FIELD: Record<string, string> = {
  entity_type: "Customer type",
  residency: "Residency",
  account_type: "Account type",
};

export const PROFILE_SOURCE: Record<S["ProfileSummary"]["source"], string> = {
  user_stated: "stated in the question",
  user_answered: "answered in clarification",
  unknown: "not provided",
};

const CODE_LABELS: Record<string, (rest: string) => string> = {
  no_entity_specific_primary_evidence: () =>
    "No primary source specific to this customer type was found.",
  primary_evidence_only_via_entity_group: () =>
    "Primary sources were found only for a broader customer group.",
  no_eligible_evidence: () =>
    "No eligible evidence was found. This does not mean that nothing is required.",
  retrieval_failed: () => "The knowledge-base search failed (technical problem).",
  no_supported_requirements: () => "No drafted item survived validation.",
  no_stored_schema: (form) => `No stored structure for form ${form}.`,
  schema_load_failed: (form) => `The stored structure of form ${form} could not be loaded.`,
  dropped_uncited_requirements: (n) =>
    `${n} drafted item(s) were dropped because they cited no valid source.`,
  mandatory_downgraded: () =>
    "An item proposed as mandatory was lowered: no qualifying primary source.",
  claims_not_verified: (n) => `${n} item(s) whose claims were not verified.`,
  applicability_lowered_by_verification: (id) =>
    `Applicability of ${id} was lowered by claim checking.`,
  conflicting_sources: (id) => `Possible conflict between sources for ${id}.`,
};

/** A readable text for a code like `prefix:detail`; unknown codes are returned as they are. */
export function codeText(code: string): string {
  const i = code.indexOf(":");
  const prefix = i === -1 ? code : code.slice(0, i);
  const rest = i === -1 ? "" : code.slice(i + 1);
  const label = CODE_LABELS[prefix];
  return label ? label(rest) : code;
}

export function pages(page: number, pageEnd: number | null | undefined): string {
  return pageEnd != null && pageEnd !== page ? `pp. ${page}–${pageEnd}` : `p. ${page}`;
}
