import { useReviewCases } from "../../api/hooks";
import { ErrorNotice } from "../assistant/ErrorNotice";
import { REVIEW_REASON } from "./labels";

/**
 * Open review cases for a report, read-only (M4 D5): there is no decision, approval,
 * rejection, resolution or dismissal route, so this panel has no action controls at all.
 */
export function ReviewCasesPanel({
  reportId,
  names,
}: {
  reportId: string;
  /** requirement id -> requirement name, from the report. */
  names: ReadonlyMap<string, string>;
}) {
  const cases = useReviewCases(reportId);
  return (
    <section aria-labelledby="review-title" className="mt-6">
      <h2 id="review-title" className="text-xl font-semibold">
        Human review cases
      </h2>
      <p className="text-sm text-slate-600">
        Read-only. These cases are open for a human reviewer. Recording a review decision is not
        available in this prototype (it needs authentication).
      </p>
      {cases.isPending && (
        <p role="status" className="mt-2 text-sm text-slate-600">
          Loading review cases…
        </p>
      )}
      {cases.isError && (
        <ErrorNotice error={cases.error}>
          <p className="mt-1">
            The report above is still valid; only its review cases could not be loaded.
          </p>
          <button type="button" className="mt-1 underline" onClick={() => void cases.refetch()}>
            Try again
          </button>
        </ErrorNotice>
      )}
      {cases.isSuccess &&
        (cases.data.review_cases.length === 0 ? (
          <p className="mt-2 text-sm">
            No review cases were recorded for this report. Every result still requires human review.
          </p>
        ) : (
          <table className="mt-2 w-full text-left text-sm">
            <caption className="sr-only">Open review cases</caption>
            <thead>
              <tr className="border-b border-slate-300">
                <th scope="col" className="py-1 pr-2">
                  Requirement
                </th>
                <th scope="col" className="py-1 pr-2">
                  Reason
                </th>
                <th scope="col" className="py-1 pr-2">
                  Status
                </th>
                <th scope="col" className="py-1">
                  Opened
                </th>
              </tr>
            </thead>
            <tbody>
              {cases.data.review_cases.map((c) => (
                <tr key={c.id} className="border-b border-slate-200">
                  <td className="py-1 pr-2">
                    {names.get(c.requirement_id) ?? c.requirement_id}{" "}
                    <span className="font-mono text-xs text-slate-500">{c.requirement_id}</span>
                  </td>
                  <td className="py-1 pr-2">{REVIEW_REASON[c.reason]}</td>
                  <td className="py-1 pr-2">Open</td>
                  <td className="py-1">{new Date(c.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}
