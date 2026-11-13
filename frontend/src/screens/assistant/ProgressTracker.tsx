import type { NodeProgress } from "../../api/events";
import type { RunStatus } from "../../api/types";
import { nodeLabel, NODE_STATUS_LABELS, RUN_STATUS_TEXT } from "./labels";

/** Live workflow progress for one run: its current status and the steps completed so far. */
export function ProgressTracker({
  status,
  nodes,
}: {
  status: RunStatus;
  nodes: readonly NodeProgress[];
}) {
  return (
    <section
      aria-labelledby="progress-title"
      className="mt-4 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="progress-title" className="text-lg font-semibold">
        Progress
      </h2>
      <p role="status" aria-live="polite" className="mt-1 text-sm text-slate-700">
        {RUN_STATUS_TEXT[status]}
      </p>
      {nodes.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">No steps completed yet.</p>
      ) : (
        <ol aria-label="Completed steps" className="mt-2 space-y-1 text-sm">
          {nodes.map((n) => (
            <li key={n.seq} className="flex gap-2">
              <span aria-hidden="true">{n.status === "failed" ? "✗" : "✓"}</span>
              <span>
                {nodeLabel(n.node)}{" "}
                <span className="text-slate-500">({NODE_STATUS_LABELS[n.status] ?? n.status})</span>
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
