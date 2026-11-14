import { useEffect, useRef, useState } from "react";

import type { AssistantAnswer, AssistantTurnRequest } from "../api/types";
import { ConfirmCard, QuestionCard, SuggestionCard } from "./AnswerPanel";
import { ChecklistView } from "./ChecklistView";
import { Composer } from "./Composer";
import { TEXT } from "./labels";
import { ProfilePanel, ProgressCompact, ProgressSteps } from "./ProfilePanel";
import { StatusNotice } from "./StatusNotice";
import { Transcript } from "./Transcript";
import { Icon } from "./ui";
import type { useConversation } from "./useConversation";

type Conversation = ReturnType<typeof useConversation>;
type View = "conversation" | "checklist";

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

function itemCount(answer: AssistantAnswer | null): number {
  const g = answer?.checklist?.groups;
  return g ? g.documents.length + g.information.length + g.not_confirmed.length : 0;
}

export function Workspace({ conversation }: { conversation: Conversation }) {
  const { state, send, retryLast, dismiss } = conversation;
  const checklistHeading = useRef<HTMLHeadingElement>(null);
  const current = currentAnswer(state.entries);
  const busy = state.pending;
  const hasChecklist = Boolean(current?.checklist);
  const [view, setView] = useState<View>("conversation");
  const [sheetOpen, setSheetOpen] = useState(false);
  const sendBody = (b: AssistantTurnRequest) => void send(b);

  // The first checklist takes the stage on narrow screens; on wide screens both are visible.
  // (State adjusted during render when the checklist appears or goes, not in an effect.)
  const [hadChecklist, setHadChecklist] = useState(hasChecklist);
  if (hadChecklist !== hasChecklist) {
    setHadChecklist(hasChecklist);
    setView(hasChecklist ? "checklist" : "conversation");
  }

  function openChecklist() {
    setView("checklist");
    requestAnimationFrame(() => checklistHeading.current?.focus());
  }

  const showConversation = !hasChecklist || view === "conversation";

  return (
    <div className="mx-auto max-w-[1440px] px-3 py-4 sm:px-6 sm:py-6">
      <h1 className="sr-only">Your account opening workspace</h1>
      <p aria-live="polite" className="sr-only">
        {state.pending ? "" : announcement(state.latest)}
      </p>

      {/* Narrow screens: compact progress and access to your answers. */}
      {current !== null && (
        <div className="mb-3 flex items-center gap-3 lg:hidden" data-print="hide">
          <div className="min-w-0 flex-1">
            <ProgressCompact answer={current} />
          </div>
          <button
            type="button"
            onClick={() => setSheetOpen(true)}
            aria-haspopup="dialog"
            className="inline-flex min-h-11 shrink-0 items-center gap-2 rounded-xl border border-line bg-surface px-3 text-sm font-medium text-fg hover:bg-raised"
          >
            <Icon name="user" className="h-4 w-4 text-cyan" />
            {TEXT.answersButton}
          </button>
        </div>
      )}

      {hasChecklist && (
        <div
          role="group"
          aria-label="Show"
          className="mb-3 grid grid-cols-2 gap-1 rounded-xl border border-line bg-surface p-1 xl:hidden"
          data-print="hide"
        >
          {(["conversation", "checklist"] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={view === v}
              onClick={() => (v === "checklist" ? openChecklist() : setView(v))}
              className={`flex min-h-10 items-center justify-center gap-2 rounded-lg text-sm font-medium transition-colors ${
                view === v
                  ? "bg-raised text-fg ring-1 ring-line-strong"
                  : "text-muted hover:text-fg"
              }`}
            >
              <Icon name={v === "checklist" ? "document" : "spark"} className="h-4 w-4" />
              {v === "checklist" ? TEXT.checklistTab : TEXT.conversationTab}
              {v === "checklist" && (
                <span className="rounded-md bg-cyan/15 px-1.5 font-code text-[0.7rem] text-cyan">
                  {itemCount(current)}
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      <div
        className={`grid gap-4 print:block lg:gap-6 ${
          hasChecklist
            ? "lg:grid-cols-[16.5rem_minmax(0,1fr)] xl:grid-cols-[16.5rem_minmax(0,25rem)_minmax(0,1fr)]"
            : "lg:grid-cols-[16.5rem_minmax(0,1fr)]"
        }`}
      >
        {/* Rail: progress and your answers (desktop). */}
        {current !== null && (
          <aside
            aria-label="Progress and your answers"
            className="hidden gap-4 self-start lg:sticky lg:top-[5.5rem] lg:grid"
            data-print="hide"
          >
            <div className="rounded-2xl border border-line bg-surface p-4">
              <ProgressSteps answer={current} />
            </div>
            <ProfilePanel answer={current} disabled={busy} onSend={sendBody} />
            {current.notice && (
              <aside
                aria-label={TEXT.aboutAnswers}
                className={`rounded-2xl border border-line bg-surface/60 p-4 text-xs leading-relaxed text-muted ${
                  hasChecklist ? "xl:hidden" : ""
                }`}
              >
                <h2 className="font-code text-[0.65rem] uppercase tracking-[0.16em] text-subtle">
                  {TEXT.aboutAnswers}
                </h2>
                <p className="mt-2">{current.notice}</p>
              </aside>
            )}
          </aside>
        )}

        {/* The conversation. */}
        <section
          aria-label="Conversation with the assistant"
          className={`flex h-[calc(100dvh-15rem)] min-h-[30rem] flex-col overflow-hidden rounded-2xl border border-line bg-surface lg:sticky lg:top-[5.5rem] lg:h-[calc(100dvh-8.5rem)] ${
            showConversation ? "" : "max-xl:hidden"
          } ${hasChecklist ? "" : "mx-auto w-full max-w-3xl lg:max-w-none"}`}
          data-print="hide"
        >
          <div className="oa-scroll min-h-0 flex-1 overflow-y-auto">
            <Transcript
              entries={state.entries}
              pending={state.pending}
              notice={current?.notice}
              onViewChecklist={openChecklist}
            />
          </div>
          <div className="oa-scroll grid max-h-[46%] gap-3 overflow-y-auto border-t border-line p-3 empty:hidden sm:p-4">
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
            ) : (current?.suggestions ?? []).length > 0 ? (
              <SuggestionCard
                key={`s${state.entries.length}`}
                suggestions={current!.suggestions!}
                disabled={busy}
                onSend={sendBody}
              />
            ) : null}
          </div>
          <Composer disabled={busy} onSend={(message) => void send({ message })} />
        </section>

        {/* The checklist: the document workspace. */}
        {current?.checklist && (
          <div
            className={`min-w-0 ${view === "checklist" ? "" : "max-xl:hidden"} print:!block lg:col-start-2 xl:col-start-3`}
          >
            <ChecklistView
              checklist={current.checklist}
              notice={current.notice}
              headingRef={checklistHeading}
            />
          </div>
        )}
      </div>

      {sheetOpen && current !== null && (
        <AnswersSheet onClose={() => setSheetOpen(false)}>
          <ProgressSteps answer={current} />
          <ProfilePanel
            answer={current}
            disabled={busy}
            onSend={(b) => {
              setSheetOpen(false);
              sendBody(b);
            }}
          />
        </AnswersSheet>
      )}
    </div>
  );
}

/** A bottom sheet for narrow screens: your answers without leaving the conversation. */
function AnswersSheet({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(null);
  useEffect(() => {
    opener.current = document.activeElement;
    closeButton.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 lg:hidden" data-print="hide">
      <div
        className="oa-fade absolute inset-0 bg-void/70 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="answers-sheet-title"
        className="oa-rise oa-scroll absolute inset-x-0 bottom-0 grid max-h-[85dvh] gap-4 overflow-y-auto rounded-t-3xl border-t border-line bg-surface p-4 pb-8"
      >
        <div className="flex items-center justify-between">
          <h2 id="answers-sheet-title" className="font-display text-lg font-semibold">
            {TEXT.answersButton}
          </h2>
          <button
            ref={closeButton}
            type="button"
            onClick={onClose}
            aria-label={TEXT.close}
            className="grid h-11 w-11 place-items-center rounded-xl text-muted hover:bg-raised hover:text-fg"
          >
            <Icon name="x" className="h-5 w-5" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
