import { useEffect, useRef } from "react";

import type { AssistantAnswer } from "../api/types";
import { TEXT } from "./labels";
import { Icon, Thinking } from "./ui";
import type { Entry } from "./useConversation";

/**
 * The conversation so far. Every assistant line comes from an API answer (messages,
 * confirmation, question, fallback); nothing is composed here. The customer's bubble shows
 * what they sent, except a blocked message, which is never shown back.
 */
export function Transcript({
  entries,
  pending,
  notice,
  onViewChecklist,
}: {
  entries: Entry[];
  pending: boolean;
  /** The dated-document notice, shown at the foot on narrow screens. */
  notice?: string | undefined;
  onViewChecklist: () => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [entries.length, pending]);
  const lastAssistant = [...entries].reverse().find((e) => e.role === "assistant");
  // The first checklist is "ready"; later checklist answers say it was updated.
  const firstChecklist = entries.find(
    (e) => e.role === "assistant" && e.answer.kind === "checklist",
  );
  return (
    <div className="flex flex-col gap-5 px-4 py-5 sm:px-6">
      <ol aria-label="Conversation" className="flex flex-col gap-5">
        {entries.map((entry) =>
          entry.role === "customer" ? (
            <li key={entry.id} className="oa-rise flex justify-end">
              {entry.blocked ? (
                <p className="max-w-[85%] rounded-2xl rounded-br-md border border-dashed border-line-strong px-4 py-2.5 text-sm italic text-subtle">
                  <span className="sr-only">You: </span>
                  {TEXT.messageNotSent}
                </p>
              ) : (
                <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-gradient-to-br from-cyan/20 to-violet/15 px-4 py-2.5 text-[0.94rem] leading-relaxed text-fg ring-1 ring-cyan/25">
                  <span className="sr-only">You: </span>
                  {entry.text}
                </p>
              )}
            </li>
          ) : (
            <li key={entry.id} className="oa-rise flex gap-3">
              <span
                aria-hidden="true"
                className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-raised text-cyan ring-1 ring-line"
              >
                <Icon name="spark" className="h-3.5 w-3.5" />
              </span>
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
          <li className="flex items-center gap-3" aria-hidden="true">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-raised text-cyan ring-1 ring-line">
              <Icon name="spark" className="h-3.5 w-3.5" />
            </span>
            <span className="flex items-center gap-2 text-sm text-subtle">
              <Thinking /> {TEXT.sending}
            </span>
          </li>
        )}
      </ol>
      {notice && (
        <p className="border-t border-line pt-4 text-xs leading-relaxed text-subtle lg:hidden">
          <span className="font-code uppercase tracking-[0.14em]">{TEXT.aboutAnswers}: </span>
          {notice}
        </p>
      )}
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
  const blocked = other.filter((m) => m.code === "blocked");
  const plain = other.filter((m) => m.code !== "blocked");
  return (
    <div className="flex min-w-0 max-w-[92%] flex-col gap-2 sm:max-w-[85%]">
      <span className="sr-only">Assistant: </span>
      {privacy.map((m, i) => (
        <p
          key={`p${i}`}
          className="flex gap-2.5 rounded-2xl rounded-tl-md border border-warn/25 bg-warn/[0.07] px-4 py-3 text-sm leading-relaxed text-muted"
        >
          <Icon name="shield" className="mt-0.5 h-4 w-4 text-warn" />
          <span>{m.text}</span>
        </p>
      ))}
      {blocked.map((m, i) => (
        <p
          key={`b${i}`}
          className="flex gap-2.5 rounded-2xl rounded-tl-md border border-warn/30 bg-warn/[0.08] px-4 py-3 text-sm font-medium leading-relaxed text-warn"
        >
          <Icon name="alert" className="mt-0.5 h-4 w-4" />
          <span>{m.text}</span>
        </p>
      ))}
      {(plain.length > 0 ||
        answer.fallback ||
        answer.confirmation ||
        answer.question ||
        answer.kind === "checklist") && (
        <div className="rounded-2xl rounded-tl-md border border-line bg-raised/60 px-4 py-3 text-[0.94rem] leading-relaxed text-fg">
          {plain.map((m, i) => (
            <p key={`m${i}`} className="mb-2 text-muted last:mb-0">
              {m.text}
            </p>
          ))}
          {answer.fallback && (
            <p className="mb-2 flex gap-2 font-medium text-fg last:mb-0">
              <Icon name="book" className="mt-1 h-4 w-4 text-violet" />
              <span>{answer.fallback}</span>
            </p>
          )}
          {answer.confirmation && <p className="font-medium">{answer.confirmation.text}</p>}
          {answer.question && <p className="font-medium">{answer.question.text}</p>}
          {answer.kind === "checklist" && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <p className="flex items-center gap-2 font-medium">
                <Icon name="check" className="h-4 w-4 text-ok" />
                {updated ? TEXT.checklistUpdated : TEXT.checklistReady}
              </p>
              {latest && (
                <button
                  type="button"
                  onClick={onViewChecklist}
                  className="inline-flex min-h-9 items-center gap-1 rounded-md text-sm font-semibold text-cyan hover:underline"
                >
                  {TEXT.viewChecklist}
                  <Icon name="arrow" className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
