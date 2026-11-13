/**
 * Report fixtures for the S5 tests (no hand-written reports):
 * - **v1**: the 8 committed M3 report fixtures, read directly from
 *   `backend/tests/fixtures/m3_reports_v1/` (captured from M3 code before M4). They are
 *   stored rows, so keys the API would add as defaults (e.g. `report_version`) are absent;
 *   the UI must cope with that.
 * - **v2**: `reports/v2_*.json`, generated on 2026-11-14 from the backend's genuine M4
 *   acceptance reports (`tests/unit/test_m4_acceptance_s8.py::reports`: real graph runs on
 *   the synthetic test corpus; NLI disabled or the scripted FakeNLI, no model) via
 *   `ComplianceReport.model_dump(mode="json")`, the API's own serialization. Their
 *   `*.review_cases.json` were derived with the backend's `review_cases()`, as
 *   `GET /reports/{id}/review-cases` returns them.
 * - **live**: the DEGRADED v1 report and its (empty) review cases recorded from the real API
 *   in S4 (`live/report.json`, `live/review_cases.json`).
 */
import type { ComplianceReport, ReviewCasesResponse } from "../../api/types";
import liveReport from "./live/report.json";
import liveReviewCases from "./live/review_cases.json";

const v1Files = import.meta.glob("../../../../backend/tests/fixtures/m3_reports_v1/*.json", {
  eager: true,
  import: "default",
});
const v2Files = import.meta.glob("./reports/v2_*.json", { eager: true, import: "default" });

const stem = (path: string) => path.slice(path.lastIndexOf("/") + 1).replace(/\.json$/, "");

export interface ReportFixture {
  name: string;
  report: ComplianceReport;
  reviewCases: ReviewCasesResponse;
}

function emptyCases(report: ComplianceReport): ReviewCasesResponse {
  return { report_id: report.report_id, review_cases: [] };
}

export const v1Reports: ReportFixture[] = Object.entries(v1Files).map(([path, data]) => {
  const report = structuredClone(data) as ComplianceReport;
  // v1 reports have no verification results, so the read endpoint returns no cases.
  return { name: `v1_${stem(path)}`, report, reviewCases: emptyCases(report) };
});

export const v2Reports: ReportFixture[] = Object.entries(v2Files)
  .filter(([path]) => !path.endsWith(".review_cases.json"))
  .map(([path, data]) => {
    const name = stem(path);
    const cases = v2Files[`./reports/${name}.review_cases.json`];
    if (cases === undefined) throw new Error(`missing review cases for ${name}`);
    return {
      name,
      report: structuredClone(data) as ComplianceReport,
      reviewCases: structuredClone(cases) as ReviewCasesResponse,
    };
  });

export const liveFixture: ReportFixture = {
  name: "live_v1_degraded",
  report: structuredClone(liveReport.body) as ComplianceReport,
  reviewCases: structuredClone(liveReviewCases.body) as ReviewCasesResponse,
};

export const allReports: ReportFixture[] = [...v1Reports, ...v2Reports, liveFixture];

export function v2(name: string): ReportFixture {
  const found = v2Reports.find((r) => r.name === `v2_${name}`);
  if (!found) throw new Error(`no v2 fixture ${name}`);
  return found;
}

export function v1(name: string): ReportFixture {
  const found = v1Reports.find((r) => r.name === `v1_${name}`);
  if (!found) throw new Error(`no v1 fixture ${name}`);
  return found;
}
