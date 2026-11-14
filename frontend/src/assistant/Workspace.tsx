import { useEffect, useRef, useState, type ReactNode } from "react";

import type { AssistantAnswer, AssistantTurnRequest } from "../api/types";
import { ConfirmCard, QuestionCard, ScenarioCard, SuggestionCard } from "./AnswerPanel";
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
type Entries = Conversation["state"]["entries"];

/** The answer that defines what can be done now. A blocked message changes nothing on the
 * server, so the answer before it still applies (its question, its checklist). */
export function currentAnswer(entries: Entries): AssistantAnswer | null {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i]!;
    if (e.role === "assistant" && e.answer.kind !== "blocked") return e.answer;
  }
  return null;
}

function sameProfile(a: AssistantAnswer, b: AssistantAnswer): boolean {
  const keys = new Set([...Object.keys(a.profile), ...Object.keys(b.profile)]);
  for (const k of keys) {
    const x = a.profile[k];
    const y = b.profile[k];
    if ((x?.status ?? "unknown") !== (y?.status ?? "unknown")) return false;
    if ((x?.value ?? null) !== (y?.value ?? null)) return false;
  }
  return true;
}

/** The checklist that still applies: the current answer's, or (while a follow-up or a
 * clarification is going on) the latest one built for exactly the same answers. A changed
 * answer makes an older checklist stale, so it is no longer shown. */
export function applicableChecklist(entries: Entries): AssistantAnswer | null {
  const current = currentAnswer(entries);
  if (current === null) return null;
  if (current.checklist) return current;
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i]!;
    if (e.role === "assistant" && e.answer.checklist) {
      return sameProfile(e.answer, current) ? e.answer : null;
    }
  }
  return null;
}

