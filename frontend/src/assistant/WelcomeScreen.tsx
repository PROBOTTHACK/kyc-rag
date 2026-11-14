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
      <p className="flex items-center gap-1.5 text-xs text-warn/90">
        <Icon name="alert" className="h-3.5 w-3.5" /> {REAL_DATA_WARNING.short}
      </p>
    );
  }
  return (
    <section
      aria-labelledby="real-data-warning"
      className="rounded-2xl border border-warn/25 bg-warn/[0.07] p-4 sm:p-5"
    >
      <h2
        id="real-data-warning"
        className="flex items-center gap-2 text-sm font-semibold text-warn"
      >
        <Icon name="alert" /> {REAL_DATA_WARNING.title}
      </h2>
      <p className="mt-1.5 text-sm leading-relaxed text-muted">{REAL_DATA_WARNING.body}</p>
    </section>
  );
}

/** Atmosphere: a faint grid and two soft light sources. Decorative only. */
function Atmosphere() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 overflow-hidden"
      data-print="hide"
    >
      <div className="oa-grid absolute inset-0" />
      <div className="oa-drift absolute -top-40 left-[8%] h-[28rem] w-[28rem] rounded-full bg-cyan/10 blur-3xl" />
      <div
        className="oa-drift absolute -right-24 top-24 h-[24rem] w-[24rem] rounded-full bg-violet/12 blur-3xl"
        style={{ animationDelay: "-4s" }}
      />
    </div>
  );
}

/** A schematic of the result (bars, not text): what the assistant produces. Decorative. */
function Preview() {
  const rows = [
    ["w-3/5", "w-1/4", "cyan"],
    ["w-1/2", "w-1/5", "cyan"],
    ["w-2/3", "w-1/4", "violet"],
    ["w-2/5", "w-1/5", "violet"],
  ] as const;
  return (
    <div aria-hidden="true" className="relative hidden lg:block">
      <div className="absolute -inset-6 rounded-[2rem] bg-gradient-to-br from-cyan/10 via-transparent to-violet/10 blur-2xl" />
      <div className="relative rounded-3xl border border-line bg-surface/80 p-6 shadow-2xl shadow-black/40 backdrop-blur">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-cyan/10 text-cyan ring-1 ring-cyan/25">
              <Icon name="spark" />
            </span>
            <div className="h-2.5 w-28 rounded-full bg-line-strong" />
          </div>
          <span className="font-code text-[0.65rem] uppercase tracking-[0.18em] text-subtle">
            checklist
          </span>
        </div>
        <div className="mt-6 grid gap-3">
          {rows.map(([title, meta, tone], i) => (
            <div
              key={i}
              className="flex items-center gap-3 rounded-xl border border-line bg-raised/70 p-3"
            >
              <span
                className={`grid h-7 w-7 place-items-center rounded-md ring-1 ${
                  tone === "cyan"
                    ? "bg-cyan/10 text-cyan ring-cyan/25"
                    : "bg-violet/10 text-violet ring-violet/25"
                }`}
              >
                <Icon name={tone === "cyan" ? "document" : "info"} className="h-3.5 w-3.5" />
              </span>
              <div className="flex-1">
                <div className={`h-2 rounded-full bg-muted/40 ${title}`} />
                <div className={`mt-2 h-1.5 rounded-full bg-line-strong ${meta}`} />
              </div>
              <Icon name="check" className="h-4 w-4 text-ok/80" />
            </div>
          ))}
        </div>
        <div className="mt-5 flex items-center gap-2 border-t border-line pt-4">
          <Icon name="book" className="h-3.5 w-3.5 text-subtle" />
          <div className="h-1.5 w-40 rounded-full bg-line-strong" />
        </div>
      </div>
    </div>
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
    <div className="relative isolate overflow-hidden">
      <Atmosphere />
      <div className="relative mx-auto grid max-w-6xl items-center gap-12 px-4 py-12 sm:px-6 sm:py-16 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)] lg:py-24">
        <section aria-labelledby="welcome-title" className="oa-rise">
          <p className="inline-flex items-center gap-2 rounded-full border border-line bg-surface/70 px-3 py-1 font-code text-[0.7rem] uppercase tracking-[0.16em] text-cyan">
            <span className="h-1.5 w-1.5 rounded-full bg-cyan shadow-[0_0_10px_2px_rgba(84,214,255,0.6)]" />
            {WELCOME.eyebrow}
          </p>
          <h1
            id="welcome-title"
            className="mt-5 font-display text-4xl font-semibold leading-[1.08] tracking-tight text-fg sm:text-5xl lg:text-[3.4rem]"
          >
            {WELCOME.title}
          </h1>
          <p className="mt-5 max-w-xl text-base leading-relaxed text-muted sm:text-lg">
            {WELCOME.lead}
          </p>

          <ol className="mt-7 flex flex-wrap gap-x-5 gap-y-2 font-code text-[0.72rem] uppercase tracking-[0.12em] text-subtle">
            {WELCOME.steps.map((step, i) => (
              <li key={step} className="flex items-center gap-2">
                <span className="text-cyan">0{i + 1}</span> {step}
              </li>
            ))}
          </ol>

          <div className="mt-8 max-w-xl">
            <RealDataWarning />
          </div>
          {error !== null && (
            <p
              role="alert"
              className="mt-4 max-w-xl rounded-xl border border-danger/30 bg-danger/10 p-3 text-sm text-danger"
            >
              {customerError(error)}
            </p>
          )}
          <div className="mt-6 flex flex-wrap items-center gap-4">
            <Button
              variant="primary"
              onClick={onStart}
              disabled={busy}
              className="h-12 px-6 text-[0.95rem]"
            >
              {busy ? <Spinner /> : <Icon name="arrow" className="order-last h-4 w-4" />}
              {error !== null ? TEXT.tryAgain : WELCOME.start}
            </Button>
            <span className="text-xs text-subtle">No sign-up. Synthetic data only.</span>
          </div>
        </section>
        <Preview />
      </div>
      <div className="relative mx-auto max-w-6xl px-4 pb-16 sm:px-6">
        <ul className="grid gap-px overflow-hidden rounded-2xl border border-line bg-line sm:grid-cols-3">
          {WELCOME.points.map((point, i) => (
            <li key={point.title} className="bg-surface p-5">
              <p className="font-code text-[0.68rem] uppercase tracking-[0.16em] text-violet">
                0{i + 1}
              </p>
              <p className="mt-2 font-display font-semibold text-fg">{point.title}</p>
              <p className="mt-1 text-sm leading-relaxed text-muted">{point.body}</p>
            </li>
          ))}
        </ul>
      </div>
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
      <section className="oa-rise w-full max-w-md rounded-3xl border border-line bg-surface p-8 text-center">
        <span className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-raised text-muted ring-1 ring-line">
          <Icon name="refresh" className="h-5 w-5" />
        </span>
        <h1
          ref={heading}
          tabIndex={-1}
          className="mt-4 font-display text-xl font-semibold focus:outline-none"
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
