/**
 * The Account Opening Assistant: the main application (served at `/`). Its own shell: the
 * product header, the persistent prototype disclaimer and one screen at a time (welcome →
 * workspace). The phase is state, not a URL, so the session id never reaches the address bar.
 */
import { ErrorBoundary } from "../components/ErrorBoundary";
import { DISCLAIMER_LINES, PRODUCT_NAME, TEXT } from "./labels";
import { Button, Icon, Mark } from "./ui";
import { useConversation } from "./useConversation";
import { EndedScreen, WelcomeScreen } from "./WelcomeScreen";
import { Workspace } from "./Workspace";

export function AssistantApp() {
  const conversation = useConversation();
  const { state, restart } = conversation;
  const active = state.phase === "active";
  return (
    <div className={`oa flex flex-col ${active ? "h-dvh overflow-hidden" : "min-h-dvh"}`}>
      <a
        href="#assistant-main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-raised focus:px-3 focus:py-2 focus:outline-2 focus:outline-accent"
      >
        Skip to main content
      </a>
      <header className="border-b border-line bg-void" data-print="hide">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <Mark />
            <p className="text-[0.9rem] font-semibold tracking-tight text-fg">
              {PRODUCT_NAME}
              <span className="ml-2 hidden font-normal text-subtle sm:inline">
                Independent prototype
              </span>
            </p>
          </div>
          {active && (
            <Button
              variant="ghost"
              onClick={restart}
              className="-mr-2"
              aria-label={TEXT.startAgain}
            >
              <Icon name="refresh" />
              <span className="hidden sm:inline">{TEXT.startAgain}</span>
            </Button>
          )}
        </div>
      </header>
      <aside aria-label="Prototype disclaimer" className="border-b border-line">
        <p className="mx-auto flex max-w-[1440px] items-start gap-2 px-4 py-2 text-xs leading-relaxed text-subtle sm:px-6">
          <span aria-hidden="true" className="mt-[0.4rem] h-1 w-1 shrink-0 rounded-full bg-warn" />
          <span>
            <strong className="font-medium text-muted">{DISCLAIMER_LINES.headline}</strong>{" "}
            {DISCLAIMER_LINES.detail}
          </span>
        </p>
      </aside>
      <main
        id="assistant-main"
        tabIndex={-1}
        className="relative min-h-0 flex-1 focus:outline-none"
      >
        <ErrorBoundary resetKey={state.phase}>
          <div
            key={active ? "active" : state.phase === "ended" ? "ended" : "welcome"}
            className="oa-fade h-full"
          >
            {active ? (
              <Workspace conversation={conversation} />
            ) : state.phase === "ended" ? (
              <EndedScreen reason={state.ended} onStart={restart} />
            ) : (
              <WelcomeScreen
                phase={state.phase}
                error={state.startError}
                onStart={() => void conversation.start()}
              />
            )}
          </div>
        </ErrorBoundary>
      </main>
    </div>
  );
}
