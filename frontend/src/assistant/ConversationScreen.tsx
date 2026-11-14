import { useRef } from "react";

import type { AssistantAnswer, AssistantTurnRequest } from "../api/types";
import { ConfirmCard, QuestionCard } from "./AnswerPanel";
import { ChecklistView } from "./ChecklistView";
import { Composer } from "./Composer";
import { TEXT } from "./labels";
import { ProfilePanel, ProgressSteps } from "./ProfilePanel";
import { StatusNotice } from "./StatusNotice";
import { Transcript } from "./Transcript";
import type { useConversation } from "./useConversation";

type Conversation = ReturnType<typeof useConversation>;

/** The answer that defines what can be done now. A blocked message changes nothing on the
 * server, so the answer before it still applies (its question, its checklist). */
export function currentAnswer(entries: Conversation["state"]["entries"]): AssistantAnswer | null {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i]!;
    if (e.role === "assistant" && e.answer.kind !== "blocked") return e.answer;
  }
  return null;
}

/** What a screen reader hears when an answer arrives (the same API text as the bubble). */
export function announcement(answer: AssistantAnswer | null): string {
  if (answer === null) return "";
  const parts = (answer.messages ?? []).map((m) => m.text);
  if (answer.fallback) parts.push(answer.fallback);
  if (answer.confirmation) parts.push(answer.confirmation.text);
  if (answer.question) parts.push(answer.question.text);
  if (answer.kind === "checklist") parts.push(TEXT.checklistHeading);
  return parts.join(" ");
}

export function ConversationScreen({ conversation }: { conversation: Conversation }) {
  const { state, send, retryLast, dismiss } = conversation;
  const checklistHeading = useRef<HTMLHeadingElement>(null);
  const current = currentAnswer(state.entries);
  const busy = state.pending;

  const sendBody = (b: AssistantTurnRequest) => void send(b);

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start">
      <h1 className="sr-only">Your account opening conversation</h1>
      <p aria-live="polite" className="sr-only">
        {state.pending ? "" : announcement(state.latest)}
      </p>
      {current !== null && (
        <div className="rounded-2xl bg-white p-3 shadow-sm ring-1 ring-slate-200 sm:p-4 lg:col-start-2 lg:row-start-1">
          <ProgressSteps answer={current} />
        </div>
      )}
      <div className="grid min-w-0 gap-6 lg:col-start-1 lg:row-span-2 lg:row-start-1">
        <section
          aria-label="Conversation with the assistant"
          className="flex flex-col overflow-hidden rounded-2xl bg-white shadow-sm ring-1 ring-slate-200"
        >
          <div className="max-h-[min(62vh,640px)] min-h-64 overflow-y-auto bg-slate-50/60">
            <Transcript
              entries={state.entries}
              pending={state.pending}
              onViewChecklist={() => checklistHeading.current?.focus()}
            />
          </div>
          <div className="grid gap-3 border-t border-slate-200 p-3 empty:hidden sm:p-4">
            {state.failure !== null && (
              <StatusNotice
                failure={state.failure}
                disabled={busy}
                onRetry={() => void retryLast()}
                onDismiss={dismiss}
              />
            )}
            {current?.confirmation ? (
              <ConfirmCard
                key={`c${state.entries.length}`}
                confirmation={current.confirmation}
                disabled={busy}
                onSend={sendBody}
              />
            ) : current?.question ? (
              <QuestionCard
                key={`q${state.entries.length}`}
                question={current.question}
                disabled={busy}
                onSend={sendBody}
              />
            ) : null}
          </div>
          <Composer disabled={busy} onSend={(message) => void send({ message })} />
        </section>
      </div>
      {/* Mobile order: progress, conversation, your answers, checklist. Desktop: the
          answers stay beside the conversation and the checklist runs below it. */}
      {current !== null && (
        <div className="grid gap-6 lg:sticky lg:top-4 lg:col-start-2 lg:row-span-2 lg:row-start-2 lg:self-start">
          <ProfilePanel answer={current} disabled={busy} onSend={sendBody} />
          {!current.checklist && (
            <aside
              aria-label={TEXT.aboutAnswers}
              className="rounded-2xl bg-white p-4 text-sm text-slate-600 ring-1 ring-slate-200"
            >
              <h2 className="font-semibold text-slate-800">{TEXT.aboutAnswers}</h2>
              <p className="mt-1 leading-relaxed">{current.notice}</p>
            </aside>
          )}
        </div>
      )}
      {current?.checklist && (
        <div className="min-w-0 lg:col-start-1 lg:row-start-3">
          <ChecklistView
            checklist={current.checklist}
            notice={current.notice}
            headingRef={checklistHeading}
          />
        </div>
      )}
    </div>
  );
}
