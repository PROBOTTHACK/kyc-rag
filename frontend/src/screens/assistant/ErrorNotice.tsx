import type { ReactNode } from "react";

import { ApiError } from "../../api/errors";

/**
 * Shows an error by its fixed, per-code message only. Nothing from the request, the user's
 * input or the server's own text is ever rendered here (M5_PLAN §E rule 8).
 */
export function ErrorNotice({ error, children }: { error: unknown; children?: ReactNode }) {
  const known = error instanceof ApiError ? error : null;
  const message = known?.userMessage ?? "Something went wrong.";
  const wait =
    known?.code === "RUN_IN_PROGRESS" && known.retryAfter !== null
      ? ` You can try again in about ${known.retryAfter} seconds.`
      : "";
  return (
    <div
      role="alert"
      className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-950"
    >
      <p>
        {message}
        {wait}
      </p>
      {children}
    </div>
  );
}

export function errorCode(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null;
}

/** True when trying the same action again may help (no response or a server error). */
export function canTryAgain(error: unknown): boolean {
  return error instanceof ApiError && error.outcomeUnknown;
}
