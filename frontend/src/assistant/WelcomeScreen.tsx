import { useRef } from "react";

import type { ApiError } from "../api/errors";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { customerError, ENDED, REAL_DATA_WARNING, TEXT, WELCOME } from "./labels";
import type { Phase } from "./useConversation";

/** The real-data warning: shown before anything can be typed, and kept beside the composer. */
export function RealDataWarning({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="flex items-center gap-2 text-xs text-amber-900">
        <WarningIcon /> {REAL_DATA_WARNING.short}
      </p>
    );
  }
  return (
    <section
      aria-labelledby="real-data-warning"
      className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-amber-950 sm:p-5"
    >
      <h2 id="real-data-warning" className="flex items-center gap-2 font-semibold">
        <WarningIcon /> {REAL_DATA_WARNING.title}
      </h2>
      <p className="mt-1 text-sm leading-relaxed">{REAL_DATA_WARNING.body}</p>
    </section>
  );
}

export function WarningIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" className="h-4 w-4 shrink-0 fill-amber-600">
      <path d="M10 2 1 18h18L10 2Zm-1 6h2v5H9V8Zm0 6h2v2H9v-2Z" />
    </svg>
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
      <p role="status" className="mx-auto mt-16 max-w-md text-center text-slate-600">
        <Spinner /> {WELCOME.resuming}
      </p>
    );
  }
  return (
    <div className="mx-auto grid max-w-3xl gap-6">
      <section className="rounded-2xl bg-white p-6 shadow-sm ring-1 ring-slate-200 sm:p-10">
        <p className="text-sm font-medium uppercase tracking-wider text-sky-700">
          Opening an account
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">
          {WELCOME.title}
        </h1>
        <p className="mt-3 text-base leading-relaxed text-slate-600">{WELCOME.lead}</p>
        <ul className="mt-6 grid gap-3 text-sm text-slate-700">
          {WELCOME.points.map((point) => (
            <li key={point} className="flex gap-3">
              <span
                aria-hidden="true"
                className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-sky-600"
              />
              {point}
            </li>
          ))}
        </ul>
        <div className="mt-6">
          <RealDataWarning />
        </div>
        {error !== null && (
          <p
            role="alert"
            className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-900 ring-1 ring-rose-200"
          >
            {customerError(error)}
          </p>
        )}
        <button
          type="button"
          onClick={onStart}
          disabled={busy}
          className="mt-6 inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-slate-900 px-6 text-base font-semibold text-white shadow-sm hover:bg-slate-800 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-sky-600 disabled:cursor-wait disabled:opacity-70 sm:w-auto"
        >
          {busy && <Spinner light />}
          {error !== null ? TEXT.tryAgain : WELCOME.start}
        </button>
      </section>
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
    <section className="mx-auto mt-8 max-w-xl rounded-2xl bg-white p-6 text-center shadow-sm ring-1 ring-slate-200 sm:p-8">
      <h1
        ref={heading}
        tabIndex={-1}
        className="text-xl font-semibold text-slate-900 focus:outline-none"
      >
        This conversation has ended
      </h1>
      <p className="mt-2 text-slate-600">{ENDED[reason ?? "SESSION_NOT_FOUND"]}</p>
      <button
        type="button"
        onClick={onStart}
        className="mt-6 min-h-12 rounded-xl bg-slate-900 px-6 font-semibold text-white hover:bg-slate-800 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
      >
        {TEXT.startNew}
      </button>
    </section>
  );
}

export function Spinner({ light = false }: { light?: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block h-4 w-4 animate-spin rounded-full border-2 motion-reduce:animate-none ${
        light ? "border-white/40 border-t-white" : "border-slate-300 border-t-slate-700"
      }`}
    />
  );
}
