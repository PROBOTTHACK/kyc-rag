import { useId, useState } from "react";

import type { AssistantAnswer, AssistantTurnRequest, ProfileSlot } from "../api/types";
import { ORIGIN_LABELS, PROGRESS_STEPS, slotLabel, STATUS_LABELS, TEXT } from "./labels";
import { Icon, valueIcon } from "./ui";

type StepState = "done" | "current" | "upcoming" | "unavailable";

/**
 * Progress through the conversation, derived only from what the API returned: which
 * profile answers are known and what kind of answer came last. It does not decide which
 * questions are still to come.
 */
export function progressOf(answer: AssistantAnswer): StepState[] {
  const known = (slot: string) => answer.profile[slot]?.status === "known";
  const conversion = answer.coverage_state === "CONVERSION_ONLY";
  const checklist = answer.kind === "checklist";
  const notCovered = answer.coverage_state === "NOT_ESTABLISHED";
  const done = [
    known("account_type") || conversion,
    known("customer_category") || conversion,
    checklist,
    checklist,
  ];
  const states: StepState[] = [];
  let currentSet = false;
  done.forEach((d, i) => {
    if (d) states.push("done");
    else if (notCovered && i >= 2) states.push("unavailable");
    else if (!currentSet) {
      states.push("current");
      currentSet = true;
    } else states.push("upcoming");
  });
  return states;
}

const SR_STATE: Record<StepState, string> = {
  done: " (done)",
  current: " (current step)",
  upcoming: "",
  unavailable: "",
};

