import { useRef } from "react";

import type { AssistantConfirmation, AssistantQuestion, AssistantTurnRequest } from "../api/types";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { ACTION_LABELS, TEXT } from "./labels";
import { Icon, valueIcon } from "./ui";

type Send = (body: AssistantTurnRequest) => void;

/** Large selection cards. Choosing a card sends it at once (one question at a time). */
const cardClass =
  "group relative flex min-h-14 w-full items-center gap-3 rounded-xl border border-line bg-raised/60 px-3.5 py-3 text-left text-[0.92rem] font-medium text-fg transition-[border-color,background-color,transform] duration-150 hover:-translate-y-px hover:border-cyan/50 hover:bg-hover active:translate-y-0 disabled:pointer-events-none disabled:opacity-50 motion-reduce:hover:translate-y-0";
const linkClass =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg px-2.5 text-sm text-muted hover:bg-raised hover:text-fg disabled:opacity-50";

function Eyebrow({
  id,
  sr,
  label,
  hint,
}: {
  id: string;
  sr: string;
  label: string;
  hint?: string;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 id={id} ref={heading} tabIndex={-1} className="focus:outline-none">
        <span className="sr-only">{sr} </span>
        <span
          aria-hidden="true"
          className="font-code text-[0.68rem] uppercase tracking-[0.18em] text-cyan"
        >
          {label}
        </span>
      </h2>
      {hint && <p className="text-xs text-subtle">{hint}</p>}
    </div>
  );
}

/** The current question as selection cards, plus "not sure", "skip" and "explain" when the
 * API offers them. The choices are exactly the API's chips, in its order. */
export function QuestionCard({
  question,
  disabled,
  onSend,
}: {
  question: AssistantQuestion;
  disabled: boolean;
  onSend: Send;
}) {
  const headingId = `q-${question.slot}`;
  const many = question.chips.length > 4;
  return (
    <section aria-labelledby={headingId} className="oa-rise">
      <Eyebrow id={headingId} sr={question.text} label={TEXT.chooseOne} hint={TEXT.orType} />
      <div
        role="group"
        aria-labelledby={headingId}
        className={`mt-3 grid gap-2 ${many ? "sm:grid-cols-2 2xl:grid-cols-3" : "sm:grid-cols-2"}`}
      >
        {question.chips.map((chip) => (
          <button
            key={chip.value}
            type="button"
            disabled={disabled}
            className={cardClass}
            onClick={() => onSend({ action: "choose", slot: question.slot, value: chip.value })}
          >
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-surface text-muted ring-1 ring-line transition-colors group-hover:text-cyan group-hover:ring-cyan/40">
              <Icon name={valueIcon(chip.value)} className="h-4 w-4" />
            </span>
            <span className="flex-1">{chip.label}</span>
            <Icon
              name="arrow"
              className="h-4 w-4 text-subtle transition-colors group-hover:text-cyan"
            />
          </button>
        ))}
      </div>
      {(question.allow_not_sure || question.allow_skip || question.allow_explain) && (
        <div className="mt-2 flex flex-wrap gap-1">
          {question.allow_not_sure && (
            <button
              type="button"
              disabled={disabled}
              className={linkClass}
              onClick={() => onSend({ action: "not_sure", slot: question.slot })}
            >
              {ACTION_LABELS.not_sure}
            </button>
          )}
          {question.allow_skip && (
            <button
              type="button"
              disabled={disabled}
              className={linkClass}
              onClick={() => onSend({ action: "skip", slot: question.slot })}
            >
              {ACTION_LABELS.skip}
            </button>
          )}
          {question.allow_explain && (
            <button
              type="button"
              disabled={disabled}
              className={linkClass}
              onClick={() => onSend({ action: "explain", slot: question.slot })}
            >
              <Icon name="question" className="h-3.5 w-3.5" />
              {ACTION_LABELS.explain}
            </button>
          )}
        </div>
      )}
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
  return (
    <section aria-labelledby="confirm-heading" className="oa-rise">
      <Eyebrow id="confirm-heading" sr={confirmation.text} label="Please confirm" />
      <div role="group" aria-labelledby="confirm-heading" className="mt-3 grid grid-cols-2 gap-2">
        <button
          type="button"
          disabled={disabled}
          className={cardClass}
          onClick={() => onSend({ action: "confirm_yes" })}
        >
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-ok/10 text-ok ring-1 ring-ok/30">
            <Icon name="check" className="h-4 w-4" />
          </span>
          {ACTION_LABELS.confirm_yes}
        </button>
        <button
          type="button"
          disabled={disabled}
          className={cardClass}
          onClick={() => onSend({ action: "confirm_no" })}
        >
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-surface text-muted ring-1 ring-line">
            <Icon name="x" className="h-4 w-4" />
          </span>
          {ACTION_LABELS.confirm_no}
        </button>
      </div>
    </section>
  );
}
