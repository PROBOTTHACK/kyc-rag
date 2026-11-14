import { customerError, TEXT } from "./labels";
import { Button, Icon } from "./ui";
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
    <div role="alert" className="oa-rise border-l-2 border-danger py-1 pl-4">
      <p className="flex gap-2 text-sm font-medium text-fg">
        <Icon name="alert" className="mt-0.5 h-4 w-4 text-danger" />
        <span>{text}</span>
      </p>
      <div className="mt-3 flex flex-wrap gap-2 pl-6">
        {failure.retryable && (
          <Button variant="danger" onClick={onRetry} disabled={disabled}>
            <Icon name="refresh" />
            {TEXT.tryAgain}
          </Button>
        )}
        <Button variant="ghost" onClick={onDismiss} disabled={disabled}>
          {TEXT.dismiss}
        </Button>
      </div>
    </div>
  );
}
