/**
 * Display text for backend codes. Labels describe; they never approve, reject or certify
 * (M5_PLAN §E rule 1). Unknown codes fall back to a neutral label, never to a guess.
 */
import type { ComplianceReport, Question, RunStatus } from "../../api/types";

const NODE_LABELS: Record<string, string> = {
  input_security: "Input screening",
  entity_intake: "Customer profile intake",
  clarify: "Clarification questions",
  policy_retrieval: "Policy evidence retrieval",
  form_analysis: "Form analysis",
  evidence_association: "Evidence association",
  compliance_synthesis: "Checklist drafting",
  output_validation: "Citation and applicability validation",
  claim_verification: "Claim verification",
  finalize: "Report",
};

export function nodeLabel(node: string): string {
  if (node.startsWith("error:")) return "Stopped by an error"; // the exception class is not shown
  return NODE_LABELS[node] ?? "Workflow step";
}

export const NODE_STATUS_LABELS: Record<string, string> = {
  completed: "done",
  failed: "failed",
  recovered: "done (recovered after a restart)",
};

export const RUN_STATUS_TEXT: Record<RunStatus, string> = {
  running: "Working on your question…",
  awaiting_clarification: "Waiting for your answers",
  completed: "Finished",
  failed: "Stopped by an error before it finished",
  abandoned: "Abandoned",
};

type Outcome = ComplianceReport["outcome"];

export const OUTCOME_TEXT: Record<Outcome, { label: string; detail: string }> = {
  BLOCKED: {
    label: "Blocked by input screening",
    detail: "The question was not processed.",
  },
  INSUFFICIENT_INFORMATION: {
    label: "Insufficient information",
    detail: "The customer profile could not be completed from the question and the answers.",
  },
  INSUFFICIENT_EVIDENCE: {
    label: "Insufficient evidence",
    detail:
      "The knowledge base did not support an answer. This does not mean that nothing is required.",
  },
  DEGRADED: {
    label: "Degraded result",
    detail: "Part of the workflow could not run normally. The report explains what was affected.",
  },
  CHECKLIST_REVIEW_REQUIRED: {
    label: "Checklist for human review",
    detail: "A draft checklist with cited sources was produced.",
  },
};

export const QUESTION_REASON_TEXT: Record<Question["reason"], string | null> = {
  missing: null,
  ambiguous: "Your question was not clear on this point.",
  rejected: "The previous answer to this question was not accepted.",
};

/** An enum value as shown: underscores become spaces; the value itself is never changed. */
export function optionLabel(value: string): string {
  return value.replaceAll("_", " ");
}
