import { useRef } from "react";

import type {
  AssistantConfirmation,
  AssistantQuestion,
  AssistantScenarioOffer,
  AssistantSuggestion,
  AssistantTurnRequest,
} from "../api/types";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { ACTION_LABELS, TEXT } from "./labels";
import { Icon, valueIcon } from "./ui";

type Send = (body: AssistantTurnRequest) => void;

/** Large selection cards. Choosing a card sends it at once (one question at a time). */
const cardClass =
  "group flex min-h-11 w-full items-center gap-3 rounded-md border border-line px-3 py-2 text-left text-sm font-medium text-fg transition-colors duration-150 hover:border-line-strong hover:bg-raised disabled:pointer-events-none disabled:opacity-50";
const linkClass =
  "inline-flex min-h-10 items-center gap-1.5 rounded-md px-2.5 text-sm text-subtle hover:bg-raised hover:text-fg disabled:opacity-50";

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
        <span aria-hidden="true" className="text-xs font-medium text-muted">
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
      <Eyebrow id={headingId} sr={question.text} label={TEXT.orType} />
      <div
        role="group"
        aria-labelledby={headingId}
        className={`mt-2 grid gap-1.5 ${many ? "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3" : "sm:grid-cols-2"}`}
      >
        {question.chips.map((chip) => (
          <button
            key={chip.value}
            type="button"
            disabled={disabled}
            className={cardClass}
            onClick={() => onSend({ action: "choose", slot: question.slot, value: chip.value })}
          >
            <span aria-hidden="true" className="text-subtle group-hover:text-fg">
              <Icon name={valueIcon(chip.value)} className="h-3.5 w-3.5" />
            </span>
            <span className="flex-1">{chip.label}</span>
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
      <Eyebrow id="confirm-heading" sr={confirmation.text} label="" />
      <div role="group" aria-labelledby="confirm-heading" className="mt-3 grid grid-cols-2 gap-2">
        <button
          type="button"
          disabled={disabled}
          className={cardClass}
          onClick={() => onSend({ action: "confirm_yes" })}
        >
          <span aria-hidden="true" className="text-subtle group-hover:text-fg">
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
          <span aria-hidden="true" className="text-subtle group-hover:text-fg">
            <Icon name="x" className="h-4 w-4" />
          </span>
          {ACTION_LABELS.confirm_no}
        </button>
      </div>
    </section>
  );
}

/** A message that may be about another account: a separate account, a change to this one,
 * or going back to an earlier one. Each option sends the API's own action; nothing changes
 * until the customer chooses. */
export function ScenarioCard({
  offer,
  disabled,
  onSend,
}: {
  offer: AssistantScenarioOffer;
  disabled: boolean;
  onSend: Send;
}) {
  return (
    <section aria-labelledby="scenario-heading" className="oa-rise">
      <Eyebrow id="scenario-heading" sr={offer.text} label="" />
      <div
        role="group"
        aria-labelledby="scenario-heading"
        className="mt-3 grid gap-2 sm:grid-cols-2"
      >
        {offer.options.map((o, i) => (
          <button
            key={o.action}
            type="button"
            disabled={disabled}
            className={cardClass}
            onClick={() =>
              onSend(
                (o.value != null
                  ? { action: o.action, value: o.value }
                  : { action: o.action }) as AssistantTurnRequest,
              )
            }
          >
            <span aria-hidden="true" className="text-subtle group-hover:text-fg">
              <Icon name={i === 0 ? "arrow" : "x"} className="h-4 w-4" />
            </span>
            <span className="flex-1">{o.label}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

/** After a not-established answer: the combinations the documents DO cover (from the API's
 * coverage map). Choosing one sends an ordinary `correct` turn, which the server validates. */
export function SuggestionCard({
  suggestions,
  disabled,
  onSend,
}: {
  suggestions: readonly AssistantSuggestion[];
  disabled: boolean;
  onSend: Send;
}) {
  return (
    <section aria-labelledby="suggestions-heading" className="oa-rise">
      <Eyebrow
        id="suggestions-heading"
        sr={`${TEXT.coveredHeading}.`}
        label={TEXT.coveredHeading}
        hint={TEXT.coveredHint}
      />
      <div
        role="group"
        aria-labelledby="suggestions-heading"
        className="mt-3 grid gap-2 sm:grid-cols-2"
      >
        {suggestions.map((s) => (
          <button
            key={`${s.slot}:${s.value}`}
            type="button"
            disabled={disabled}
            className={cardClass}
            onClick={() =>
              onSend({ action: "correct", slot: s.slot, value: s.value } as AssistantTurnRequest)
            }
          >
            <span aria-hidden="true" className="text-subtle group-hover:text-fg">
              <Icon name={valueIcon(s.value)} className="h-4 w-4" />
            </span>
            <span className="flex-1">{s.label}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
