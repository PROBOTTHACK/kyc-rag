/**
 * Fixtures for the account opening assistant (A6).
 *
 * `assistant/*.json` were RECORDED from the real A5 API (`/api/v2/assistant`, development
 * corpus, no live LLM) on 2026-11-14 by a one-off script, with synthetic inputs only. The
 * server-issued session id was replaced by one stable fake id; the synthetic PAN sent in
 * the blocked turn appears nowhere (the API never echoes or stores it).
 *
 * Two shapes cannot be produced on demand and are DERIVED below from recorded ones using the
 * documented contract: a `technical_error` answer (stored evidence unreadable) and an
 * expired session (410).
 */
import type {
  AssistantAnswer,
  AssistantSessionStarted,
  AssistantSessionView,
  AssistantTurnResult,
} from "../../api/types";
import afterChecklistBleeh from "./assistant/after_checklist_bleeh.json";
import blocked from "./assistant/blocked.json";
import cannotBuild from "./assistant/cannot_build.json";
import errorIdempotencyMismatch from "./assistant/error_idempotency_mismatch.json";
import errorNotFound from "./assistant/error_not_found.json";
import errorValidation from "./assistant/error_validation.json";
import explainAccountType from "./assistant/explain_account_type.json";
import llpChecklist from "./assistant/llp_checklist.json";
import llpChecklistPanCopy from "./assistant/llp_checklist_pan_copy.json";
import llpCorrected from "./assistant/llp_corrected.json";
import llpSavingsFallback from "./assistant/llp_savings_fallback.json";
import llpSavingsTakeSuggestion from "./assistant/llp_savings_take_suggestion.json";
import nriChecklist from "./assistant/nri_checklist.json";
import nriChecklistPhoto from "./assistant/nri_checklist_photo.json";
import nriConfirm from "./assistant/nri_confirm.json";
import nriQuestionHolding from "./assistant/nri_question_holding.json";
import nriQuestionMinor from "./assistant/nri_question_minor.json";
import nriQuestionScheme from "./assistant/nri_question_scheme.json";
import nriSessionView from "./assistant/nri_session_view.json";
import outOfScope from "./assistant/out_of_scope.json";
import outOfScopeWithConflict from "./assistant/out_of_scope_with_conflict.json";
import residentFallback from "./assistant/resident_fallback.json";
import started from "./assistant/started.json";

type Recorded = { status: number; body: unknown };

const as = <T>(recorded: Recorded) => structuredClone(recorded.body) as T;
const turn = (recorded: Recorded) => as<AssistantTurnResult>(recorded);

export const FAKE_SESSION_ID = "0a5e5510-0000-4000-8000-000000000001";
export const SYNTHETIC_PAN = "ABCPE1234F";
/** The fixed technical message (A1 §7), as the API sends it. */
export const TECHNICAL_MESSAGE =
  "I couldn't check the HDFC Bank documents just now. Please try again in a moment.";

export const recorded = {
  started: () => as<AssistantSessionStarted>(started),
  explainAccountType: () => turn(explainAccountType),
  nriConfirm: () => turn(nriConfirm),
  nriQuestionScheme: () => turn(nriQuestionScheme),
  nriQuestionHolding: () => turn(nriQuestionHolding),
  nriQuestionMinor: () => turn(nriQuestionMinor),
  nriChecklist: () => turn(nriChecklist),
  nriChecklistPhoto: () => turn(nriChecklistPhoto),
  afterChecklistBleeh: () => turn(afterChecklistBleeh),
  nriSessionView: () => as<AssistantSessionView>(nriSessionView),
  llpChecklist: () => turn(llpChecklist),
  llpChecklistPanCopy: () => turn(llpChecklistPanCopy),
  llpCorrected: () => turn(llpCorrected),
  llpSavingsFallback: () => turn(llpSavingsFallback),
  llpSavingsTakeSuggestion: () => turn(llpSavingsTakeSuggestion),
  residentFallback: () => turn(residentFallback),
  cannotBuild: () => turn(cannotBuild),
  blocked: () => turn(blocked),
  outOfScope: () => turn(outOfScope),
  outOfScopeWithConflict: () => turn(outOfScopeWithConflict),
  errors: {
    notFound: errorNotFound,
    idempotencyMismatch: errorIdempotencyMismatch,
    validation: errorValidation,
  } satisfies Record<string, Recorded>,
};

/** DERIVED: the answer when stored evidence could not be read (nothing is stored). */
export function technicalErrorTurn(): AssistantTurnResult {
  const base = recorded.nriQuestionMinor();
  const answer: AssistantAnswer = {
    kind: "technical_error",
    question: null,
    confirmation: null,
    messages: [],
    profile: base.answer.profile,
    coverage_state: null,
    checklist: null,
    fallback: null,
    technical_message: TECHNICAL_MESSAGE,
    notice: base.answer.notice,
  };
  return { ...base, answer };
}

/** DERIVED: the 410 envelope of an expired conversation (A5 `SESSION_EXPIRED`). */
export const expiredError: Recorded = {
  status: 410,
  body: {
    error: {
      code: "SESSION_EXPIRED",
      message: "The conversation has expired. Please start a new one.",
    },
  },
};