/** What a screen reader hears when an answer arrives (the same API text as the bubble). */
export function announcement(answer: AssistantAnswer | null): string {
  if (answer === null) return "";
  if (answer.reply) {
    return [answer.reply, answer.fallback, answer.explanation].filter(Boolean).join(" ");
  }
  const parts = (answer.messages ?? []).map((m) => m.text);
  if (answer.fallback) parts.push(answer.fallback);
  if (answer.confirmation) parts.push(answer.confirmation.text);
  if (answer.scenario_offer) parts.push(answer.scenario_offer.text);
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
  const withChecklist = applicableChecklist(state.entries);
  const busy = state.pending;
  const [view, setView] = useState<View>("conversation");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sendBody = (b: AssistantTurnRequest) => void send(b);

  // A checklist that no longer applies takes its view with it.
  const [hadChecklist, setHadChecklist] = useState(withChecklist !== null);
  if (hadChecklist !== (withChecklist !== null)) {
    setHadChecklist(withChecklist !== null);
    if (withChecklist === null) setView("conversation");
  }

  function openChecklist() {
    setView("checklist");
    requestAnimationFrame(() => checklistHeading.current?.focus());
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col px-3 pb-4 pt-3 sm:px-6 sm:pt-5">
      <h1 className="sr-only">Your account opening conversation</h1>
      <p aria-live="polite" className="sr-only">
        {state.pending ? "" : announcement(state.latest)}
      </p>

      {/* One quiet bar: progress, the view switch, your answers. */}
      {current !== null && (
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2" data-print="hide">
          <div className="min-w-0 flex-1 basis-48">
            <ProgressCompact answer={current} />
          </div>
          {withChecklist !== null && (
            <div
              role="group"
              aria-label="Show"
              className="flex rounded-xl bg-surface p-1 ring-1 ring-line"
            >
              {(["conversation", "checklist"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  aria-pressed={view === v}
                  onClick={() => (v === "checklist" ? openChecklist() : setView(v))}
                  className={`flex min-h-9 items-center gap-1.5 rounded-lg px-3 text-sm font-medium transition-colors ${
                    view === v ? "bg-raised text-fg" : "text-muted hover:text-fg"
                  }`}
                >
                  {v === "checklist" ? TEXT.checklistTab : TEXT.conversationTab}
                  {v === "checklist" && (
                    <span className="font-code text-[0.7rem] text-cyan">
                      {itemCount(withChecklist)}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-haspopup="dialog"
            aria-label={TEXT.answersButton}
            className="inline-flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-xl px-3 text-sm font-medium text-muted ring-1 ring-line hover:bg-surface hover:text-fg"
          >
            <Icon name="user" className="h-4 w-4 text-cyan" />
            <span className="hidden sm:inline">{TEXT.answersButton}</span>
          </button>
        </div>
      )}

      {/* The conversation: the main element. */}
      <section
        aria-label="Conversation with the assistant"
        className={`flex h-[calc(100dvh-14rem)] min-h-[28rem] flex-col overflow-hidden rounded-2xl bg-surface/70 ring-1 ring-line/70 ${
          view === "conversation" ? "" : "hidden"
        }`}
        data-print="hide"
      >
        <div className="oa-scroll min-h-0 flex-1 overflow-y-auto">
          <Transcript
            entries={state.entries}
            pending={state.pending}
            notice={current?.notice}
            hasChecklist={withChecklist !== null}
            onViewChecklist={openChecklist}
          />
        </div>
        <div className="oa-scroll grid max-h-[46%] gap-3 overflow-y-auto border-t border-line/70 p-3 empty:hidden sm:p-4">
          {state.failure !== null && (
            <StatusNotice
              failure={state.failure}
              disabled={busy}
              onRetry={() => void retryLast()}
              onDismiss={dismiss}
            />
          )}
          {current?.scenario_offer ? (
            <ScenarioCard
              key={`o${state.entries.length}`}
              offer={current.scenario_offer}
              disabled={busy}
              onSend={sendBody}
            />
          ) : current?.confirmation ? (
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

      {/* The checklist: its own view (printed on its own). */}
      {withChecklist?.checklist && (
        <div className={`min-w-0 ${view === "checklist" ? "" : "hidden"} print:!block`}>
          <ChecklistView
            checklist={withChecklist.checklist}
            notice={withChecklist.notice}
            headingRef={checklistHeading}
            onBack={() => setView("conversation")}
          />
        </div>
      )}

      {drawerOpen && current !== null && (
        <AnswersDrawer onClose={() => setDrawerOpen(false)}>
          <OtherAccounts
            answer={current}
            disabled={busy}
            onSwitch={(index) => {
              setDrawerOpen(false);
              setView("conversation");
              sendBody({ action: "switch_scenario", value: String(index) } as AssistantTurnRequest);
            }}
          />
          <ProgressSteps answer={current} />
          <ProfilePanel
            answer={current}
            disabled={busy}
            onSend={(b) => {
              setDrawerOpen(false);
              setView("conversation");
              sendBody(b);
            }}
          />
          <p className="text-xs leading-relaxed text-subtle">{current.notice}</p>
        </AnswersDrawer>
      )}
    </div>
  );
}

/** The other accounts discussed in this conversation (parked by the API, each with its own
 * answers). Going back to one is an ordinary `switch_scenario` turn. */
export function OtherAccounts({
  answer,
  disabled,
  onSwitch,
}: {
  answer: AssistantAnswer;
  disabled: boolean;
  onSwitch: (index: number) => void;
}) {
  const others = answer.scenarios ?? [];
  if (others.length === 0) return null;
  return (
    <section aria-labelledby="other-accounts" className="grid gap-2">
      {answer.active_scenario && (
        <p className="text-sm text-muted">
          {TEXT.nowDiscussing}:{" "}
          <span className="font-medium text-fg">{answer.active_scenario}</span>
        </p>
      )}
      <h3 id="other-accounts" className="text-xs font-medium uppercase tracking-wide text-subtle">
        {TEXT.otherAccounts}
      </h3>
      <ul className="grid gap-2">
        {others.map((s) => (
          <li key={s.index}>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onSwitch(s.index)}
              aria-label={`${TEXT.goBackTo} ${s.label}`}
              className="flex min-h-11 w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm ring-1 ring-line hover:bg-raised disabled:opacity-50"
            >
              <Icon name="arrow" className="h-4 w-4 rotate-180 text-cyan" />
              <span>{s.label}</span>
            </button>
          </li>
        ))}
      </ul>
      <p className="text-xs text-subtle">{TEXT.otherAccountsHint}</p>
    </section>
  );
}

/** Your answers: a side panel on wide screens, a bottom sheet on narrow ones. */
function AnswersDrawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
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
    <div className="fixed inset-0 z-40" data-print="hide">
      <div
        className="oa-fade absolute inset-0 bg-void/70 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="answers-drawer-title"
        className="oa-rise oa-scroll absolute inset-x-0 bottom-0 grid max-h-[85dvh] content-start gap-5 overflow-y-auto rounded-t-3xl border-t border-line bg-surface p-5 pb-8 lg:inset-y-0 lg:left-auto lg:right-0 lg:max-h-none lg:w-[24rem] lg:rounded-none lg:border-l lg:border-t-0"
      >
        <div className="flex items-center justify-between">
          <h2 id="answers-drawer-title" className="font-display text-lg font-semibold">
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
