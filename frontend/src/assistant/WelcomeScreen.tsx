import { useRef } from "react";

import type { ApiError } from "../api/errors";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { customerError, ENDED, REAL_DATA_WARNING, TEXT, WELCOME } from "./labels";
import type { Phase } from "./useConversation";
import { Button, Icon, Spinner } from "./ui";

/** The real-data warning: shown before anything can be typed, and kept beside the composer. */
export function RealDataWarning({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-subtle">
        <span aria-hidden="true" className="h-1 w-1 shrink-0 rounded-full bg-warn" />
        {REAL_DATA_WARNING.short}
      </p>
    );
  }
  return (
    <section aria-labelledby="real-data-warning" className="border-l-2 border-warn/70 py-1 pl-4">
      <h2 id="real-data-warning" className="text-sm font-semibold text-fg">
        {REAL_DATA_WARNING.title}
      </h2>
      <p className="mt-1 text-sm leading-relaxed text-muted">{REAL_DATA_WARNING.body}</p>
    </section>
  );
}

export function WelcomeScreen({
  phase,
  error,
  onStart,
}: {
  phase: Phase;
  error: ApiError | null;
  onStart: () => void;
}) {
  const busy = phase === "starting" || phase === "restoring";
  if (phase === "restoring") {
    return (
      <p role="status" className="flex min-h-[60vh] items-center justify-center gap-3 text-muted">
        <Spinner /> {WELCOME.resuming}
      </p>
    );
  }
  return (
    <div className="mx-auto max-w-5xl px-4 py-12 sm:px-6 sm:py-20">
      <section aria-labelledby="welcome-title" className="oa-rise max-w-2xl">
        <p className="text-sm text-subtle">{WELCOME.eyebrow}</p>
        <h1
          id="welcome-title"
          className="mt-4 font-display text-3xl font-semibold leading-tight tracking-tight text-fg sm:text-[2.6rem]"
        >
          {WELCOME.title}
        </h1>
        <p className="mt-5 text-base leading-relaxed text-muted sm:text-[1.05rem]">
          {WELCOME.lead}
        </p>

        <ol className="mt-8 flex flex-wrap gap-x-6 gap-y-2 text-sm text-muted">
          {WELCOME.steps.map((step, i) => (
            <li key={step} className="flex items-baseline gap-2">
              <span className="text-xs tabular-nums text-subtle">{i + 1}.</span> {step}
            </li>
          ))}
        </ol>

        <div className="mt-10">
          <RealDataWarning />
        </div>
        {error !== null && (
          <p role="alert" className="mt-4 border-l-2 border-danger pl-4 text-sm text-danger">
            {customerError(error)}
          </p>
        )}
        <div className="mt-8 flex flex-wrap items-center gap-4">
          <Button variant="primary" onClick={onStart} disabled={busy} className="px-5">
            {busy ? <Spinner /> : <Icon name="arrow" className="order-last h-4 w-4" />}
            {error !== null ? TEXT.tryAgain : WELCOME.start}
          </Button>
          <span className="text-xs text-subtle">No sign-up. Synthetic data only.</span>
        </div>
      </section>

      <ul className="mt-16 grid gap-8 border-t border-line pt-8 sm:grid-cols-3 sm:gap-10">
        {WELCOME.points.map((point) => (
          <li key={point.title}>
            <p className="text-sm font-semibold text-fg">{point.title}</p>
            <p className="mt-1.5 text-sm leading-relaxed text-muted">{point.body}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function EndedScreen({
  reason,
  onStart,
}: {
  reason: "SESSION_EXPIRED" | "SESSION_NOT_FOUND" | null;
  onStart: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  return (
    <div className="grid min-h-[60vh] place-items-center px-4">
      <section className="oa-rise w-full max-w-md text-center">
        <h1
          ref={heading}
          tabIndex={-1}
          className="font-display text-xl font-semibold tracking-tight focus:outline-none"
        >
          This conversation has ended
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-muted">
          {ENDED[reason ?? "SESSION_NOT_FOUND"]}
        </p>
        <Button variant="primary" onClick={onStart} className="mt-6">
          {TEXT.startNew}
        </Button>
      </section>
    </div>
  );
}
