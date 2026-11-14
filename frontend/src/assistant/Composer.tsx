import { useState, type FormEvent, type KeyboardEvent } from "react";

import { TEXT } from "./labels";
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
    <form onSubmit={submit} className="border-t border-slate-200 bg-white p-3 sm:p-4">
      <RealDataWarning compact />
      <label htmlFor="composer" className="sr-only">
        {TEXT.composerLabel}
      </label>
      <div className="mt-2 flex items-end gap-2">
        <textarea
          id="composer"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          maxLength={MAX_MESSAGE}
          rows={2}
          placeholder={TEXT.composerLabel}
          aria-describedby="composer-hint"
          autoComplete="off"
          spellCheck
          className="min-h-12 flex-1 resize-none rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-[0.95rem] text-slate-900 placeholder:text-slate-400 focus:border-sky-600 focus:outline-2 focus:outline-sky-600/30"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="min-h-12 rounded-xl bg-slate-900 px-5 font-semibold text-white hover:bg-slate-800 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-sky-600 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {TEXT.send}
        </button>
      </div>
      <p id="composer-hint" className="mt-1.5 flex justify-between gap-2 text-xs text-slate-500">
        <span>{TEXT.composerHint}</span>
        <span aria-live="polite">
          {text.length > MAX_MESSAGE - 200 ? `${text.length} / ${MAX_MESSAGE}` : ""}
        </span>
      </p>
    </form>
  );
}
