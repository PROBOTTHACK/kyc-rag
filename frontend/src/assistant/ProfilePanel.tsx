import { useState } from "react";

import type { AssistantAnswer, AssistantTurnRequest, ProfileSlot } from "../api/types";
import { ORIGIN_LABELS, PROGRESS_STEPS, slotLabel, STATUS_LABELS, TEXT } from "./labels";

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

export function ProgressSteps({ answer }: { answer: AssistantAnswer }) {
  const states = progressOf(answer);
  return (
    <nav aria-label={TEXT.progress}>
      <ol className="flex gap-1.5 sm:grid sm:gap-2">
        {PROGRESS_STEPS.map((label, i) => {
          const s = states[i]!;
          return (
            <li
              key={label}
              aria-current={s === "current" ? "step" : undefined}
              className="flex flex-1 flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3"
            >
              <span
                aria-hidden="true"
                className={`grid h-1.5 w-full place-items-center rounded-full text-[0.7rem] font-semibold sm:h-7 sm:w-7 sm:shrink-0 ${
                  s === "done"
                    ? "bg-emerald-600 text-white"
                    : s === "current"
                      ? "bg-sky-700 text-white ring-4 ring-sky-100"
                      : "bg-slate-200 text-slate-500"
                }`}
              >
                <span className="hidden sm:inline">
                  {s === "done" ? "✓" : s === "unavailable" ? "–" : i + 1}
                </span>
              </span>
              <span
                className={`text-[0.7rem] leading-tight sm:text-sm ${
                  s === "current" ? "font-semibold text-slate-900" : "text-slate-600"
                } ${s === "upcoming" || s === "unavailable" ? "max-sm:sr-only" : ""}`}
              >
                {label}
                <span className="sr-only">
                  {s === "done" ? " (done)" : s === "current" ? " (current step)" : ""}
                </span>
                {s === "unavailable" && (
                  <span className="block text-xs font-normal text-slate-500 max-sm:sr-only">
                    Not available for these answers
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
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
  const rows = Object.entries(answer.profile).filter(([, p]) => p.status !== "unknown");
  return (
    <section
      aria-labelledby="your-answers"
      className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200 sm:p-5"
    >
      <h2 id="your-answers" className="font-semibold text-slate-900">
        {TEXT.yourAnswers}
      </h2>
      {rows.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">{TEXT.noAnswersYet}</p>
      ) : (
        <dl className="mt-3 divide-y divide-slate-100">
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
  const value = entry.status === "known" ? entry.label : STATUS_LABELS[entry.status];
  const choices = entry.choices ?? [];
  const listId = `change-${slot}`;
  return (
    <div className="py-3 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <dt className="text-xs font-medium uppercase tracking-wider text-slate-500">{name}</dt>
          <dd className="mt-0.5 font-medium text-slate-900">{value}</dd>
          {entry.status === "known" && entry.origin && (
            <dd className="text-xs text-slate-500">{ORIGIN_LABELS[entry.origin] ?? ""}</dd>
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
            className="min-h-11 shrink-0 rounded-lg px-3 text-sm font-medium text-sky-800 hover:bg-sky-50 focus-visible:outline-2 focus-visible:outline-sky-600 disabled:opacity-60"
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
          className="mt-2 grid gap-1.5"
        >
          {choices.map((c) => {
            const current = entry.status === "known" && entry.value === c.value;
            return (
              <button
                key={c.value}
                type="button"
                disabled={disabled || current}
                aria-current={current ? "true" : undefined}
                onClick={() => onChoose(c.value)}
                className="min-h-11 rounded-lg border border-slate-200 px-3 py-2 text-left text-sm text-slate-800 hover:border-sky-600 hover:bg-sky-50 focus-visible:outline-2 focus-visible:outline-sky-600 disabled:cursor-default disabled:border-sky-200 disabled:bg-sky-50 disabled:font-medium"
              >
                {c.label}
                {current && <span className="sr-only"> (current answer)</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
