import { useEffect, useRef, useState, type ReactNode } from "react";

import type { AssistantAnswer, AssistantTurnRequest } from "../api/types";
import { ConfirmCard, QuestionCard, ScenarioCard, SuggestionCard } from "./AnswerPanel";
import { ChecklistView } from "./ChecklistView";
import { Composer } from "./Composer";
import { TEXT } from "./labels";
import { ProfilePanel, ProgressSteps } from "./ProfilePanel";
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

/** How many choices the current answer offers as buttons, or null when it offers none. */
export function choiceCount(answer: AssistantAnswer | null): number | null {
  if (answer === null) return null;
  if (answer.scenario_offer) return answer.scenario_offer.options.length;
  if (answer.confirmation) return 2;
  if (answer.question) return answer.question.chips.length;
  const n = (answer.suggestions ?? []).length;
  return n > 0 ? n : null;
}

/** "Current account · A limited liability partnership (LLP)": the account being discussed,
 * from the API's profile labels; a neutral title before anything is known. */
export function subjectOf(answer: AssistantAnswer): string {
  const labels = ["account_type", "customer_category"]
    .map((slot) => answer.profile[slot])
    .filter((p) => p?.status === "known" && p.label)
    .map((p) => p!.label!);
  return labels.length > 0 ? labels.join(" · ") : TEXT.newConversation;
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
  // The choices for the current answer can be folded away to give the conversation room;
  // a new answer shows its choices again.
  const [collapsedAt, setCollapsedAt] = useState<number | null>(null);
  const collapsed = collapsedAt === state.entries.length && state.failure === null;
  const choices = choiceCount(current);

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
    <div className="flex h-full w-full flex-col">
      <h1 className="sr-only">Your account opening conversation</h1>
      <p aria-live="polite" className="sr-only">
        {state.pending ? "" : announcement(state.latest)}
      </p>

      {/* One quiet line: the account being discussed, the checklist, your answers. */}
      {current !== null && (
        <div className="border-b border-line" data-print="hide">
          <div className="mx-auto flex min-h-12 max-w-3xl items-center gap-2 px-4 sm:px-6">
            <p className="min-w-0 flex-1 truncate text-sm text-muted">{subjectOf(current)}</p>
            {withChecklist !== null && (
              <button
                type="button"
                aria-pressed={view === "checklist"}
                onClick={() => (view === "checklist" ? setView("conversation") : openChecklist())}
                className={`inline-flex min-h-10 items-center gap-2 rounded-md px-3 text-sm font-medium transition-colors ${
                  view === "checklist"
                    ? "bg-raised text-fg"
                    : "text-muted hover:bg-raised hover:text-fg"
                }`}
              >
                {TEXT.checklistTab}{" "}
                <span className="text-xs tabular-nums text-subtle">{itemCount(withChecklist)}</span>
              </button>
            )}
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              aria-haspopup="dialog"
              aria-label={TEXT.answersButton}
              className="inline-flex min-h-10 min-w-10 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium text-muted transition-colors hover:bg-raised hover:text-fg"
            >
              <Icon name="user" className="h-4 w-4" />
              <span className="hidden sm:inline">{TEXT.answersButton}</span>
            </button>
          </div>
        </div>
      )}

      {/* The conversation: the main element. */}
      <section
        aria-label="Conversation with the assistant"
        className={`flex min-h-0 flex-1 flex-col overflow-hidden ${
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
        {choices !== null && (
          <div className="mx-auto flex w-full max-w-3xl justify-end border-t border-line px-4 pt-1.5 sm:px-6">
            <button
              type="button"
              aria-expanded={!collapsed}
              aria-controls="answer-choices"
              onClick={() => setCollapsedAt(collapsed ? null : state.entries.length)}
              className="-mr-2 inline-flex min-h-9 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-subtle hover:text-fg"
            >
              <Icon
                name="chevron"
                className={`h-3.5 w-3.5 transition-transform ${collapsed ? "-rotate-90" : "rotate-90"}`}
              />
              {collapsed ? `${TEXT.showOptions} (${choices})` : TEXT.hideOptions}
            </button>
          </div>
        )}
        <div
          id="answer-choices"
          hidden={collapsed}
          className={`oa-scroll mx-auto grid max-h-[42%] w-full max-w-3xl gap-3 overflow-y-auto px-4 pb-3 empty:hidden sm:px-6 ${
            choices !== null ? "pt-1" : "border-t border-line pt-3"
          }`}
        >
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
        <div
          className={`oa-scroll min-h-0 min-w-0 flex-1 overflow-y-auto ${view === "checklist" ? "" : "hidden"} print:!block print:overflow-visible`}
        >
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
      <h3 id="other-accounts" className="text-xs font-medium text-subtle">
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
              className="flex min-h-11 w-full items-center gap-2 rounded-md border border-line px-3 py-2 text-left text-sm hover:bg-raised disabled:opacity-50"
            >
              <Icon name="arrow" className="h-4 w-4 rotate-180 text-muted" />
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
      <div className="oa-fade absolute inset-0 bg-void/80" onClick={onClose} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="answers-drawer-title"
        className="oa-rise oa-scroll absolute inset-x-0 bottom-0 grid max-h-[85dvh] content-start gap-5 overflow-y-auto rounded-t-lg border-t border-line bg-surface p-5 pb-8 lg:inset-y-0 lg:left-auto lg:right-0 lg:max-h-none lg:w-[24rem] lg:rounded-none lg:border-l lg:border-t-0"
      >
        <div className="flex items-center justify-between">
          <h2 id="answers-drawer-title" className="text-base font-semibold tracking-tight">
            {TEXT.answersButton}
          </h2>
          <button
            ref={closeButton}
            type="button"
            onClick={onClose}
            aria-label={TEXT.close}
            className="-mr-2 grid h-11 w-11 place-items-center rounded-md text-muted hover:bg-raised hover:text-fg"
          >
            <Icon name="x" className="h-5 w-5" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
