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
    <div className="oa flex min-h-dvh flex-col">
      <a
        href="#assistant-main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-raised focus:px-3 focus:py-2 focus:outline-2 focus:outline-cyan"
      >
        Skip to main content
      </a>
      <header
        className="sticky top-0 z-30 border-b border-line/80 bg-void/85 backdrop-blur-md"
        data-print="hide"
      >
        <div className="mx-auto flex h-16 max-w-[1440px] items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <Mark />
            <div className="leading-tight">
              <p className="font-display text-sm font-semibold tracking-tight sm:text-[0.95rem]">
                {PRODUCT_NAME}
              </p>
              <p className="font-code text-[0.65rem] uppercase tracking-[0.18em] text-subtle">
                Independent prototype
              </p>
            </div>
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
      <aside aria-label="Prototype disclaimer" className="border-b border-warn/15 bg-warn/[0.06]">
        <p className="mx-auto flex max-w-[1440px] items-start gap-2.5 px-4 py-2 text-xs leading-relaxed text-warn/90 sm:px-6 sm:text-[0.8rem]">
          <Icon name="shield" className="mt-px h-4 w-4 text-warn" />
          <span>
            <strong className="font-semibold text-warn">{DISCLAIMER_LINES.headline}</strong>{" "}
            <span className="text-muted">{DISCLAIMER_LINES.detail}</span>
          </span>
        </p>
      </aside>
      <main id="assistant-main" tabIndex={-1} className="relative flex-1 focus:outline-none">
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
