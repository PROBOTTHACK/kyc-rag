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
  hasChecklist = true,
  onViewChecklist,
}: {
  entries: Entry[];
  pending: boolean;
  /** The dated-document notice, shown at the foot of the conversation. */
  notice?: string | undefined;
  /** A checklist applies to the current answers (the latest reply can open it). */
  hasChecklist?: boolean;
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
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-6 sm:px-6 sm:py-8">
      <ol aria-label="Conversation" className="flex flex-col gap-7">
        {entries.map((entry) =>
          entry.role === "customer" ? (
            <li key={entry.id} className="oa-rise flex justify-end">
              {entry.blocked ? (
                <p className="max-w-[80%] rounded-md border border-dashed border-line-strong px-3 py-2 text-sm italic text-subtle">
                  <span className="sr-only">You: </span>
                  {TEXT.messageNotSent}
                </p>
              ) : (
                <p className="max-w-[80%] whitespace-pre-wrap rounded-md bg-raised px-3 py-2 text-[0.94rem] leading-relaxed text-fg">
                  <span className="sr-only">You: </span>
                  {entry.text}
                </p>
              )}
            </li>
          ) : (
            <li key={entry.id} className="oa-rise">
              <AssistantBubble
                answer={entry.answer}
                latest={entry === lastAssistant}
                updated={entry !== firstChecklist}
                hasChecklist={hasChecklist}
                onViewChecklist={onViewChecklist}
              />
            </li>
          ),
        )}
        {pending && (
          <li aria-hidden="true">
            <Thinking />
          </li>
        )}
      </ol>
      {notice && (
        <p className="border-t border-line pt-4 text-xs leading-relaxed text-subtle">
          <span className="font-medium text-muted">{TEXT.aboutAnswers}: </span>
          {notice}
        </p>
      )}
      <div ref={end} />
    </div>
  );
}

/** The assistant's reply: paragraphs, with "- " lines rendered as a list. */
export function ReplyText({ text }: { text: string }) {
  const blocks: Array<{ kind: "p"; text: string } | { kind: "ul"; items: string[] }> = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const bullet = /^[-•*]\s+(.*)$/.exec(line);
    const last = blocks[blocks.length - 1];
    if (bullet) {
      if (last?.kind === "ul") last.items.push(bullet[1]!);
      else blocks.push({ kind: "ul", items: [bullet[1]!] });
    } else {
      blocks.push({ kind: "p", text: line });
    }
  }
  return (
    <div className="grid gap-2.5">
      {blocks.map((b, i) =>
        b.kind === "p" ? (
          <p key={i}>{b.text}</p>
        ) : (
          <ul key={i} className="grid gap-1">
            {b.items.map((item, j) => (
              <li
                key={j}
                className="flex gap-2 before:shrink-0 before:text-subtle before:content-['–']"
              >
                <span>{item}</span>
              </li>
            ))}
          </ul>
        ),
      )}
    </div>
  );
}

function AssistantBubble({
  answer,
  latest,
  updated,
  hasChecklist,
  onViewChecklist,
}: {
  answer: AssistantAnswer;
  latest: boolean;
  updated: boolean;
  hasChecklist: boolean;
  onViewChecklist: () => void;
}) {
  const messages = answer.messages ?? [];
  const privacy = messages.filter((m) => m.code === "privacy");
  const other = messages.filter((m) => m.code !== "privacy");
  const blocked = other.filter((m) => m.code === "blocked");
  const plain = other.filter((m) => m.code !== "blocked");
  const reply = answer.reply ?? null;
  const viewButton = answer.kind === "checklist" && latest && hasChecklist && (
    <button
      type="button"
      onClick={onViewChecklist}
      className="-ml-2 inline-flex min-h-10 items-center gap-1.5 rounded-md px-2 text-sm font-medium text-accent underline-offset-4 hover:text-accent-strong hover:underline"
    >
      {TEXT.viewChecklist}
      <Icon name="arrow" className="h-3.5 w-3.5" />
    </button>
  );
  // A greeting or thanks changes nothing: the fixed texts already shown are not repeated.
  const social = messages.some((m) => m.code === "social");
  const fixed = !social && (answer.fallback || answer.explanation) && (
    <div className={`border-l-2 border-line-strong pl-4 text-sm ${reply ? "mt-4" : ""}`}>
      {answer.fallback && <p className="mb-2 font-medium text-fg last:mb-0">{answer.fallback}</p>}
      {answer.explanation && (
        <p className="mb-2 text-sm leading-relaxed text-muted last:mb-0">{answer.explanation}</p>
      )}
    </div>
  );
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <span className="sr-only">Assistant: </span>
      {privacy.map((m, i) => (
        <p
          key={`p${i}`}
          className="border-l-2 border-warn/70 pl-4 text-sm leading-relaxed text-muted"
        >
          {m.text}
        </p>
      ))}
      {blocked.map((m, i) => (
        <p
          key={`b${i}`}
          className="border-l-2 border-warn pl-4 text-sm font-medium leading-relaxed text-warn"
        >
          {m.text}
        </p>
      ))}
      {reply ? (
        <div data-reply className="text-[0.97rem] leading-[1.7] text-fg">
          <ReplyText text={reply} />
          {fixed}
          {viewButton && <div className="mt-3">{viewButton}</div>}
        </div>
      ) : (
        (plain.length > 0 ||
          answer.fallback ||
          answer.confirmation ||
          answer.scenario_offer ||
          answer.question ||
          answer.kind === "checklist") && (
          // An answer stored before replies existed: the earlier rendering.
          <div data-reply className="text-[0.97rem] leading-[1.7] text-fg">
            {plain.map((m, i) => (
              <p key={`m${i}`} className="mb-2 text-muted last:mb-0">
                {m.text}
              </p>
            ))}
            {fixed}
            {answer.confirmation && <p className="font-medium">{answer.confirmation.text}</p>}
            {answer.scenario_offer && <p className="font-medium">{answer.scenario_offer.text}</p>}
            {answer.question && <p className="font-medium">{answer.question.text}</p>}
            {answer.kind === "checklist" && (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <p className="flex items-center gap-2 font-medium">
                  <Icon name="check" className="h-4 w-4 text-ok" />
                  {updated ? TEXT.checklistUpdated : TEXT.checklistReady}
                </p>
                {viewButton}
              </div>
            )}
          </div>
        )
      )}
    </div>
  );
}
