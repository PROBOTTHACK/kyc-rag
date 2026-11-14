import { customerError, TEXT } from "./labels";
import type { Failure } from "./useConversation";

/** A turn that did not produce an answer: the API's fixed technical message, or a fixed
 * text for an error code. "Try again" resends the same turn with the same key. */
export function StatusNotice({
  failure,
  disabled,
  onRetry,
  onDismiss,
}: {
  failure: Failure;
  disabled: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const text =
    failure.technicalMessage ?? (failure.error ? customerError(failure.error) : TEXT.tryAgain);
  return (
    <div
      role="alert"
      className="rounded-xl bg-rose-50 p-4 text-sm text-rose-950 ring-1 ring-rose-200"
    >
      <p className="font-medium">{text}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {failure.retryable && (
          <button
            type="button"
            onClick={onRetry}
            disabled={disabled}
            className="min-h-11 rounded-lg bg-rose-900 px-4 font-semibold text-white hover:bg-rose-800 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-rose-700 disabled:opacity-60"
          >
            {TEXT.tryAgain}
          </button>
        )}
        <button
          type="button"
          onClick={onDismiss}
          disabled={disabled}
          className="min-h-11 rounded-lg px-4 font-medium text-rose-900 ring-1 ring-rose-300 hover:bg-rose-100 focus-visible:outline-2 focus-visible:outline-rose-700"
        >
          {TEXT.dismiss}
        </button>
      </div>
    </div>
  );
}
