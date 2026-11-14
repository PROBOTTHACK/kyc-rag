import { useState, type FormEvent, type KeyboardEvent } from "react";

import { TEXT } from "./labels";
import { Icon } from "./ui";
import { RealDataWarning } from "./WelcomeScreen";

/** The API screens up to 2 000 characters; longer text would be refused anyway. */
export const MAX_MESSAGE = 2000;

/** Free text: questions ("do I need a photograph?") or answers in the customer's own words. */
export function Composer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (message: string) => void;
}) {
  const [text, setText] = useState("");
  const trimmed = text.trim();
  const canSend = !disabled && trimmed.length > 0;

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canSend) return;
    onSend(trimmed);
    setText(""); // never kept: a blocked message must not stay on screen
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  }

  return (
    <form onSubmit={submit} className="border-t border-line bg-void px-4 pb-3 pt-3 sm:px-6">
      <div className="mx-auto max-w-3xl">
        <label htmlFor="composer" className="sr-only">
          {TEXT.composerLabel}
        </label>
        <div className="flex items-end gap-2 rounded-md border border-line-strong bg-surface py-1 pl-3 pr-1 transition-colors focus-within:border-accent focus-within:ring-1 focus-within:ring-accent">
          <textarea
            id="composer"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            maxLength={MAX_MESSAGE}
            rows={1}
            placeholder={TEXT.composerLabel}
            aria-describedby="composer-hint"
            autoComplete="off"
            spellCheck
            className="max-h-32 min-h-9 flex-1 resize-none bg-transparent py-1.5 text-[0.94rem] text-fg placeholder:text-subtle focus:outline-none [field-sizing:content]"
          />
          <button
            type="submit"
            disabled={!canSend}
            aria-label={TEXT.send}
            className="grid h-9 w-9 shrink-0 place-items-center rounded text-accent transition-colors hover:text-accent-strong disabled:text-line-strong"
          >
            <Icon name="send" className="h-4 w-4" />
          </button>
        </div>
        <div id="composer-hint" className="mt-2 flex items-center justify-between gap-3 px-1">
          <RealDataWarning compact />
          <span className="shrink-0 font-code text-[0.68rem] text-subtle" aria-live="polite">
            {text.length > MAX_MESSAGE - 200 ? `${text.length} / ${MAX_MESSAGE}` : ""}
          </span>
        </div>
      </div>
    </form>
  );
}
