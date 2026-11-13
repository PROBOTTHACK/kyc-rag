import { Link, useParams } from "react-router";

import { useReport } from "../../api/hooks";
import { errorCode, ErrorNotice } from "../assistant/ErrorNotice";
import { ReportView } from "./ReportView";
import { ReviewCasesPanel } from "./ReviewCasesPanel";

/**
 * A stored compliance report (v1 or v2) and its read-only review cases (M5 S5). The report
 * is immutable once stored, so it is fetched once. This page never touches the assistant
 * session, which stays in memory above the routes.
 */
export function ReportPage() {
  const { reportId = "" } = useParams();
  const report = useReport(reportId === "" ? null : reportId);

  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        Compliance Report
      </h1>
      <p className="mt-1 text-sm">
        <Link to="/assistant" className="underline">
          Back to the KYC Assistant
        </Link>
      </p>

      {report.isPending && (
        <p role="status" className="mt-4 text-sm text-slate-600">
          Loading the report…
        </p>
      )}

      {report.isError && (
        <ErrorNotice error={report.error}>
          {errorCode(report.error) === "REPORT_NOT_FOUND" ? (
            <p className="mt-1">
              Reports are deleted after the retention period, and a mistyped address finds nothing.
            </p>
          ) : (
            <button type="button" className="mt-1 underline" onClick={() => void report.refetch()}>
              Try again
            </button>
          )}
        </ErrorNotice>
      )}

      {report.isSuccess && (
        <>
          <ReportView report={report.data} />
          <ReviewCasesPanel
            reportId={report.data.report_id}
            names={
              new Map(
                [...(report.data.checklist ?? []), ...(report.data.unverified_items ?? [])].map(
                  (i) => [i.requirement_id, i.name],
                ),
              )
            }
          />
        </>
      )}
    </section>
  );
}
