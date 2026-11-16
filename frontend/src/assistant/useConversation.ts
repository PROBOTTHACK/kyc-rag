/**
 * The conversation state for `/open-account` (A6). The A5 API is the source of truth: this
 * hook only sends what the customer chose or typed and keeps what came back. It never
 * decides what to ask next, which items apply, or how anything is classified.
 *
 * - Session id: in memory and in `sessionStorage` only (`storage/assistantSession`); never
 *   in the URL, the page or a log.
 * - Idempotency: every turn carries a key. Retrying the same turn after an unknown outcome
 *   (no response, 5xx) or a `technical_error` answer (nothing was stored) reuses its key;
 *   anything else retires it.
 * - Blocked messages are never shown back: the customer's bubble says only that it was not
 *   sent.
 */
import { useCallback, useEffect, useReducer, useRef } from "react";

import { ApiError } from "../api/errors";
import { useApi } from "../api/hooks";
import { fingerprint, newIdempotencyKey } from "../api/idempotency";
import type {
  AssistantAnswer,
  AssistantSessionView,
  AssistantTurnRequest,
  AssistantTurnView,
} from "../api/types";
import {
  forgetAssistantSession,
  loadAssistantSession,
  saveAssistantSession,
} from "../storage/assistantSession";
import { ACTION_LABELS, slotLabel, TEXT } from "./labels";

export interface CustomerEntry {
  role: "customer";
  id: number;
  /** What the customer sent: their text or the label of their choice. Null when blocked. */
  text: string | null;
  blocked: boolean;
}

export interface AssistantEntry {
  role: "assistant";
  id: number;
  answer: AssistantAnswer;
}

export type Entry = CustomerEntry | AssistantEntry;

export type Phase = "welcome" | "restoring" | "starting" | "active" | "ended";

export interface Failure {
  /** "retry": the same turn can be resent with the same key. */
  retryable: boolean;
  /** Customer-facing text: the API's fixed technical message, or an error code's text. */
  error: ApiError | null;
  technicalMessage: string | null;
}

export interface ConversationState {
  phase: Phase;
  sessionId: string | null;
  entries: Entry[];
  latest: AssistantAnswer | null;
  pending: boolean;
  failure: Failure | null;
  ended: "SESSION_EXPIRED" | "SESSION_NOT_FOUND" | null;
  startError: ApiError | null;
}

type Action =
  | { type: "restoring"; sessionId: string }
  | { type: "starting" }
  | { type: "started"; sessionId: string; entries: Entry[]; latest: AssistantAnswer | null }
  | { type: "start_failed"; error: ApiError }
  | { type: "sending" }
  | { type: "answered"; customer: CustomerEntry; answer: AssistantAnswer }
  | { type: "failed"; failure: Failure }
  | { type: "dismiss" }
  | { type: "ended"; reason: "SESSION_EXPIRED" | "SESSION_NOT_FOUND" }
  | { type: "reset" };

export const INITIAL: ConversationState = {
  phase: "welcome",
  sessionId: null,
  entries: [],
  latest: null,
  pending: false,
  failure: null,
  ended: null,
  startError: null,
};

let nextId = 1;
const id = () => nextId++;

export function reducer(state: ConversationState, action: Action): ConversationState {
  switch (action.type) {
    case "restoring":
      return { ...INITIAL, phase: "restoring", sessionId: action.sessionId };
    case "starting":
      return { ...INITIAL, phase: "starting" };
    case "started":
      return {
        ...INITIAL,
        phase: "active",
        sessionId: action.sessionId,
        entries: action.entries,
        latest: action.latest,
      };
    case "start_failed":
      return { ...INITIAL, phase: "welcome", startError: action.error };
    case "sending":
      return { ...state, pending: true, failure: null };
    case "answered":
      return {
        ...state,
        pending: false,
        failure: null,
        entries: [
          ...state.entries,
          action.customer,
          { role: "assistant", id: id(), answer: action.answer },
        ],
        latest: action.answer,
      };
    case "failed":
      return { ...state, pending: false, failure: action.failure };
    case "dismiss":
      return { ...state, failure: null };
    case "ended":
      return { ...INITIAL, phase: "ended", ended: action.reason };
    case "reset":
      return INITIAL;
  }
}

/** The text of the customer's own bubble for a turn (before the answer is known). */
export function describeTurn(body: AssistantTurnRequest, latest: AssistantAnswer | null): string {
  if (body.message) return body.message;
  const action = body.action ?? "";
  // A scenario offer's choice reads as the option the customer clicked ("Ask about a separate
  // account", "Yes, go back to it"), never its raw value (A7.6: an empty or "0" bubble).
  const offered = latest?.scenario_offer?.options.find(
    (o) => o.action === action && (o.value ?? null) === (body.value ?? null),
  );
  if (offered) return offered.label;
  if (action === "switch_scenario") {
    const other = latest?.scenarios?.find((s) => String(s.index) === body.value);
    if (other) return `${TEXT.goBackTo} ${other.label}`;
  }
  if (action in ACTION_LABELS) return ACTION_LABELS[action as keyof typeof ACTION_LABELS];
  const slot = body.slot ?? "";
  const label =
    latest?.profile[slot]?.choices?.find((c) => c.value === body.value)?.label ??
    latest?.question?.chips.find((c) => c.value === body.value)?.label ??
    body.value ??
    "";
  return action === "correct" ? `${slotLabel(slot)}: ${label}` : label;
}