/** The vertical stepper (desktop rail and the answers sheet). */
export function ProgressSteps({ answer }: { answer: AssistantAnswer }) {
  const states = progressOf(answer);
  return (
    <nav aria-label={TEXT.progress}>
      <p className="font-code text-[0.65rem] uppercase tracking-[0.18em] text-subtle">
        {TEXT.progress}
      </p>
      <ol className="mt-3 grid">
        {PROGRESS_STEPS.map((label, i) => {
          const s = states[i]!;
          const last = i === PROGRESS_STEPS.length - 1;
          return (
            <li
              key={label}
              aria-current={s === "current" ? "step" : undefined}
              className="relative flex gap-3 pb-3 last:pb-0"
            >
              {!last && (
                <span
                  aria-hidden="true"
                  className={`absolute left-[0.6875rem] top-6 h-[calc(100%-1.25rem)] w-px transition-colors duration-300 ${
                    s === "done" ? "bg-ok/50" : "bg-line"
                  }`}
                />
              )}
              <span
                aria-hidden="true"
                className={`relative grid h-[1.375rem] w-[1.375rem] shrink-0 place-items-center rounded-full font-code text-[0.65rem] transition-all duration-300 ${
                  s === "done"
                    ? "bg-ok/15 text-ok ring-1 ring-ok/50"
                    : s === "current"
                      ? "bg-cyan/15 text-cyan ring-1 ring-cyan/60 shadow-[0_0_0_4px_rgba(84,214,255,0.08)]"
                      : "bg-raised text-subtle ring-1 ring-line"
                }`}
              >
                {s === "done" ? (
                  <Icon name="check" className="h-3 w-3" strokeWidth={2.2} />
                ) : s === "unavailable" ? (
                  "–"
                ) : (
                  i + 1
                )}
              </span>
              <span className="min-w-0 pt-px">
                <span
                  className={`block text-sm ${
                    s === "current"
                      ? "font-semibold text-fg"
                      : s === "done"
                        ? "text-muted"
                        : "text-subtle"
                  }`}
                >
                  {label}
                  <span className="sr-only">{SR_STATE[s]}</span>
                </span>
                {s === "unavailable" && (
                  <span className="block text-xs text-subtle">Not available for these answers</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** A one-line progress bar for narrow screens. */
export function ProgressCompact({ answer }: { answer: AssistantAnswer }) {
  const states = progressOf(answer);
  const currentIndex = states.findIndex((s) => s === "current");
  const index = Math.max(currentIndex === -1 ? states.lastIndexOf("done") : currentIndex, 0);
  const unavailable = states.includes("unavailable");
  return (
    <div>
      <p className="flex items-baseline gap-2 text-xs">
        <span className="font-code text-subtle">
          {index + 1}/{PROGRESS_STEPS.length}
        </span>
        <span className="truncate font-medium text-fg">
          {unavailable ? "No checklist for these answers" : PROGRESS_STEPS[index]}
        </span>
      </p>
      <div aria-hidden="true" className="mt-1.5 grid grid-cols-4 gap-1">
        {states.map((s, i) => (
          <span
            key={i}
            className={`h-1 rounded-full transition-colors duration-300 ${
              s === "done"
                ? "bg-ok/70"
                : s === "current"
                  ? "bg-cyan"
                  : s === "unavailable"
                    ? "bg-danger/40"
                    : "bg-line"
            }`}
          />
        ))}
      </div>
    </div>
  );
}

/** The customer's answers as the API holds them, each with a "Change" control offering the
 * slot's valid choices (from the API). A change is sent as a `correct` turn; the server
 * validates it and decides what follows. */
export function ProfilePanel({
  answer,
  disabled,
  onSend,
}: {
  answer: AssistantAnswer;
  disabled: boolean;
  onSend: (body: AssistantTurnRequest) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const headingId = useId();
  const rows = Object.entries(answer.profile).filter(([, p]) => p.status !== "unknown");
  return (
    <section aria-labelledby={headingId} className="rounded-2xl border border-line bg-surface p-4">
      <h2
        id={headingId}
        className="font-code text-[0.65rem] uppercase tracking-[0.18em] text-subtle"
      >
        {TEXT.yourAnswers}
      </h2>
      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-subtle">{TEXT.noAnswersYet}</p>
      ) : (
        <dl className="mt-2 divide-y divide-line">
          {rows.map(([slot, p]) => (
            <ProfileRow
              key={slot}
              slot={slot}
              entry={p}
              editing={editing === slot}
              disabled={disabled}
              onEdit={() => setEditing(editing === slot ? null : slot)}
              onChoose={(value) => {
                setEditing(null);
                onSend({ action: "correct", slot, value } as AssistantTurnRequest);
              }}
            />
          ))}
        </dl>
      )}
    </section>
  );
}

function ProfileRow({
  slot,
  entry,
  editing,
  disabled,
  onEdit,
  onChoose,
}: {
  slot: string;
  entry: ProfileSlot;
  editing: boolean;
  disabled: boolean;
  onEdit: () => void;
  onChoose: (value: string) => void;
}) {
  const name = slotLabel(slot);
  const known = entry.status === "known";
  const value = known ? entry.label : STATUS_LABELS[entry.status];
  const choices = entry.choices ?? [];
  const listId = useId();
  return (
    <div className="py-3 first:pt-2 last:pb-0">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <dt className="text-xs text-subtle">{name}</dt>
          <dd
            className={`mt-0.5 flex items-start gap-1.5 text-sm ${
              known ? "font-medium text-fg" : "italic text-muted"
            }`}
          >
            <span
              aria-hidden="true"
              className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${known ? "bg-ok" : "bg-warn/70"}`}
            />
            <span>{value}</span>
          </dd>
          {known && entry.origin && (
            <dd className="ml-3 text-[0.7rem] text-subtle">{ORIGIN_LABELS[entry.origin] ?? ""}</dd>
          )}
        </div>
        {choices.length > 0 && (
          <button
            type="button"
            onClick={onEdit}
            disabled={disabled}
            aria-expanded={editing}
            aria-controls={editing ? listId : undefined}
            aria-label={`${editing ? TEXT.cancel : TEXT.change} ${name}`}
            className="min-h-9 shrink-0 rounded-lg px-2.5 text-xs font-medium text-cyan hover:bg-raised disabled:opacity-50"
          >
            {editing ? TEXT.cancel : TEXT.change}
          </button>
        )}
      </div>
      {editing && (
        <div
          id={listId}
          role="group"
          aria-label={TEXT.changeTo(name)}
          className="oa-rise mt-2 grid gap-1"
        >
          {choices.map((c) => {
            const current = known && entry.value === c.value;
            return (
              <button
                key={c.value}
                type="button"
                disabled={disabled || current}
                aria-current={current ? "true" : undefined}
                onClick={() => onChoose(c.value)}
                className="flex min-h-11 items-center gap-2.5 rounded-lg border border-line bg-raised/50 px-2.5 py-2 text-left text-sm text-fg hover:border-cyan/50 hover:bg-hover disabled:cursor-default disabled:border-cyan/40 disabled:bg-cyan/10"
              >
                <Icon name={valueIcon(c.value)} className="h-3.5 w-3.5 text-muted" />
                <span className="flex-1">
                  {c.label}
                  {current && <span className="sr-only"> (current answer)</span>}
                </span>
                {current && <Icon name="check" className="h-3.5 w-3.5 text-cyan" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
