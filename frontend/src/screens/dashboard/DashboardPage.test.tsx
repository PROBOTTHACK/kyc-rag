import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { ApiError, messageFor } from "../../api/errors";
import { ApiContext } from "../../api/hooks";
import type { HealthResponse, SessionView } from "../../api/types";
import { App } from "../../App";
import { DISCLAIMER } from "../../components/Disclaimer";
import { STORAGE_KEY } from "../../storage/sessions";
import { derivedSessionExpired, live } from "../../test/fixtures/live";
import { Pipe } from "../../test/http";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const AT = "2026-11-14T09:00:00.000Z";

const HEALTH_OK: HealthResponse = {
  status: "ok",
  checks: {
    postgres: { ok: true, latency_ms: 4.2, error: null },
    redis: { ok: true, latency_ms: 1.1, error: null },
    qdrant: { ok: true, latency_ms: 9.7, error: null },
  },
};

function withId(view: SessionView, sid: string): SessionView {
  return { ...view, session_id: sid };
}

function running(): SessionView {
  const view = live.sessionAwaiting();
  const wf = view.workflow!;
  return {
    ...view,
    workflow: {
      run_id: wf.run_id,
      thread_id: wf.thread_id,
      status: "running",
      started_at: wf.started_at,
      finished_at: null,
      node_events: wf.node_events.slice(0, 1),
    },
  };
}

/** What each known session's GET returns: a view, or an error to throw. */
type Server = Record<string, SessionView | ApiError>;

function seed(ids: string[], current: string | null = null) {
  window.sessionStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ sessions: ids.map((i) => ({ id: i, addedAt: AT })), current }),
  );
}

function setup(server: Server, health: () => Promise<HealthResponse> = async () => HEALTH_OK) {
  const client = {
    health: vi.fn(health),
    getSession: vi.fn(async (sid: string) => {
      const answer = server[sid];
      if (answer === undefined) throw new ApiError(404, "SESSION_NOT_FOUND");
      if (answer instanceof ApiError) throw answer;
      return structuredClone(answer);
    }),
    getReport: vi.fn(async () => {
      throw new ApiError(404, "REPORT_NOT_FOUND");
    }),
    getReviewCases: vi.fn(async () => ({ report_id: "x", review_cases: [] })),
    fetchEvents: vi.fn(async (_s: string, _l: string | null, signal: AbortSignal) => {
      const pipe = new Pipe();
      signal.addEventListener("abort", () => pipe.end(), { once: true });
      return pipe.response();
    }),
    // state-changing calls: none may happen from the Dashboard
    createSession: vi.fn(async () => live.sessionCreated()),
    submitMessage: vi.fn(),
    resume: vi.fn(),
    retry: vi.fn(),
  };
  const view = (path = "/") =>
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <ApiContext.Provider value={client as unknown as ApiClient}>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ApiContext.Provider>
      </QueryClientProvider>,
    );
  const utils = view();
  return { client, user: userEvent.setup(), utils, rerender: view };
}

const pageText = () => document.body.textContent ?? "";
const row = (n: number) => screen.getByRole("listitem", { name: `Session ${n}` });

