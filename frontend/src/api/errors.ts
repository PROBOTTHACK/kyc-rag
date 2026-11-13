/**
 * API errors (docs/07 error envelope). The UI shows only the fixed message for a code; the
 * server's message, the request body and any user input are never copied into an error.
 */

/** Codes produced by the client itself (no response, or a response without the envelope). */
export type ClientErrorCode = "NETWORK_ERROR" | "TIMEOUT" | "BAD_RESPONSE" | "UNKNOWN_ERROR";

const MESSAGES: Record<string, string> = {
  VALIDATION_ERROR: "The request was not valid.",
  SENSITIVE_DATA_DETECTED:
    "The text appears to contain personal data (for example an ID or account number), so it " +
    "was not processed. Use synthetic data only.",
  INPUT_REJECTED: "This message cannot be processed.",
  INVALID_ANSWER: "Answer each question with one of the listed options.",
  SESSION_NOT_FOUND: "This session was not found. It may have expired and been deleted.",
  REPORT_NOT_FOUND:
    "This report was not found. It may have been deleted after the retention period.",
  SESSION_EXPIRED: "This session has expired. Start a new session.",
  IDEMPOTENCY_KEY_REQUIRED: "The request could not be sent correctly. Please try again.",
  IDEMPOTENCY_MISMATCH: "The request could not be sent correctly. Please try again.",
  RUN_IN_PROGRESS: "A question is already being processed in this session.",
  NOT_AWAITING_CLARIFICATION: "There is no question waiting for an answer.",
  STALE_CHECKPOINT:
    "The question you answered is no longer the current one. Load the latest question and answer again.",
  NOTHING_TO_RETRY: "There is nothing to retry in this session.",
  WORKFLOW_UNAVAILABLE: "The service is not available right now. Please try again later.",
  INTERNAL_ERROR: "Something went wrong on the server.",
  NETWORK_ERROR: "The server could not be reached.",
  TIMEOUT: "The server did not respond in time.",
  BAD_RESPONSE: "The server returned an unexpected response.",
  UNKNOWN_ERROR: "Something went wrong.",
};

const CODE = /^[A-Z][A-Z0-9_]{0,63}$/;

export function messageFor(code: string): string {
  return MESSAGES[code] ?? MESSAGES.UNKNOWN_ERROR!;
}

export class ApiError extends Error {
  override readonly name = "ApiError";
  /** HTTP status, or 0 when no response was received. */
  readonly status: number;
  /** A stable code: a backend envelope code or a ClientErrorCode. */
  readonly code: string;
  /** Screening category codes (codes only, never text). */
  readonly categories: readonly string[];
  /** Seconds, from the envelope or the Retry-After header. */
  readonly retryAfter: number | null;

  constructor(
    status: number,
    code: string,
    extra: { categories?: readonly string[]; retryAfter?: number | null } = {},
  ) {
    const safeCode = CODE.test(code) ? code : "UNKNOWN_ERROR";
    super(messageFor(safeCode));
    this.status = status;
    this.code = safeCode;
    this.categories = (extra.categories ?? []).filter((c) => /^[a-z0-9_:-]{1,64}$/i.test(c));
    this.retryAfter = extra.retryAfter ?? null;
  }

  /** The fixed, user-facing text for this code. */
  get userMessage(): string {
    return this.message;
  }

  /** No response, or a server-side failure: the request may or may not have taken effect,
   * so a retry of the same action must reuse its Idempotency-Key. */
  get outcomeUnknown(): boolean {
    return this.status === 0 || this.status >= 500;
  }

  /** Worth retrying automatically (bounded): no response or a gateway/availability error. */
  get transient(): boolean {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

/** Build an ApiError from an error response without reading anything but codes. */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  const header = Number.parseInt(res.headers.get("Retry-After") ?? "", 10);
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  const err = (body as { error?: Record<string, unknown> } | null)?.error;
  if (err === undefined || err === null || typeof err.code !== "string") {
    return new ApiError(res.status, res.status >= 500 ? "BAD_RESPONSE" : "UNKNOWN_ERROR", {
      retryAfter: Number.isFinite(header) ? header : null,
    });
  }
  const categories = Array.isArray(err.categories)
    ? err.categories.filter((c): c is string => typeof c === "string")
    : [];
  const retryAfter =
    typeof err.retry_after === "number" ? err.retry_after : Number.isFinite(header) ? header : null;
  return new ApiError(res.status, err.code, { categories, retryAfter });
}
