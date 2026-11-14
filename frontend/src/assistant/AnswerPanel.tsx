import { useRef } from "react";

import type { AssistantConfirmation, AssistantQuestion, AssistantTurnRequest } from "../api/types";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { ACTION_LABELS, TEXT } from "./labels";

type Send = (body: AssistantTurnRequest) => void;

const card =
  "group flex min-h-14 w-full items-center justify-between gap-3 rounded-xl border border-slate-300 bg-white px-4 py-3 text-left text-[0.95rem] font-medium text-slate-900 shadow-sm transition-colors hover:border-sky-600 hover:bg-sky-50 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-sky-600 disabled:cursor-not-allowed disabled:opacity-60 motion-reduce:transition-none";
const link =
  "min-h-11 rounded-lg px-3 text-sm font-medium text-slate-700 underline decoration-slate-300 underline-offset-4 hover:text-slate-950 hover:decoration-slate-600 focus-visible:outline-2 focus-visible:outline-sky-600 disabled:opacity-60";

/** The current question as large choice cards, plus "not sure", "skip" and "explain" when
 * the API offers them. Choosing a card sends it at once. */
export function QuestionCard({
  question,
  disabled,
  onSend,
}: {
  question: AssistantQuestion;
  disabled: boolean;
  onSend: Send;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  const headingId = `q-${question.slot}`;
  return (
    <section
      aria-labelledby={headingId}
      className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200 sm:p-5"
    >
      <h2
        id={headingId}
        ref={heading}
        tabIndex={-1}
        className="font-semibold text-slate-900 focus:outline-none"
      >
        <span className="sr-only">{question.text} </span>
        <span
          aria-hidden="true"
          className="text-sm font-medium uppercase tracking-wider text-slate-500"
        >
          {TEXT.chooseOne}
        </span>
      </h2>
      <div role="group" aria-labelledby={headingId} className="mt-3 grid gap-2 sm:grid-cols-2">
        {question.chips.map((chip) => (
          <button
            key={chip.value}
            type="button"
            disabled={disabled}
            className={card}
            onClick={() => onSend({ action: "choose", slot: question.slot, value: chip.value })}
          >
            <span>{chip.label}</span>
            <span aria-hidden="true" className="text-slate-400 group-hover:text-sky-700">
              →
            </span>
          </button>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-1">
        {question.allow_not_sure && (
          <button
            type="button"
            disabled={disabled}
            className={link}
            onClick={() => onSend({ action: "not_sure", slot: question.slot })}
          >
            {ACTION_LABELS.not_sure}
          </button>
        )}
        {question.allow_skip && (
          <button
            type="button"
            disabled={disabled}
            className={link}
            onClick={() => onSend({ action: "skip", slot: question.slot })}
          >
            {ACTION_LABELS.skip}
          </button>
        )}
        {question.allow_explain && (
          <button
            type="button"
            disabled={disabled}
            className={link}
            onClick={() => onSend({ action: "explain", slot: question.slot })}
          >
            {ACTION_LABELS.explain}
          </button>
        )}
      </div>
    </section>
  );
}

/** A yes / no check of a value the assistant proposed. */
export function ConfirmCard({
  confirmation,
  disabled,
  onSend,
}: {
  confirmation: AssistantConfirmation;
  disabled: boolean;
  onSend: Send;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  return (
    <section
      aria-labelledby="confirm-heading"
      className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200 sm:p-5"
    >
      <h2
        id="confirm-heading"
        ref={heading}
        tabIndex={-1}
        className="font-semibold text-slate-900 focus:outline-none"
      >
        <span className="sr-only">{confirmation.text}</span>
        <span
          aria-hidden="true"
          className="text-sm font-medium uppercase tracking-wider text-slate-500"
        >
          Please confirm
        </span>
      </h2>
      <div role="group" aria-labelledby="confirm-heading" className="mt-3 grid grid-cols-2 gap-2">
        <button
          type="button"
          disabled={disabled}
          className={card}
          onClick={() => onSend({ action: "confirm_yes" })}
        >
          {ACTION_LABELS.confirm_yes}
        </button>
        <button
          type="button"
          disabled={disabled}
          className={card}
          onClick={() => onSend({ action: "confirm_no" })}
        >
          {ACTION_LABELS.confirm_no}
        </button>
      </div>
    </section>
  );
}