describe("Dashboard: API health", () => {
  it("shows every service as reachable", async () => {
    setup({});
    await screen.findByText("All services are reachable.");
    const checks = screen.getByRole("list", { name: "Service checks" });
    expect(
      within(checks)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([
      "Database (PostgreSQL): reachable (4 ms)",
      "Cache and live events (Redis): reachable (1 ms)",
      "Search index (Qdrant): reachable (10 ms)",
    ]);
    expect(screen.getByText(DISCLAIMER)).toBeTruthy();
    expect(screen.getByText("Demo data")).toBeTruthy();
  });

  it("shows a degraded API (503 with its checks) as data, not as an error", async () => {
    setup({}, async () => ({
      status: "degraded",
      checks: {
        ...HEALTH_OK.checks,
        redis: { ok: false, latency_ms: 2000, error: "TimeoutError" },
      },
    }));
    await screen.findByText(/Some services are unavailable/);
    expect(pageText()).toContain("Cache and live events (Redis): unavailable");
    expect(pageText()).not.toContain("TimeoutError"); // exception names are not shown
  });

  it("shows loading, then an unreachable API, and can check again", async () => {
    let fail: () => void = () => undefined;
    const { client, user } = setup(
      {},
      () => new Promise((_, reject) => (fail = () => reject(new ApiError(0, "NETWORK_ERROR")))),
    );
    expect(screen.getByText("Checking the API…")).toBeTruthy();
    fail();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(messageFor("NETWORK_ERROR"));
    client.health.mockImplementation(async () => HEALTH_OK);
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await screen.findByText("All services are reachable.");
  });
});

describe("Dashboard: sessions in this browser tab", () => {
  it("is empty with no stored sessions, and says the list is browser-local", async () => {
    setup({});
    expect(screen.getByText(/No sessions in this tab yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Start one in the KYC Assistant" })).toBeTruthy();
    expect(pageText()).toContain("This is not a complete history");
    expect(screen.queryByRole("button", { name: "Forget sessions" })).toBeNull();
  });

  it("shows each session's real status, never its id, and no completion it cannot establish", async () => {
    const report = live.sessionCompleted().workflow!.report_id!;
    seed([id(1), id(2), id(3), id(4), id(5), id(6), id(7)]);
    setup({
      [id(1)]: withId(live.sessionNoRun(), id(1)),
      [id(2)]: withId(running(), id(2)),
      [id(3)]: withId(live.sessionAwaiting(), id(3)),
      [id(4)]: withId(live.sessionCompleted(), id(4)),
      [id(5)]: withId(derivedSessionExpired(), id(5)),
      // id(6): unknown to the server (purged) -> 404
      [id(7)]: new ApiError(503, "WORKFLOW_UNAVAILABLE"),
    });
    await waitFor(() => expect(screen.queryByText("Loading its status…")).toBeNull());

    expect(row(1).textContent).toContain("No question asked yet.");
    expect(row(2).textContent).toContain("Latest question: Working on your question…");
    expect(row(3).textContent).toContain("Latest question: Waiting for your answers");
    expect(row(4).textContent).toContain("Latest question: Finished · Result: Degraded result");
    expect(within(row(4)).getByRole("link", { name: "Open the report" }).getAttribute("href")).toBe(
      `/reports/${report}`,
    );
    expect(row(5).textContent).toContain("Expired.");
    expect(row(6).textContent).toContain("No longer available on the server");
    expect(within(row(6)).queryByRole("button", { name: "Open in the Assistant" })).toBeNull();
    expect(row(7).textContent).toContain(messageFor("WORKFLOW_UNAVAILABLE"));

    // only a completed run links a report or shows a result
    for (const n of [1, 2, 3, 5, 6, 7]) {
      expect(within(row(n)).queryByRole("link", { name: "Open the report" })).toBeNull();
      expect(row(n).textContent).not.toMatch(/Finished|Result:/);
    }
    for (let n = 1; n <= 7; n++) expect(pageText()).not.toContain(id(n));
    expect(pageText()).not.toMatch(/\b(approved?|rejected?|compliant)\b/i);
  });

  it("opens a stored session in the Assistant", async () => {
    seed([id(1), id(3)]);
    const { user } = setup({
      [id(1)]: withId(live.sessionNoRun(), id(1)),
      [id(3)]: withId(live.sessionAwaiting(), id(3)),
    });
    await waitFor(() => expect(row(2).textContent).toContain("Waiting for your answers"));
    await user.click(within(row(2)).getByRole("button", { name: "Open in the Assistant" }));
    await screen.findByRole("heading", { name: "More information needed" });
    expect(JSON.parse(window.sessionStorage.getItem(STORAGE_KEY)!).current).toBe(id(3));
    await user.click(screen.getByRole("link", { name: "Dashboard" }));
    await waitFor(() => expect(row(2).textContent).toContain("(open in the Assistant)"));
  });

  it("forgets sessions locally only: nothing on the server is touched", async () => {
    seed([id(1), id(4)], id(4));
    const { client, user } = setup({
      [id(1)]: withId(live.sessionNoRun(), id(1)),
      [id(4)]: withId(live.sessionCompleted(), id(4)),
    });
    await waitFor(() => expect(row(2).textContent).toContain("Finished"));
    await user.click(screen.getByRole("button", { name: "Forget sessions" }));
    expect(screen.getByText(/No sessions in this tab yet/)).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    for (const write of [client.createSession, client.submitMessage, client.resume, client.retry]) {
      expect(write).not.toHaveBeenCalled();
    }
    // the Assistant has no current session any more
    await user.click(screen.getByRole("link", { name: "KYC Assistant" }));
    expect(screen.getByRole("button", { name: "Start a demo session" })).toBeTruthy();
  });

  it("ignores malformed stored data and starts empty", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, '{"sessions":[{"id":"bad"}], "current": 7');
    setup({});
    expect(screen.getByText(/No sessions in this tab yet/)).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe("Session persistence across a reload of the tab", () => {
  it("a session started in the Assistant survives a reload and is listed on the Dashboard", async () => {
    const sid = live.sessionCreated().session_id;
    const { client, user, utils, rerender } = setup({ [sid]: live.sessionNoRun() });
    await user.click(screen.getByRole("link", { name: "KYC Assistant" }));
    await user.click(screen.getByRole("button", { name: "Start a demo session" }));
    await screen.findByRole("textbox", { name: "Your question" });

    utils.unmount(); // the reload: only sessionStorage survives
    rerender("/assistant");
    await screen.findByRole("textbox", { name: "Your question" }); // same session, no start screen
    expect(client.createSession).toHaveBeenCalledTimes(1);
    expect(client.getSession).toHaveBeenLastCalledWith(sid, expect.anything());
    await user.click(screen.getByRole("link", { name: "Dashboard" }));
    await waitFor(() => expect(row(1).textContent).toContain("No question asked yet."));
    expect(row(1).textContent).toContain("(open in the Assistant)");
    expect(pageText()).not.toContain(sid);
  });
});
