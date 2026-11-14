/**
 * `/open-account`: the customer-facing account opening assistant (A6). Its own shell, apart
 * from the legacy compliance-engine layout: a header, the persistent prototype disclaimer
 * and one screen at a time (welcome → conversation). The phase is state, not a URL, so the
 * session id never reaches the address bar.
 */
import { ErrorBoundary } from "../components/ErrorBoundary";
import { ConversationScreen } from "./ConversationScreen";
import { DISCLAIMER_LINES, PRODUCT_NAME, TEXT } from "./labels";
import { useConversation } from "./useConversation";
import { EndedScreen, WelcomeScreen } from "./WelcomeScreen";

export function AssistantApp() {
  const conversation = useConversation();
  const { state, restart } = conversation;
  const active = state.phase === "active";
  return (
    <div className="flex min-h-screen flex-col bg-slate-100 text-slate-900">
      <a
        href="#assistant-main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to main content
      </a>
      <header className="bg-slate-900 text-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="grid h-9 w-9 place-items-center rounded-lg bg-sky-500/20 ring-1 ring-sky-300/40"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-5 w-5 fill-none stroke-sky-200"
                strokeWidth="1.8"
              >
                <path d="M9 4h6M8 4h8a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" />
                <path d="m9 12 2 2 4-4" />
              </svg>
            </span>
            <div className="leading-tight">
              <p className="text-base font-semibold tracking-tight">{PRODUCT_NAME}</p>
              <p className="text-xs text-slate-300">Independent prototype</p>
            </div>
          </div>
          {active && (
            <button
              type="button"
              onClick={restart}
              className="rounded-md px-3 py-2 text-sm font-medium text-slate-200 ring-1 ring-slate-600 hover:bg-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-300"
            >
              {TEXT.startAgain}
            </button>
          )}
        </div>
        <aside
          aria-label="Prototype disclaimer"
          className="border-t border-amber-200 bg-amber-50 text-amber-950"
        >
          <p className="mx-auto max-w-6xl px-4 py-2 text-xs sm:px-6 sm:text-sm">
            <strong className="font-semibold">{DISCLAIMER_LINES.headline}</strong>{" "}
            {DISCLAIMER_LINES.detail}
          </p>
        </aside>
      </header>
      <main
        id="assistant-main"
        tabIndex={-1}
        className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 sm:py-8"
      >
        <ErrorBoundary resetKey={state.phase}>
          {state.phase === "active" ? (
            <ConversationScreen conversation={conversation} />
          ) : state.phase === "ended" ? (
            <EndedScreen reason={state.ended} onStart={restart} />
          ) : (
            <WelcomeScreen
              phase={state.phase}
              error={state.startError}
              onStart={() => void conversation.start()}
            />
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
