import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";

import { queryKeys, useResume } from "../../api/hooks";
import { useFocusWhenLost } from "../../components/useFocusWhenLost";
import type { PendingClarification } from "../../api/types";
import { canTryAgain, errorCode, ErrorNotice } from "./ErrorNotice";
import { optionLabel, QUESTION_REASON_TEXT } from "./labels";

/**
 * The pending clarification, answered only with the backend's own options. Nothing is
 * pre-selected or guessed, and every asked question must be answered before sending (an
 * unanswered field would be asked again and use up a round). Mount it with
 * `key={checkpoint_id}`, so a new checkpoint always starts with empty answers.
 */
export function ClarificationForm({
  sessionId,
  clarification,
  onResumed,
}: {
  sessionId: string;
  clarification: PendingClarification;
  onResumed: () => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const resume = useResume(sessionId);
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  const queryClient = useQueryClient();
  const { questions } = clarification;
  const complete = questions.every((q) => answers[q.field] !== undefined);

  function send() {
    // Only the asked fields, only listed options (the state can hold nothing else).
    const body = {
      checkpoint_id: clarification.checkpoint_id,
      answers: Object.fromEntries(questions.map((q) => [q.field, answers[q.field] as string])),
    };
    resume.mutate(body, {
      onSuccess: onResumed,
      onError: (error) => {
        const code = errorCode(error);
        if (
          code === "STALE_CHECKPOINT" ||
          code === "NOT_AWAITING_CLARIFICATION" ||
          code === "SESSION_EXPIRED" ||
          code === "RUN_IN_PROGRESS"
        ) {
          // Load the current state; a new checkpoint remounts this form with empty answers.
          void queryClient.invalidateQueries({ queryKey: queryKeys.session(sessionId) });
        }
      },
    });
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (complete) send();
  }

  return (
    <form onSubmit={onSubmit} className="mt-4 rounded border border-amber-300 bg-amber-50 p-4">
      <h2 ref={heading} tabIndex={-1} className="text-lg font-semibold">
        More information needed
      </h2>
      {clarification.round != null && clarification.max_rounds != null && (
        <p className="text-sm text-slate-700">
          Round {clarification.round} of {clarification.max_rounds}
        </p>
      )}
      {questions.map((q) => {
        const reason = QUESTION_REASON_TEXT[q.reason];
        return (
          <fieldset key={q.field} className="mt-3">
            <legend className="font-medium">{q.text}</legend>
            {reason !== null && <p className="text-sm text-slate-700">{reason}</p>}
            <div className="mt-1 grid gap-1 sm:grid-cols-2">
              {q.options.map((option) => (
                <label key={option} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name={q.field}
                    value={option}
                    checked={answers[q.field] === option}
                    disabled={resume.isPending}
                    onChange={() => setAnswers((a) => ({ ...a, [q.field]: option }))}
                  />
                  {optionLabel(option)}
                </label>
              ))}
            </div>
          </fieldset>
        );
      })}
      <button
        type="submit"
        className="mt-3 rounded bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
        disabled={!complete || resume.isPending}
      >
        {resume.isPending ? "Sending…" : "Send answers"}
      </button>
      {!complete && (
        <p className="mt-1 text-sm text-slate-600">Answer every question to continue.</p>
      )}
      {resume.isError && (
        <ErrorNotice error={resume.error}>
          {canTryAgain(resume.error) && (
            <button
              type="button"
              className="mt-2 rounded border border-red-400 px-3 py-1"
              onClick={send}
            >
              Try again
            </button>
          )}
        </ErrorNotice>
      )}
    </form>
  );
}