/** Rebuild the transcript from a reloaded conversation (A5 `GET /sessions/{id}`). */
export function entriesFromView(view: AssistantSessionView): Entry[] {
  const entries: Entry[] = [];
  let previous: AssistantAnswer | null = null;
  for (const t of view.turns as AssistantTurnView[]) {
    if (t.turn > 0) {
      const blocked = t.answer.kind === "blocked";
      // Only read to label the customer's bubble; the stored choice was validated by the API.
      const body = (
        t.message
          ? { message: t.message }
          : {
              action: t.choice?.action ?? null,
              slot: t.choice?.slot ?? null,
              value: t.choice?.value ?? null,
            }
      ) as AssistantTurnRequest;
      const text = blocked
        ? null
        : t.message !== null || t.choice
          ? describeTurn(body, previous)
          : null;
      entries.push({ role: "customer", id: id(), text, blocked });
    }
    entries.push({ role: "assistant", id: id(), answer: t.answer });
    previous = t.answer;
  }
  return entries;
}

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, "UNKNOWN_ERROR");
}

function endedReason(error: ApiError): "SESSION_EXPIRED" | "SESSION_NOT_FOUND" | null {
  if (error.code === "SESSION_EXPIRED" || error.status === 410) return "SESSION_EXPIRED";
  if (error.code === "SESSION_NOT_FOUND" || error.status === 404) return "SESSION_NOT_FOUND";
  return null;
}

export function useConversation() {
  const api = useApi();
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);
  /** The turn that may be resent, with its key (memory only). */
  const retry = useRef<{ fp: string; key: string; body: AssistantTurnRequest } | null>(null);

  // Recover a conversation from this tab's sessionStorage (reload, return to the page).
  useEffect(() => {
    const stored = loadAssistantSession();
    if (stored === null) return;
    const controller = new AbortController();
    dispatch({ type: "restoring", sessionId: stored });
    api
      .assistantGet(stored, controller.signal)
      .then((view) => {
        const entries = entriesFromView(view);
        dispatch({ type: "started", sessionId: stored, entries, latest: view.answer ?? null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const e = asApiError(error);
        const reason = endedReason(e);
        if (reason !== null) {
          forgetAssistantSession();
          dispatch({ type: "ended", reason });
        } else {
          dispatch({ type: "start_failed", error: e }); // keep the id: "Start" offers a retry
        }
      });
    return () => controller.abort();
  }, [api]);

  const start = useCallback(async () => {
    const stored = loadAssistantSession();
    if (stored !== null) {
      // A reload failed earlier (e.g. offline): try the same conversation again.
      dispatch({ type: "restoring", sessionId: stored });
      try {
        const view = await api.assistantGet(stored);
        dispatch({
          type: "started",
          sessionId: stored,
          entries: entriesFromView(view),
          latest: view.answer ?? null,
        });
      } catch (error) {
        const e = asApiError(error);
        const reason = endedReason(e);
        if (reason !== null) {
          forgetAssistantSession();
          dispatch({ type: "ended", reason });
        } else {
          dispatch({ type: "start_failed", error: e });
        }
      }
      return;
    }
    dispatch({ type: "starting" });
    try {
      const started = await api.assistantStart();
      saveAssistantSession(started.session_id);
      dispatch({
        type: "started",
        sessionId: started.session_id,
        entries: [{ role: "assistant", id: id(), answer: started.answer }],
        latest: started.answer,
      });
    } catch (error) {
      dispatch({ type: "start_failed", error: asApiError(error) });
    }
  }, [api]);

  const send = useCallback(
    async (body: AssistantTurnRequest) => {
      const { sessionId, pending, latest } = stateRef.current;
      if (sessionId === null || pending) return;
      const fp = fingerprint("turns", sessionId, body);
      const key = retry.current?.fp === fp ? retry.current.key : newIdempotencyKey();
      retry.current = null;
      const text = describeTurn(body, latest);
      stateRef.current = { ...stateRef.current, pending: true }; // no double submit
      dispatch({ type: "sending" });
      try {
        const result = await api.assistantTurn(sessionId, body, key);
        const answer = result.answer;
        if (answer.kind === "technical_error") {
          // Nothing was stored: the same request, with the same key, simply runs again.
          retry.current = { fp, key, body };
          dispatch({
            type: "failed",
            failure: {
              retryable: true,
              error: null,
              technicalMessage: answer.technical_message ?? null,
            },
          });
          return;
        }
        const blocked = answer.kind === "blocked";
        dispatch({
          type: "answered",
          customer: { role: "customer", id: id(), text: blocked ? null : text, blocked },
          answer,
        });
      } catch (error) {
        const e = asApiError(error);
        const reason = endedReason(e);
        if (reason !== null) {
          forgetAssistantSession();
          dispatch({ type: "ended", reason });
          return;
        }
        if (e.outcomeUnknown) retry.current = { fp, key, body };
        dispatch({
          type: "failed",
          failure: { retryable: e.outcomeUnknown, error: e, technicalMessage: null },
        });
      }
    },
    [api],
  );

  const retryLast = useCallback(async () => {
    const last = retry.current;
    if (last !== null) await send(last.body);
  }, [send]);

  const dismiss = useCallback(() => {
    retry.current = null;
    dispatch({ type: "dismiss" });
  }, []);

  const restart = useCallback(() => {
    retry.current = null;
    forgetAssistantSession();
    dispatch({ type: "reset" });
  }, []);

  return { state, start, send, retryLast, dismiss, restart };
}
