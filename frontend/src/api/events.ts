/**
 * Session progress over SSE (`GET /sessions/{id}/events`, docs/07 and
 * backend/app/workflow/stream.py), read with `fetch` so the client controls reconnects:
 *
 * - `node` events are durable and replayable; `run` events carry a run's CURRENT status only
 *   (S7-D3), so the latest one wins.
 * - Ids: `<run_id>.<seq>` (node) and `<run_id>.<seq>.<status>` (run). The last id is sent back
 *   as `Last-Event-ID` on every reconnect; ids already applied are ignored (no duplicates).
 * - The server closes the stream when the latest run is terminal (completed, failed,
 *   abandoned) and everything was sent. The client then stops: it does NOT reconnect, unlike
 *   a browser EventSource. `follow()` reopens it, e.g. after a new message or a retry.
 * - Any other end (idle timeout, a dropped connection, a silent connection, 5xx) reconnects
 *   with backoff, bounded by MAX_CONSECUTIVE_FAILURES; a 4xx stops with an error.
 * - Nothing is stored in the browser; the state lives in memory for this page only.
 */
import { ApiError, errorFromResponse } from "./errors";
import { SseParser } from "./sse";
import type { ProgressEvent, RunStatus } from "./types";

export const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "abandoned"]);
export const MAX_CONSECUTIVE_FAILURES = 5;
export const RECONNECT_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 10_000] as const;
/** 3x the server keep-alive (SSE_KEEPALIVE_S = 15 s): a silent connection is presumed dead. */
export const SILENCE_TIMEOUT_MS = 45_000;

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed" | "error";

export interface NodeProgress {
  seq: number;
  node: string;
  status: string;
  at: string;
}

export interface RunProgress {
  runId: string;
  /** The current status (null until the first `run` event for this run arrives). */
  status: RunStatus | null;
  statusAt: string | null;
  nodes: readonly NodeProgress[];
}

export interface StreamSnapshot {
  status: StreamStatus;
  runs: readonly RunProgress[];
  lastEventId: string | null;
  error: ApiError | null;
  /** Consecutive failed connections since the last successful read. */
  failures: number;
}

export interface StreamDeps {
  fetchEvents: (
    sessionId: string,
    lastEventId: string | null,
    signal: AbortSignal,
  ) => Promise<Response>;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  silenceTimeoutMs?: number;
  /** Called after a `run` event changed a run's status (e.g. to refetch the session). */
  onRunStatus?: (runId: string, status: string) => void;
}

const RUN_STATUSES: ReadonlySet<string> = new Set([
  "running",
  "awaiting_clarification",
  "completed",
  "failed",
  "abandoned",
]);

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

function parseEvent(kind: string, data: string): ProgressEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  const e = value as Partial<ProgressEvent> | null;
  if (
    e === null ||
    typeof e !== "object" ||
    (e.kind !== "node" && e.kind !== "run") ||
    e.kind !== kind ||
    typeof e.run_id !== "string" ||
    typeof e.status !== "string" ||
    typeof e.at !== "string"
  ) {
    return null;
  }
  if (e.kind === "node" && (typeof e.seq !== "number" || typeof e.node !== "string")) return null;
  return e as ProgressEvent;
}

const EMPTY: StreamSnapshot = {
  status: "idle",
  runs: [],
  lastEventId: null,
  error: null,
  failures: 0,
};

export class SessionEventStream {
  private snapshot: StreamSnapshot = EMPTY;
  private readonly listeners = new Set<() => void>();
  private readonly seen = new Set<string>();
  private controller: AbortController | null = null;
  private loop: Promise<void> | null = null;

  constructor(
    readonly sessionId: string,
    private readonly deps: StreamDeps,
  ) {}

  // --- store (useSyncExternalStore) ----------------------------------------------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): StreamSnapshot => this.snapshot;

