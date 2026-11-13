/**
 * Typed client for the existing `/api/v1` endpoints only (docs/07). Types come from the
 * generated contract (`types.ts`); nothing here redeclares a request or response shape.
 *
 * - Every call has a timeout (CLAUDE.md rule 8).
 * - Automatic retries are bounded and only for transient failures (no response, 502/503/504)
 *   of requests that are safe to repeat: GETs and POSTs carrying an Idempotency-Key, which
 *   are retried with the SAME key. `POST /sessions` has no key and is never retried.
 * - Errors carry codes only (`ApiError`); request bodies are never copied into them.
 * - Nothing is written to browser storage (session ids are credentials; M5-D4 is open).
 */
import { ApiError } from "./errors";
import { errorFromResponse } from "./errors";
import type {
  ComplianceReport,
  DataClass,
  HealthResponse,
  MessageRequest,
  ResumeRequest,
  ReviewCasesResponse,
  RunAccepted,
  SessionCreated,
  SessionView,
} from "./types";

export const API_PREFIX = "/api/v1";
export const DEFAULT_TIMEOUT_MS = 15_000;
export const MAX_ATTEMPTS = 3; // one try plus at most two retries
export const BACKOFF_MS = [500, 1_000] as const;

export interface ClientOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
  timeoutMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

interface RequestOptions {
  body?: unknown;
  idempotencyKey?: string;
  /** Non-2xx statuses whose body is the expected payload (health: 503 + HealthResponse). */
  acceptStatuses?: readonly number[];
  signal?: AbortSignal;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function sessionPath(sessionId: string, suffix = ""): string {
  return `${API_PREFIX}/sessions/${encodeURIComponent(sessionId)}${suffix}`;
}

export function createApiClient(options: ClientOptions = {}) {
  const base = options.baseUrl ?? "";
  const doFetch = options.fetch ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const sleep = options.sleep ?? defaultSleep;

  async function attempt(method: string, path: string, opts: RequestOptions): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (opts.idempotencyKey !== undefined) headers["Idempotency-Key"] = opts.idempotencyKey;
    const timeout = AbortSignal.timeout(timeoutMs);
    const signal = opts.signal ? AbortSignal.any([opts.signal, timeout]) : timeout;
    try {
      return await doFetch(`${base}${path}`, {
        method,
        headers,
        body: opts.body === undefined ? null : JSON.stringify(opts.body),
        signal,
        credentials: "same-origin",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
    } catch (error) {
      if (opts.signal?.aborted) throw error; // the caller cancelled: not an API error
      throw new ApiError(0, timeout.aborted ? "TIMEOUT" : "NETWORK_ERROR");
    }
  }

  async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const repeatable = method === "GET" || opts.idempotencyKey !== undefined;
    for (let n = 1; ; n++) {
      let error: ApiError;
      try {
        const res = await attempt(method, path, opts);
        if (res.ok || opts.acceptStatuses?.includes(res.status)) {
          try {
            return (await res.json()) as T;
          } catch {
            throw new ApiError(res.status, "BAD_RESPONSE");
          }
        }
        error = await errorFromResponse(res);
      } catch (caught) {
        if (!(caught instanceof ApiError)) throw caught;
        error = caught;
      }
      if (!(repeatable && error.transient && n < MAX_ATTEMPTS)) throw error;
      await sleep(BACKOFF_MS[n - 1] ?? BACKOFF_MS[BACKOFF_MS.length - 1]!);
    }
  }

  return {
    /** 200 when every check is ok, 503 `degraded` otherwise; both carry HealthResponse. */
    health: (signal?: AbortSignal) =>
      request<HealthResponse>("GET", `${API_PREFIX}/health`, {
        acceptStatuses: [503],
        ...(signal ? { signal } : {}),
      }),

    createSession: (dataClass: DataClass = "synthetic_demo") =>
      request<SessionCreated>("POST", `${API_PREFIX}/sessions`, {
        body: { data_class: dataClass },
      }),

    getSession: (sessionId: string, signal?: AbortSignal) =>
      request<SessionView>("GET", sessionPath(sessionId), signal ? { signal } : {}),

    submitMessage: (sessionId: string, body: MessageRequest, idempotencyKey: string) =>
      request<RunAccepted>("POST", sessionPath(sessionId, "/messages"), { body, idempotencyKey }),

    resume: (sessionId: string, body: ResumeRequest, idempotencyKey: string) =>
      request<RunAccepted>("POST", sessionPath(sessionId, "/resume"), { body, idempotencyKey }),

    retry: (sessionId: string, idempotencyKey: string) =>
      request<RunAccepted>("POST", sessionPath(sessionId, "/retry"), { body: {}, idempotencyKey }),

    getReport: (reportId: string, signal?: AbortSignal) =>
      request<ComplianceReport>(
        "GET",
        `${API_PREFIX}/reports/${encodeURIComponent(reportId)}`,
        signal ? { signal } : {},
      ),

    getReviewCases: (reportId: string, signal?: AbortSignal) =>
      request<ReviewCasesResponse>(
        "GET",
        `${API_PREFIX}/reports/${encodeURIComponent(reportId)}/review-cases`,
        signal ? { signal } : {},
      ),

    /** The raw fetch used by the event stream (same base URL and fetch implementation). */
    fetchEvents: (sessionId: string, lastEventId: string | null, signal: AbortSignal) =>
      doFetch(`${base}${sessionPath(sessionId, "/events")}`, {
        method: "GET",
        headers: {
          Accept: "text/event-stream",
          ...(lastEventId !== null ? { "Last-Event-ID": lastEventId } : {}),
        },
        signal,
        credentials: "same-origin",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      }),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;

export const api: ApiClient = createApiClient();
