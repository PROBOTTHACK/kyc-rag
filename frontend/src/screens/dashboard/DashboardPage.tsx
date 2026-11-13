import { Link, useNavigate } from "react-router";

import { useHealth, useSession } from "../../api/hooks";
import type { StoredSession } from "../../storage/sessions";
import { errorCode, ErrorNotice } from "../assistant/ErrorNotice";
import { OUTCOME_TEXT, RUN_STATUS_TEXT } from "../assistant/labels";
import { useAssistantSession } from "../assistant/session";

/**
 * The minimal Dashboard (M5 S6): API health and the demo sessions known to THIS browser tab
 * (sessionStorage, M5-D4). It is not an account-wide history (there are no accounts) and it
 * never shows a session id. Every status comes from the existing read endpoints; nothing is
 * inferred. Document Intelligence and Observability are M6.
 */
export function DashboardPage() {
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        Dashboard
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        <span className="rounded bg-slate-200 px-1 text-xs font-semibold uppercase">Demo data</span>{" "}
        Synthetic demo sessions only.
      </p>
      <HealthPanel />
      <SessionsPanel />
    </section>
  );
}

const CHECK_LABELS: Record<string, string> = {
  postgres: "Database (PostgreSQL)",
  redis: "Cache and live events (Redis)",
  qdrant: "Search index (Qdrant)",
};

function HealthPanel() {
  const health = useHealth();
  return (
    <section
      aria-labelledby="health-title"
      className="mt-4 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="health-title" className="text-lg font-semibold">
        API health
      </h2>
      {health.isPending && (
        <p role="status" className="mt-1 text-sm text-slate-600">
          Checking the API…
        </p>
      )}
      {health.isError && (
        <ErrorNotice error={health.error}>
          <p className="mt-1">
            The API could not be reached, so its services could not be checked.
          </p>
        </ErrorNotice>
      )}
      {health.isSuccess && (
        <>
          <p className="mt-1 text-sm font-medium">
            {health.data.status === "ok"
              ? "All services are reachable."
              : "Some services are unavailable; the workflow may not work."}
          </p>
          <ul aria-label="Service checks" className="mt-1 text-sm">
            {Object.entries(health.data.checks).map(([name, check]) => (
              <li key={name}>
                {CHECK_LABELS[name] ?? name}: {check.ok ? "reachable" : "unavailable"}
                {check.ok && (
                  <span className="text-slate-500"> ({Math.round(check.latency_ms)} ms)</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      <button
        type="button"
        className="mt-2 text-sm underline disabled:opacity-50"
        disabled={health.isFetching}
        onClick={() => void health.refetch()}
      >
        Check again
      </button>
    </section>
  );
}

function SessionsPanel() {
  const { known, sessionId, forget } = useAssistantSession();
  return (
    <section
      aria-labelledby="sessions-title"
      className="mt-4 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="sessions-title" className="text-lg font-semibold">
        Sessions in this browser tab
      </h2>
      <p className="text-sm text-slate-600">
        Only demo sessions started or opened in this browser tab are listed. This is not a complete
        history: the list is kept in this tab only, survives reloads, and is cleared when the tab is
        closed.
      </p>
      {known.length === 0 ? (
        <p className="mt-2 text-sm">
          No sessions in this tab yet.{" "}
          <Link to="/assistant" className="underline">
            Start one in the KYC Assistant
          </Link>
          .
        </p>
      ) : (
        <>
          <ol aria-label="Sessions" className="mt-2 space-y-2">
            {known.map((s, i) => (
              <SessionRow key={s.id} session={s} index={i + 1} current={s.id === sessionId} />
            ))}
          </ol>
          <div className="mt-3 border-t border-slate-200 pt-2 text-sm">
            <button type="button" className="underline" onClick={forget}>
              Forget sessions
            </button>
            <p className="text-xs text-slate-600">
              Removes these sessions from this browser tab only. Nothing is deleted on the server;
              sessions and reports expire there on their own schedule.
            </p>
          </div>
        </>
      )}
    </section>
  );
}

function SessionRow({
  session,
  index,
  current,
}: {
  session: StoredSession;
  index: number;
  current: boolean;
}) {
  const view = useSession(session.id);
  const { setSessionId } = useAssistantSession();
  const navigate = useNavigate();
  const label = `Session ${index}`;
  const gone = view.isError && errorCode(view.error) === "SESSION_NOT_FOUND";

  return (
    <li aria-label={label} className="rounded border border-slate-200 p-3 text-sm">
      <p className="font-medium">
        {label}
        {current && <span className="font-normal text-slate-600"> (open in the Assistant)</span>}
      </p>
      <p className="text-xs text-slate-500">
        Added in this tab {new Date(session.addedAt).toLocaleString()}
      </p>

      {view.isPending && <p role="status">Loading its status…</p>}
      {gone && (
        <p>
          No longer available on the server. It may have been deleted after the retention period.
        </p>
      )}
      {view.isError && !gone && (
        <ErrorNotice error={view.error}>
          <button type="button" className="mt-1 underline" onClick={() => void view.refetch()}>
            Try again
          </button>
        </ErrorNotice>
      )}
      {view.isSuccess && <SessionStatus view={view.data} />}

      {!gone && (
        <button
          type="button"
          className="mt-2 underline"
          onClick={() => {
            setSessionId(session.id);
            void navigate("/assistant");
          }}
        >
          Open in the Assistant
        </button>
      )}
    </li>
  );
}

function SessionStatus({ view }: { view: NonNullable<ReturnType<typeof useSession>["data"]> }) {
  const wf = view.workflow;
  return (
    <div className="mt-1">
      <p>
        {view.status === "expired"
          ? "Expired. Its results stay readable until they are deleted."
          : `Active until ${new Date(view.expires_at).toLocaleString()}.`}
      </p>
      {wf == null ? (
        <p>No question asked yet.</p>
      ) : (
        <>
          <p>
            Latest question: {RUN_STATUS_TEXT[wf.status]}
            {wf.status === "completed" && wf.outcome != null && (
              <> · Result: {OUTCOME_TEXT[wf.outcome].label}</>
            )}
          </p>
          {wf.status === "completed" &&
            (wf.report_id != null ? (
              <Link to={`/reports/${encodeURIComponent(wf.report_id)}`} className="underline">
                Open the report
              </Link>
            ) : (
              <p className="text-slate-600">No report was stored for this question.</p>
            ))}
        </>
      )}
    </div>
  );
}
