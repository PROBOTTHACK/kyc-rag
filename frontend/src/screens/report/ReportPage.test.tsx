import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { ApiError, messageFor } from "../../api/errors";
import { ApiContext } from "../../api/hooks";
import {
  allReports,
  liveFixture,
  v1,
  v2,
  v2Reports,
  type ReportFixture,
} from "../../test/fixtures/reports";
import { OUTCOME_TEXT } from "../assistant/labels";
import {
  APPLICABILITY,
  CONFLICT_SCAN_FAILED,
  CONFLICT_SCAN_OFF,
  conflictScanBounded,
  pages,
  REVIEW_REASON,
  VERDICT,
} from "./labels";
import { ReportPage } from "./ReportPage";

function show(
  fixture: ReportFixture,
  overrides: Partial<Pick<ApiClient, "getReport" | "getReviewCases">> = {},
) {
  const client = {
    getReport: vi.fn(async () => structuredClone(fixture.report)),
    getReviewCases: vi.fn(async () => structuredClone(fixture.reviewCases)),
    ...overrides,
  };
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ApiContext.Provider value={client as unknown as ApiClient}>
        <MemoryRouter initialEntries={[`/reports/${fixture.report.report_id}`]}>
          <Routes>
            <Route path="/reports/:reportId" element={<ReportPage />} />
          </Routes>
        </MemoryRouter>
      </ApiContext.Provider>
    </QueryClientProvider>,
  );
  return client;
}

const pageText = () => document.body.textContent ?? "";

async function loaded(fixture: ReportFixture) {
  const outcome = OUTCOME_TEXT[fixture.report.outcome].label;
  await screen.findByRole("heading", { name: `Outcome: ${outcome}` });
  await waitFor(() => expect(screen.queryByText("Loading review cases…")).toBeNull());
}

/** "verified" may only appear negated ("not verified", "not claim-verified", "unverified"). */
function unnegatedVerified(text: string): string[] {
  const stripped = text.replace(/\b(not|un)[\s-]?(claim-)?verified\b/gi, "");
  return stripped.match(/\bverified\b/gi) ?? [];
}

describe("Compliance Report: every committed and generated fixture", () => {
  it("covers v1 and every v2 verification and conflict-scan state", () => {
    expect(allReports.filter((f) => f.name.startsWith("v1_"))).toHaveLength(8);
    const statuses = new Set(v2Reports.map((f) => f.report.verification_meta?.status));
    expect(statuses).toEqual(new Set(["ran", "partial", "failed", "not_configured"]));
    const scans = new Set(v2Reports.map((f) => f.report.verification_meta?.contradiction_scan));
    expect(scans).toEqual(new Set(["not_run", "bounded", "failed"]));
  });

  it.each(allReports.map((f) => [f.name, f] as const))(
    "%s renders its own data",
    async (_n, fixture) => {
      const r = fixture.report;
      show(fixture);
      await loaded(fixture);
      const text = pageText();

      // human review, the notice and the backend's own message (with its disclaimer)
      expect(text).toContain("Human review required.");
      expect(text).toContain(r.notice ?? "Decision support only. Not a KYC decision.");
      for (const p of r.user_message.split(/\n{2,}/)) expect(text).toContain(p.trim());

      // the conflict-scan statement, word for word as the backend writes it
      const meta = r.verification_meta ?? null;
      const scan =
        meta === null || meta.contradiction_scan === "not_run"
          ? CONFLICT_SCAN_OFF
          : meta.contradiction_scan === "failed"
            ? CONFLICT_SCAN_FAILED
            : conflictScanBounded(
                meta.contradiction_scan_size ?? 0,
                meta.contradiction_pairs_not_scanned ?? 0,
              );
      expect(text).toContain(scan);
      if (meta !== null) expect(r.user_message).toContain(scan); // identical to the backend's wording

      // every checklist item: applicability, check status, validator note and citations as stored
      for (const item of r.checklist ?? []) {
        const group = within(
          screen.getByRole("group", { name: `${item.name} (${item.requirement_id})` }),
        );
        const groupText = group.getByRole("heading", { name: item.name }).parentElement!
          .textContent!;
        expect(groupText).toContain(`Applicability: ${APPLICABILITY[item.applicability].label}`);
        expect(groupText).toContain(
          VERDICT[item.verification_status ?? "not_claim_verified"].label,
        );
        expect(groupText).toContain(item.uncertainty_label);
        for (const c of item.citations) {
          expect(groupText).toContain(`${c.document_key}, ${pages(c.page, c.page_end)}`);
        }
        if ((item.verification_status ?? "not_claim_verified").startsWith("not_")) {
          expect(groupText).not.toMatch(/Supported by the cited text|Partly supported/);
        }
      }
      if ((r.checklist ?? []).length > 0) expect(text).toContain("known issue K5");

      // never an approval or a rejection, anywhere on the page
      expect(text).not.toMatch(/\b(approved?|approval|rejected?|rejection|compliant)\b/i);
      // never an unqualified "verified" in the UI's own text. The backend's user_message is
      // shown verbatim (checked above) and its prescribed fallback ("I don't have sufficient
      // verified information ...") is a negation the UI must not reword.
      const summary = screen.getByRole("region", { name: "Summary" }).textContent ?? "";
      expect(unnegatedVerified(text.replace(summary, ""))).toEqual([]);
      // read-only: no action control anywhere on the page
      expect(screen.queryAllByRole("button")).toHaveLength(0);
      expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
      expect(screen.queryAllByRole("radio")).toHaveLength(0);
    },
  );
});

