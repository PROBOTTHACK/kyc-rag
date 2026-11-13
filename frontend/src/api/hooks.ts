/**
 * React Query bindings for the API (server state lives in the query cache, not in
 * component state or browser storage).
 *
 * - Reports and review cases are immutable once stored (insert-only), so they never go stale.
 * - Session views are refetched when the event stream reports a run-status change
 *   (`useSessionEvents` invalidates them), not polled.
 * - Mutations do not retry on their own: the client retries transient failures with the same
 *   Idempotency-Key, and a user's "try again" of the same action reuses the key through
 *   `IdempotencyTracker`.
 */
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import { api as defaultApi, type ApiClient } from "./client";
import { SessionEventStream, type StreamSnapshot } from "./events";
import { fingerprint, IdempotencyTracker } from "./idempotency";
import type { DataClass, MessageRequest, ResumeRequest } from "./types";

export const ApiContext = createContext<ApiClient>(defaultApi);

export function useApi(): ApiClient {
  return useContext(ApiContext);
}

export const queryKeys = {
  health: ["health"] as const,
  session: (sessionId: string) => ["session", sessionId] as const,
  report: (reportId: string) => ["report", reportId] as const,
  reviewCases: (reportId: string) => ["report", reportId, "review-cases"] as const,
};

// --- reads -----------------------------------------------------------------------------------

export function useHealth() {
  const client = useApi();
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => client.health(signal),
    retry: false,
  });
}

export function useSession(sessionId: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: queryKeys.session(sessionId ?? ""),
    queryFn: ({ signal }) => client.getSession(sessionId as string, signal),
    enabled: sessionId !== null,
    retry: false,
  });
}

export function useReport(reportId: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: queryKeys.report(reportId ?? ""),
    queryFn: ({ signal }) => client.getReport(reportId as string, signal),
    enabled: reportId !== null,
    staleTime: Infinity,
    retry: false,
  });
}

export function useReviewCases(reportId: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: queryKeys.reviewCases(reportId ?? ""),
    queryFn: ({ signal }) => client.getReviewCases(reportId as string, signal),
    enabled: reportId !== null,
    staleTime: Infinity,
    retry: false,
  });
}

// --- state-changing actions --------------------------------------------------------------------

function invalidateSession(queryClient: QueryClient, sessionId: string) {
  return queryClient.invalidateQueries({ queryKey: queryKeys.session(sessionId) });
}

/** `POST /sessions`. It has no Idempotency-Key in the API, so it is never retried. */
export function useCreateSession() {
  const client = useApi();
  return useMutation({
    mutationFn: (dataClass: DataClass = "synthetic_demo") => client.createSession(dataClass),
    retry: 0,
  });
}

/** One tracker per mounted hook, stable for its lifetime (lazy state, never re-created). */
function useTracker(): IdempotencyTracker {
  const [tracker] = useState(() => new IdempotencyTracker());
  return tracker;
}

export function useSubmitMessage(sessionId: string) {
  const client = useApi();
  const queryClient = useQueryClient();
  const tracker = useTracker();
  return useMutation({
    mutationFn: (body: MessageRequest) =>
      tracker.run(fingerprint("messages", sessionId, body), (key) =>
        client.submitMessage(sessionId, body, key),
      ),
    onSettled: () => invalidateSession(queryClient, sessionId),
    retry: 0,
  });
}

export function useResume(sessionId: string) {
  const client = useApi();
  const queryClient = useQueryClient();
  const tracker = useTracker();
  return useMutation({
    mutationFn: (body: ResumeRequest) =>
      tracker.run(fingerprint("resume", sessionId, body), (key) =>
        client.resume(sessionId, body, key),
      ),
    onSettled: () => invalidateSession(queryClient, sessionId),
    retry: 0,
  });
}

export function useRetryRun(sessionId: string) {
  const client = useApi();
  const queryClient = useQueryClient();
  const tracker = useTracker();
  return useMutation({
    mutationFn: () =>
      tracker.run(fingerprint("retry", sessionId, {}), (key) => client.retry(sessionId, key)),
    onSettled: () => invalidateSession(queryClient, sessionId),
    retry: 0,
  });
}

// --- progress events ---------------------------------------------------------------------------

const IDLE: StreamSnapshot = {
  status: "idle",
  runs: [],
  lastEventId: null,
  error: null,
  failures: 0,
};
const noopSubscribe = () => () => undefined;

/**
 * Follow a session's progress events while mounted. Each run-status change refetches the
 * session view (clarification questions, report id). Call `follow()` again after starting a
 * new run (message, resume, retry) once the stream has closed on a terminal run.
 */
export function useSessionEvents(sessionId: string | null) {
  const client = useApi();
  const queryClient = useQueryClient();
  const stream = useMemo(
    () =>
      sessionId === null
        ? null
        : new SessionEventStream(sessionId, {
            fetchEvents: client.fetchEvents,
            onRunStatus: () => void invalidateSession(queryClient, sessionId),
          }),
    [client, queryClient, sessionId],
  );
  useEffect(() => {
    stream?.follow();
    return () => stream?.stop();
  }, [stream]);
  const snapshot = useSyncExternalStore(
    stream?.subscribe ?? noopSubscribe,
    stream?.getSnapshot ?? (() => IDLE),
  );
  return { ...snapshot, follow: stream?.follow ?? (() => undefined) };
}
