import { useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { Link } from "react-router";

import { queryKeys, useRetryRun } from "../../api/hooks";
import { useFocusWhenLost } from "../../components/useFocusWhenLost";
import type { WorkflowView } from "../../api/types";
import { errorCode, ErrorNotice } from "./ErrorNotice";
import { OUTCOME_TEXT } from "./labels";

const REVIEW_NOTE =
  "Every result requires human review. This is decision support, not a KYC decision.";

/** The end of a run: its outcome and report link, or an explicit retry after a failure. */
export function RunResult({
  sessionId,
  workflow,
  onRetried,
}: {
  sessionId: string;
  workflow: WorkflowView;
  onRetried: () => void;
}) {
  if (workflow.status === "completed") {
    return <CompletedRun workflow={workflow} />;
  }
  if (workflow.status === "failed") {
    return <FailedRun sessionId={sessionId} onRetried={onRetried} />;
  }
  if (workflow.status === "abandoned") {
    return (
      <section className="mt-4 rounded border border-slate-200 bg-white p-4">
        <h2 className="text-lg font-semibold">Run abandoned</h2>
        <p className="mt-1 text-sm">This run was abandoned and cannot be continued.</p>
      </section>
    );
  }
  return null;
}

function CompletedRun({ workflow }: { workflow: WorkflowView }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  const outcome = workflow.outcome != null ? OUTCOME_TEXT[workflow.outcome] : null;
  return (
    <section
      aria-labelledby="result-title"
      className="mt-4 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="result-title" ref={heading} tabIndex={-1} className="text-lg font-semibold">
        Result: {outcome?.label ?? "Finished"}
      </h2>
      {outcome && <p className="mt-1 text-sm">{outcome.detail}</p>}
      <p className="mt-1 text-sm font-medium">{REVIEW_NOTE}</p>
      {workflow.report_id != null ? (
        <Link
          to={`/reports/${encodeURIComponent(workflow.report_id)}`}
          className="mt-2 inline-block underline"
        >
          Open the report
        </Link>
      ) : (
        <p className="mt-2 text-sm text-slate-700">No report was stored for this run.</p>
      )}
    </section>
  );
}

function FailedRun({ sessionId, onRetried }: { sessionId: string; onRetried: () => void }) {
  const retry = useRetryRun(sessionId);
  const queryClient = useQueryClient();
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  return (
    <section
      aria-labelledby="failed-title"
      className="mt-4 rounded border border-red-300 bg-white p-4"
    >
      <h2 id="failed-title" ref={heading} tabIndex={-1} className="text-lg font-semibold">
        The run stopped before it finished
      </h2>
      <p className="mt-1 text-sm">
        No result was produced. You can retry it from its last saved step, or ask a new question.
      </p>
      <button
        type="button"
        className="mt-2 rounded bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
        disabled={retry.isPending}
        onClick={() =>
          retry.mutate(undefined, {
            onSuccess: onRetried,
            onError: (error) => {
              if (
                errorCode(error) === "NOTHING_TO_RETRY" ||
                errorCode(error) === "SESSION_EXPIRED"
              ) {
                void queryClient.invalidateQueries({ queryKey: queryKeys.session(sessionId) });
              }
            },
          })
        }
      >
        {retry.isPending ? "Retrying…" : "Retry the run"}
      </button>
      {retry.isError && <ErrorNotice error={retry.error} />}
    </section>
  );
}