describe("Compliance Report: specific states", () => {
  it("v2 with checking not enabled: every item 'Not verified', open cases listed read-only", async () => {
    const fixture = v2("not_configured");
    show(fixture);
    await loaded(fixture);
    expect(pageText()).toContain("Automated claim checking is not enabled");
    const item = fixture.report.checklist![0]!;
    const group = screen.getByRole("group", { name: `${item.name} (${item.requirement_id})` });
    expect(group.textContent).toContain("Claim check: Not verified.");
    expect(group.textContent).toContain("not a finding that the item is wrong");
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(fixture.reviewCases.review_cases.length);
    expect(rows[0]!.textContent).toContain(item.name);
    expect(rows[0]!.textContent).toContain(REVIEW_REASON.not_verified);
    expect(rows[0]!.textContent).toContain("Open");
    expect(pageText()).toContain("Recording a review decision is not available");
  });

  it("v2 check ran: the automated support is qualified, with its model and uncalibrated scores", async () => {
    const fixture = v2("ran_mandatory");
    show(fixture);
    await loaded(fixture);
    const section = screen.getByRole("region", { name: "Mandatory" });
    expect(section.textContent).toContain(VERDICT.partially_supported.label);
    expect(pageText()).toContain("not a legal determination");
    expect(pageText()).toContain("uncalibrated signals, not confidence");
  });

  it("v2 unsupported: the item is set aside for review and never called not required", async () => {
    const fixture = v2("unsupported");
    show(fixture);
    await loaded(fixture);
    expect(pageText()).toContain("No checklist item remained after validation and checking");
    const set = screen.getByRole("region", { name: "Set aside for review" });
    const u = fixture.report.unverified_items![0]!;
    expect(
      within(set).getByRole("group", { name: `${u.name} (${u.requirement_id})` }),
    ).toBeTruthy();
    expect(set.textContent).toContain("This does not mean they are not required.");
    expect(screen.getByRole("table").textContent).toContain(REVIEW_REASON.unsupported);
  });

  it("v2 source conflict: both sources shown, unresolved, never settled by date", async () => {
    const fixture = v2("conflict");
    show(fixture);
    await loaded(fixture);
    const c = fixture.report.source_conflicts![0]!;
    const section = screen.getByRole("region", { name: "Possible conflicts between sources" });
    expect(section.textContent).toContain(c.side_a.document_key);
    expect(section.textContent).toContain(c.side_b.document_key);
    expect(section.textContent).toContain("Unresolved");
    expect(section.textContent).toContain("a newer date never settles a conflict");
    expect(pageText()).toContain(REVIEW_REASON.conflicting_sources);
  });

  it("v2 contradicted: shown as unclear and contradicted, flagged for review", async () => {
    const fixture = v2("contradicted_and_conflict");
    show(fixture);
    await loaded(fixture);
    const section = screen.getByRole("region", { name: "Unclear" });
    expect(section.textContent).toContain(VERDICT.contradicted.label);
    expect(section.textContent).toContain("Needs human review");
  });

  it("v1: shows the M3 notice and 'not claim-verified', with form links as relationships only", async () => {
    const fixture = v1("checklist_partial_llp");
    show(fixture);
    await loaded(fixture);
    expect(pageText()).toContain("Not claim-verified (M4).");
    expect(pageText()).toContain("before claim checking existed");
    const linked = (fixture.report.checklist ?? []).find(
      (i) => (i.related_form_fields ?? []).length > 0,
    );
    if (linked) {
      const group = screen.getByRole("group", {
        name: `${linked.name} (${linked.requirement_id})`,
      });
      expect(group.textContent).toContain("the form asks for this; not an obligation");
    }
    expect(screen.getAllByText(/Evidence gaps|Unresolved items/).length).toBeGreaterThan(0);
  });

  it("a failed search is a technical problem, distinct from no evidence", async () => {
    show(v1("insufficient_evidence_retrieval_failed"));
    await loaded(v1("insufficient_evidence_retrieval_failed"));
    expect(pageText()).toContain("search failed (a technical problem)");
    expect(pageText()).not.toContain("No eligible evidence was found");
  });

  it("no eligible evidence never reads as 'not required'", async () => {
    show(v1("insufficient_evidence_none"));
    await loaded(v1("insufficient_evidence_none"));
    expect(pageText()).toContain("This does not mean that nothing is required.");
  });

  it("blocked and missing-information outcomes show codes and fields, never input", async () => {
    show(v1("blocked"));
    await loaded(v1("blocked"));
    expect(pageText()).toContain("Input screening codes");
    expect(pageText()).toContain("No checklist is shown for this outcome.");
  });

  it("insufficient information lists the missing details", async () => {
    show(v1("insufficient_information"));
    await loaded(v1("insufficient_information"));
    expect(pageText()).toMatch(/Missing details: .*(Customer type|Account type)/);
  });

  it("degradations and forms are shown when present", async () => {
    const fixture = liveFixture;
    show(fixture);
    await loaded(fixture);
    expect(pageText()).toContain("Parts that did not run normally");
    expect(pageText()).toContain("Language model");
    expect(screen.getByText(/No review cases were recorded for this report/)).toBeTruthy();
    if ((fixture.report.forms ?? []).length > 0)
      expect(pageText()).toContain("Form fields are never policy obligations.");
  });
});

