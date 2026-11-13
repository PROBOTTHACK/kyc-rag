import { useCreateSession, useSession, useSessionEvents } from "../../api/hooks";
import type { NodeProgress } from "../../api/events";
import { ClarificationForm } from "./ClarificationForm";
import { errorCode, ErrorNotice } from "./ErrorNotice";
import { ProgressTracker } from "./ProgressTracker";
import { QuestionForm } from "./QuestionForm";
import { RunResult } from "./RunResult";
import { useAssistantSession } from "./session";

/**
 * The KYC Assistant (M5 S4): start a synthetic demo session, ask a question, follow the run
 * live, answer clarification questions, and retry a failed run explicitly. Report details
 * are S5; this screen only links to the report.
 */
export function AssistantPage() {
  const { sessionId, setSessionId } = useAssistantSession();
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        KYC Assistant
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Ask which KYC documents a customer type needs. Answers are drafted from cited public
        sources, checked deterministically, and always need human review.
      </p>
      {sessionId === null ? (
        <StartSession onCreated={setSessionId} />
      ) : (
        <SessionWorkspace
          key={sessionId}
          sessionId={sessionId}
          onNewSession={() => setSessionId(null)}
        />
      )}
    </section>
  );
}

function StartSession({ onCreated }: { onCreated: (id: string) => void }) {
  const create = useCreateSession();
  return (
    <div className="mt-4 rounded border border-slate-200 bg-white p-4">
      <p className="text-sm">
        Sessions use <strong>synthetic demo data only</strong>. A session is kept only while this
        page stays open; reloading the page forgets it.
      </p>
      <button
        type="button"
        className="mt-2 rounded bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
        disabled={create.isPending}
        onClick={() =>
          create.mutate("synthetic_demo", { onSuccess: (s) => onCreated(s.session_id) })
        }
      >
        {create.isPending ? "Starting…" : "Start a demo session"}
      </button>
      {create.isError && <ErrorNotice error={create.error} />}
    </div>
  );
}

function SessionWorkspace({
  sessionId,
  onNewSession,
}: {
  sessionId: string;
  onNewSession: () => void;
}) {
  const session = useSession(sessionId);
  const events = useSessionEvents(sessionId);

  const newSession = (
    <button type="button" className="mt-2 underline" onClick={onNewSession}>
      Start a new session
    </button>
  );

  if (session.isPending) {
    return (
      <p role="status" className="mt-4 text-sm text-slate-600">
        Loading the session…
      </p>
    );
  }
  if (session.isError) {
    const gone = errorCode(session.error) === "SESSION_NOT_FOUND";
    return (
      <ErrorNotice error={session.error}>
        {gone ? (
          newSession
        ) : (
          <button type="button" className="mt-2 underline" onClick={() => void session.refetch()}>
            Try again
          </button>
        )}
      </ErrorNotice>
    );
  }

  const view = session.data;
  const workflow = view.workflow;
  const expired = view.status === "expired";
  const running = workflow?.status === "running";
  const awaiting = workflow?.status === "awaiting_clarification";
  const live = events.runs.find((r) => r.runId === workflow?.run_id);
  const nodes: readonly NodeProgress[] = live?.nodes.length
    ? live.nodes
    : (workflow?.node_events ?? []);

  return (
    <div>
      <p className="mt-3 text-xs text-slate-500">
        Demo session active until {new Date(view.expires_at).toLocaleString()}.
      </p>
      <ConnectionNotice
        status={events.status}
        failures={events.failures}
        error={events.error}
        onReconnect={events.follow}
        onRefresh={() => void session.refetch()}
      />

      {expired && (
        <div role="alert" className="mt-4 rounded border border-slate-300 bg-slate-100 p-4 text-sm">
          <p>This session has expired. Its results stay readable until they are deleted.</p>
          {newSession}
        </div>
      )}

      {workflow && <ProgressTracker status={workflow.status} nodes={nodes} />}

      {awaiting &&
        !expired &&
        (workflow.clarification != null ? (
          <ClarificationForm
            key={workflow.clarification.checkpoint_id}
            sessionId={sessionId}
            clarification={workflow.clarification}
            onResumed={events.follow}
          />
        ) : (
          <div
            role="alert"
            className="mt-4 rounded border border-amber-300 bg-amber-50 p-4 text-sm"
          >
            <p>The pending question could not be loaded.</p>
            <button type="button" className="mt-2 underline" onClick={() => void session.refetch()}>
              Load it again
            </button>
          </div>
        ))}

      {workflow && (
        <RunResult sessionId={sessionId} workflow={workflow} onRetried={events.follow} />
      )}

      {!running && !awaiting && !expired && (
        <QuestionForm sessionId={sessionId} disabled={false} onSubmitted={events.follow} />
      )}

      {!expired && (
        <p className="mt-4 text-sm">
          Finished with this session?{" "}
          <button type="button" className="underline" onClick={onNewSession}>
            Start a new session
          </button>
        </p>
      )}
    </div>
  );
}

function ConnectionNotice({
  status,
  failures,
  error,
  onReconnect,
  onRefresh,
}: {
  status: string;
  failures: number;
  error: unknown;
  onReconnect: () => void;
  onRefresh: () => void;
}) {
  if (status === "reconnecting") {
    return (
      <p role="status" className="mt-3 rounded bg-amber-50 p-2 text-sm text-amber-950">
        Live progress connection lost. Reconnecting (attempt {failures + 1})…
      </p>
    );
  }
  if (status === "error") {
    return (
      <div role="alert" className="mt-3 rounded bg-amber-50 p-2 text-sm text-amber-950">
        <p>
          Live progress is unavailable. The workflow keeps running on the server; the status below
          may be out of date.
        </p>
        {error !== null && <p className="text-xs">({errorCodeText(error)})</p>}
        <button type="button" className="mr-3 mt-1 underline" onClick={onReconnect}>
          Reconnect
        </button>
        <button type="button" className="mt-1 underline" onClick={onRefresh}>
          Refresh the status
        </button>
      </div>
    );
  }
  return null;
}

function errorCodeText(error: unknown): string {
  return errorCode(error) ?? "UNKNOWN_ERROR";
}