  private set(patch: Partial<StreamSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  // --- lifecycle ------------------------------------------------------------------------------

  /** Start (or resume after `closed` / `error`) following the session. Idempotent. */
  follow = (): void => {
    const finished = this.snapshot.status === "closed" || this.snapshot.status === "error";
    if (this.loop !== null && !finished) return;
    const controller = new AbortController();
    this.controller = controller;
    this.set({ status: "connecting", error: null, failures: 0 });
    this.loop = this.run(controller.signal).finally(() => {
      if (this.controller === controller) {
        this.controller = null;
        this.loop = null;
      }
    });
  };

  /** Stop and release the connection (unmount). The progress already received is kept. */
  stop = (): void => {
    this.controller?.abort();
    this.controller = null;
    this.loop = null;
    if (this.snapshot.status !== "closed" && this.snapshot.status !== "error") {
      this.set({ status: "idle" });
    }
  };

  /** Resolves when the current follow loop ends (tests). */
  settled(): Promise<void> {
    return this.loop ?? Promise.resolve();
  }

  private latestIsTerminal(): boolean {
    const latest = this.snapshot.runs.at(-1);
    return latest?.status != null && TERMINAL.has(latest.status);
  }

  private async run(signal: AbortSignal): Promise<void> {
    const sleep = this.deps.sleep ?? abortableSleep;
    let failures = 0;
    while (!signal.aborted) {
      const outcome = await this.connectOnce(signal);
      if (signal.aborted) return;
      if (outcome.kind === "fatal") {
        this.set({ status: "error", error: outcome.error, failures });
        return;
      }
      if (outcome.kind === "ended" && this.latestIsTerminal()) {
        this.set({ status: "closed", failures: 0 });
        return;
      }
      // Reconnect. A connection that delivered something resets the failure count; one that
      // delivered nothing (or failed) counts, so a misbehaving server cannot cause a tight loop.
      failures = outcome.received ? 0 : failures + 1;
      if (failures >= MAX_CONSECUTIVE_FAILURES) {
        this.set({
          status: "error",
          error: outcome.kind === "failed" ? outcome.error : new ApiError(0, "NETWORK_ERROR"),
          failures,
        });
        return;
      }
      this.set({ status: "reconnecting", failures });
      const delay = outcome.received
        ? RECONNECT_BACKOFF_MS[0]
        : (RECONNECT_BACKOFF_MS[Math.max(0, failures - 1)] ?? RECONNECT_BACKOFF_MS.at(-1)!);
      await sleep(delay, signal);
    }
  }

  private async connectOnce(
    signal: AbortSignal,
  ): Promise<
    | { kind: "ended"; received: boolean }
    | { kind: "failed"; received: boolean; error: ApiError }
    | { kind: "fatal"; error: ApiError }
  > {
    const connection = new AbortController();
    const onAbort = () => connection.abort();
    signal.addEventListener("abort", onAbort, { once: true });
    const silenceMs = this.deps.silenceTimeoutMs ?? SILENCE_TIMEOUT_MS;
    let silence: ReturnType<typeof setTimeout> | undefined;
    const armSilence = () => {
      clearTimeout(silence);
      silence = setTimeout(() => connection.abort(), silenceMs);
    };
    let received = false;
    try {
      armSilence();
      let res: Response;
      try {
        res = await this.deps.fetchEvents(
          this.sessionId,
          this.snapshot.lastEventId,
          connection.signal,
        );
      } catch {
        return { kind: "failed", received, error: new ApiError(0, "NETWORK_ERROR") };
      }
      if (!res.ok) {
        const error = await errorFromResponse(res);
        return res.status >= 500 ? { kind: "failed", received, error } : { kind: "fatal", error };
      }
      const type = res.headers.get("Content-Type") ?? "";
      if (!type.startsWith("text/event-stream") || res.body === null) {
        return { kind: "fatal", error: new ApiError(res.status, "BAD_RESPONSE") };
      }
      this.set({ status: "open", error: null });
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      // Aborting the request does not always end a pending read (only a real network body is
      // tied to the signal), so cancel the reader explicitly: stop() and the silence
      // watchdog must always release the connection.
      const cancel = () => void reader.cancel().catch(() => undefined);
      connection.signal.addEventListener("abort", cancel, { once: true });
      const parser = new SseParser();
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (connection.signal.aborted) {
            // stop() (the run loop exits) or the watchdog: a dead connection, not an end.
            return { kind: "failed", received, error: new ApiError(0, "NETWORK_ERROR") };
          }
          if (done) return { kind: "ended", received };
          received = true; // events and keep-alive comments both prove the connection is alive
          armSilence();
          for (const message of parser.push(value))
            this.apply(message.id, message.event, message.data);
        }
      } catch {
        return { kind: "failed", received, error: new ApiError(0, "NETWORK_ERROR") };
      } finally {
        connection.signal.removeEventListener("abort", cancel);
        cancel();
      }
    } finally {
      clearTimeout(silence);
      signal.removeEventListener("abort", onAbort);
    }
  }

  private apply(id: string | null, kind: string, data: string): void {
    if (id === null || this.seen.has(id)) return; // replayed or id-less: already applied / ignore
    const event = parseEvent(kind, data);
    if (event === null) return; // not part of the contract: ignored, never displayed
    this.seen.add(id);
    const runs = [...this.snapshot.runs];
    let index = runs.findIndex((r) => r.runId === event.run_id);
    if (index === -1) {
      runs.push({ runId: event.run_id, status: null, statusAt: null, nodes: [] });
      index = runs.length - 1;
    }
    const run = runs[index]!;
    if (event.kind === "node") {
      const seq = event.seq as number;
      if (!run.nodes.some((n) => n.seq === seq)) {
        const nodes = [
          ...run.nodes,
          { seq, node: event.node as string, status: event.status, at: event.at },
        ];
        nodes.sort((a, b) => a.seq - b.seq);
        runs[index] = { ...run, nodes };
      }
      this.set({ runs, lastEventId: id });
      return;
    }
    if (!RUN_STATUSES.has(event.status)) {
      this.set({ lastEventId: id });
      return;
    }
    const changed = run.status !== event.status;
    runs[index] = { ...run, status: event.status as RunStatus, statusAt: event.at };
    this.set({ runs, lastEventId: id });
    if (changed) this.deps.onRunStatus?.(event.run_id, event.status);
  }
}
