/**
 * Idempotency-Key handling for one kind of state-changing action (docs/07):
 * - a genuinely new action gets a new key;
 * - retrying the SAME action after an unknown outcome (no response, 5xx) reuses its key, so
 *   the backend replays the stored response instead of starting a second run;
 * - after a success or a definitive rejection (4xx) the key is retired.
 *
 * "Same action" means the same endpoint, session and body. The fingerprint lives in memory
 * only (never stored or logged) and is dropped once the action settles.
 */
import { ApiError } from "./errors";

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export class IdempotencyTracker {
  private pending: { fingerprint: string; key: string } | null = null;

  constructor(private readonly makeKey: () => string = newIdempotencyKey) {}

  /** The key for this attempt: the pending key when it is a retry of the same action. */
  keyFor(fingerprint: string): string {
    if (this.pending?.fingerprint !== fingerprint) {
      this.pending = { fingerprint, key: this.makeKey() };
    }
    return this.pending.key;
  }

  /** Record how the attempt ended. Only an unknown outcome keeps the key for a retry. */
  settle(error?: unknown): void {
    if (error instanceof ApiError && error.outcomeUnknown) {
      return;
    }
    this.pending = null;
  }

  /** Run one attempt of an action with the right key, and settle it. */
  async run<T>(fingerprint: string, send: (key: string) => Promise<T>): Promise<T> {
    const key = this.keyFor(fingerprint);
    try {
      const result = await send(key);
      this.settle();
      return result;
    } catch (error) {
      this.settle(error);
      throw error;
    }
  }
}

/** A stable fingerprint: endpoint, session and the body with sorted keys. */
export function fingerprint(endpoint: string, sessionId: string, body: unknown): string {
  return JSON.stringify([endpoint, sessionId, canonical(body)]);
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((k) => [k, canonical((value as Record<string, unknown>)[k])]),
    );
  }
  return value;
}