describe("Compliance Report: loading, missing and unavailable", () => {
  it("shows loading, then the report", async () => {
    let release: () => void = () => undefined;
    const fixture = v2("not_configured");
    show(fixture, {
      getReport: () =>
        new Promise((resolve) => {
          release = () => resolve(structuredClone(fixture.report));
        }),
    });
    expect(screen.getByRole("status").textContent).toContain("Loading the report");
    release();
    await loaded(fixture);
  });

  it("a missing or purged report says so, with no retry loop", async () => {
    show(v2("not_configured"), {
      getReport: async () => {
        throw new ApiError(404, "REPORT_NOT_FOUND");
      },
    });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(messageFor("REPORT_NOT_FOUND"));
    expect(alert.textContent).toContain("deleted after the retention period");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("an unreachable server offers a retry", async () => {
    show(v2("not_configured"), {
      getReport: async () => {
        throw new ApiError(0, "NETWORK_ERROR");
      },
    });
    expect((await screen.findByRole("alert")).textContent).toContain(messageFor("NETWORK_ERROR"));
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("review cases failing to load does not hide the report", async () => {
    const fixture = v2("not_configured");
    show(fixture, {
      getReviewCases: async () => {
        throw new ApiError(503, "WORKFLOW_UNAVAILABLE");
      },
    });
    await screen.findByRole("heading", { name: `Outcome: ${OUTCOME_TEXT.DEGRADED.label}` });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("only its review cases could not be loaded");
    const first = fixture.report.checklist![0]!;
    expect(
      screen.getByRole("group", { name: `${first.name} (${first.requirement_id})` }),
    ).toBeTruthy();
  });
});
