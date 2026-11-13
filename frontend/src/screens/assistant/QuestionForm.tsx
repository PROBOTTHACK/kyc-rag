import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { queryKeys, useSubmitMessage } from "../../api/hooks";
import { canTryAgain, errorCode, ErrorNotice } from "./ErrorNotice";

/** The backend's default QUERY_MAX_CHARS; the server enforces its own configured limit. */
const MAX_CHARS = 2000;

/**
 * Asks a new question. The text box is cleared after a successful submit, and also after a
 * SENSITIVE_DATA_DETECTED rejection so that personal data does not stay on screen. The text
 * is never copied into an error or a log.
 */
export function QuestionForm({
  sessionId,
  disabled,
  onSubmitted,
}: {
  sessionId: string;
  disabled: boolean;
  onSubmitted: () => void;
}) {
  const [text, setText] = useState("");
  const [cleared, setCleared] = useState(false);
  const submit = useSubmitMessage(sessionId);
  const queryClient = useQueryClient();

  function send(message: string) {
    setCleared(false);
    submit.mutate(
      { message },
      {
        onSuccess: () => {
          setText("");
          onSubmitted();
        },
        onError: (error) => {
          const code = errorCode(error);
          if (code === "SENSITIVE_DATA_DETECTED") {
            setText("");
            setCleared(true);
          }
          if (code === "RUN_IN_PROGRESS" || code === "SESSION_EXPIRED") {
            void queryClient.invalidateQueries({ queryKey: queryKeys.session(sessionId) });
          }
        },
      },
    );
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (text.trim() !== "") send(text);
  }

  const last = submit.variables;
  return (
    <form onSubmit={onSubmit} className="mt-4 rounded border border-slate-200 bg-white p-4">
      <label htmlFor="question" className="block font-semibold">
        Your question
      </label>
      <p id="question-hint" className="text-sm text-slate-600">
        Synthetic data only. Do not enter real names, ID numbers or account numbers.
      </p>
      <textarea
        id="question"
        aria-describedby="question-hint"
        className="mt-2 w-full rounded border border-slate-300 p-2"
        rows={3}
        maxLength={MAX_CHARS}
        value={text}
        disabled={disabled || submit.isPending}
        placeholder="For example: What KYC documents does an LLP need to open a current account?"
        onChange={(e) => setText(e.target.value)}
      />
      <button
        type="submit"
        className="mt-2 rounded bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
        disabled={disabled || submit.isPending || text.trim() === ""}
      >
        {submit.isPending ? "Sending…" : "Ask"}
      </button>
      {submit.isError && (
        <ErrorNotice error={submit.error}>
          {cleared && <p className="mt-1">The text box was cleared.</p>}
          {/* Same text = the same action: the client reuses its Idempotency-Key. */}
          {canTryAgain(submit.error) && last !== undefined && last.message === text && (
            <button
              type="button"
              className="mt-2 rounded border border-red-400 px-3 py-1"
              onClick={() => send(last.message)}
            >
              Try again
            </button>
          )}
        </ErrorNotice>
      )}
    </form>
  );
}
