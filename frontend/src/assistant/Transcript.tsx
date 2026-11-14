import { useEffect, useRef } from "react";

import type { AssistantAnswer } from "../api/types";
import { TEXT } from "./labels";
import type { Entry } from "./useConversation";
import { Spinner, WarningIcon } from "./WelcomeScreen";

/**
 * The conversation so far. Every assistant line comes from an API answer (messages,
 * confirmation, question, fallback); nothing is composed here. The customer's bubble shows
 * what they sent, except a blocked message, which is never shown back.
 */
export function Transcript({
  entries,
  pending,
  onViewChecklist,
}: {
  entries: Entry[];
  pending: boolean;
  onViewChecklist: () => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [entries.length, pending]);
  const lastAssistant = [...entries].reverse().find((e) => e.role === "assistant");
  // The first checklist is "ready"; later checklist answers (a follow-up question, a change)
  // say it was updated.
  const firstChecklist = entries.find(
    (e) => e.role === "assistant" && e.answer.kind === "checklist",
  );
  return (
    <div className="flex flex-col gap-4 p-4 sm:p-6">
      <ol aria-label="Conversation" className="flex flex-col gap-4">
        {entries.map((entry) =>
          entry.role === "customer" ? (
            <li key={entry.id} className="flex justify-end">
              {entry.blocked ? (
                <p className="max-w-[85%] rounded-2xl rounded-br-md border border-dashed border-slate-300 bg-slate-50 px-4 py-2.5 text-sm italic text-slate-600">
                  <span className="sr-only">You: </span>
                  {TEXT.messageNotSent}
                </p>
              ) : (
                <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-slate-900 px-4 py-2.5 text-[0.95rem] leading-relaxed text-white">
                  <span className="sr-only">You: </span>
                  {entry.text}
                </p>
              )}
            </li>
          ) : (
            <li key={entry.id} className="flex justify-start">
              <AssistantBubble
                answer={entry.answer}
                latest={entry === lastAssistant}
                updated={entry !== firstChecklist}
                onViewChecklist={onViewChecklist}
              />
            </li>
          ),
        )}
        {pending && (
          <li className="flex justify-start" aria-hidden="true">
            <p className="flex items-center gap-2 rounded-2xl rounded-bl-md bg-white px-4 py-2.5 text-sm text-slate-500 ring-1 ring-slate-200">
              <Spinner /> {TEXT.sending}
            </p>
          </li>
        )}
      </ol>
      <div ref={end} />
    </div>
  );
}

function AssistantBubble({
  answer,
  latest,
  updated,
  onViewChecklist,
}: {
  answer: AssistantAnswer;
  latest: boolean;
  updated: boolean;
  onViewChecklist: () => void;
}) {
  const messages = answer.messages ?? [];
  const privacy = messages.filter((m) => m.code === "privacy");
  const other = messages.filter((m) => m.code !== "privacy");
  return (
    <div className="flex max-w-[92%] flex-col gap-2 sm:max-w-[85%]">
      <span className="sr-only">Assistant: </span>
      {privacy.map((m, i) => (
        <p
          key={`p${i}`}
          className="flex gap-2 rounded-2xl rounded-bl-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm leading-relaxed text-amber-950"
        >
          <WarningIcon />
          <span>{m.text}</span>
        </p>
      ))}
      <div className="rounded-2xl rounded-bl-md bg-white px-4 py-3 text-[0.95rem] leading-relaxed text-slate-800 shadow-sm ring-1 ring-slate-200">
        {other.map((m, i) => (
          <p
            key={`m${i}`}
            className={m.code === "blocked" ? "font-medium text-amber-900" : "mb-2 last:mb-0"}
          >
            {m.text}
          </p>
        ))}
        {answer.fallback && <p className="mb-2 font-medium last:mb-0">{answer.fallback}</p>}
        {answer.confirmation && <p className="font-medium">{answer.confirmation.text}</p>}
        {answer.question && <p className="font-medium">{answer.question.text}</p>}
        {answer.kind === "checklist" && (
          <div className="flex flex-wrap items-center gap-3">
            <p className="font-medium">{updated ? TEXT.checklistUpdated : TEXT.checklistReady}</p>
            {latest && (
              <button
                type="button"
                onClick={onViewChecklist}
                className="rounded-md text-sm font-semibold text-sky-800 underline decoration-sky-300 underline-offset-4 hover:decoration-sky-700 focus-visible:outline-2 focus-visible:outline-sky-600"
              >
                {TEXT.viewChecklist}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
