/**
 * API fixtures for the assistant tests.
 *
 * `live/*.json` and `live/events_completed.txt` were RECORDED from the real backend
 * (`make api`, dev corpus, no live LLM) with a synthetic LLP question, on 2026-11-13, by a
 * one-off script. Every server-issued UUID was replaced by a stable fake one. Neither the
 * question text nor the rejected synthetic PAN appears in them (the backend never echoes
 * input). Shapes the backend cannot be made to produce on demand (a failed run, an expired
 * session, a lease conflict) are DERIVED below from the recorded ones, using the documented
 * contract, and are labelled as derived.
 */
import { ApiError, errorFromResponse } from "../../api/errors";
import type { RunAccepted, SessionCreated, SessionView } from "../../api/types";
import errorInvalidAnswer from "./live/error_invalid_answer.json";
import errorNothingToRetry from "./live/error_nothing_to_retry.json";
import errorRunInProgress from "./live/error_run_in_progress.json";
import errorSensitive from "./live/error_sensitive_message.json";
import errorSessionNotFound from "./live/error_session_not_found.json";
import errorStaleCheckpoint from "./live/error_stale_checkpoint.json";
import messageAccepted from "./live/message_accepted.json";
import resumeAccepted from "./live/resume_accepted.json";
import sessionAwaiting from "./live/session_awaiting.json";
import sessionCompleted from "./live/session_completed.json";
import sessionCreated from "./live/session_created.json";
import sessionNoRun from "./live/session_no_run.json";

type Recorded = { status: number; body: unknown };

// The JSON is typed by inference (plain strings); the recorded payloads are the contract's
// shapes, which the backend's S1 contract tests enforce.
const as = <T>(recorded: Recorded) => structuredClone(recorded.body) as T;

/** The real client's parsing of a recorded error response. */
export function recordedError(recorded: Recorded): Promise<ApiError> {
  return errorFromResponse(
    new Response(JSON.stringify(recorded.body), {
      status: recorded.status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

export const SYNTHETIC_PAN = "ABCPE1234F";

export const live = {
  sessionCreated: () => as<SessionCreated>(sessionCreated),
  sessionNoRun: () => as<SessionView>(sessionNoRun),
  sessionAwaiting: () => as<SessionView>(sessionAwaiting),
  sessionCompleted: () => as<SessionView>(sessionCompleted),
  messageAccepted: () => as<RunAccepted>(messageAccepted),
  resumeAccepted: () => as<RunAccepted>(resumeAccepted),
  errors: {
    runInProgress: errorRunInProgress,
    staleCheckpoint: errorStaleCheckpoint,
    invalidAnswer: errorInvalidAnswer,
    sensitive: errorSensitive,
    nothingToRetry: errorNothingToRetry,
    sessionNotFound: errorSessionNotFound,
  } satisfies Record<string, Recorded>,
};

// --- derived shapes (documented contract, not recorded) ------------------------------------------

/** A run that failed after three steps (service.py `_execute`: an `error:<Class>` event). */
export function derivedSessionFailed(): SessionView {
  const view = live.sessionCompleted();
  const wf = view.workflow!;
  const kept = wf.node_events.slice(0, 3);
  return {
    ...view,
    workflow: {
      run_id: wf.run_id,
      thread_id: wf.thread_id,
      status: "failed",
      started_at: wf.started_at,
      finished_at: wf.finished_at,
      node_events: [
        ...kept,
        { seq: kept.length + 1, node: "error:RuntimeError", status: "failed", at: wf.started_at },
      ],
    },
  };
}

export function derivedSessionExpired(): SessionView {
  return { ...live.sessionNoRun(), status: "expired" };
}

/** The same pause after the graph re-ran: a new checkpoint id (STALE_CHECKPOINT case). */
export function derivedAwaitingNewCheckpoint(): SessionView {
  const view = live.sessionAwaiting();
  view.workflow!.clarification!.checkpoint_id = "00000000-0000-4000-8000-0000000000aa";
  return view;
}

export function derivedAwaitingUnreadable(): SessionView {
  const view = live.sessionAwaiting();
  view.workflow!.clarification = null;
  return view;
}

export const derivedErrors = {
  /** service.py `_active_session`: 410 SESSION_EXPIRED. */
  sessionExpired: {
    status: 410,
    body: { error: { code: "SESSION_EXPIRED", message: "The session has expired." } },
  },
  /** service.py `retry`: the lease is still valid (retry_after = lease seconds). */
  retryLeaseValid: {
    status: 409,
    body: {
      error: {
        code: "RUN_IN_PROGRESS",
        message: "The workflow is still running; retry after its lease expires.",
        retry_after: 60,
      },
    },
  },
} satisfies Record<string, Recorded>;
