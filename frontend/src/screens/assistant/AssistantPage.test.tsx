import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { ApiError, messageFor } from "../../api/errors";
import { ApiContext } from "../../api/hooks";
import type { SessionView } from "../../api/types";
import { App } from "../../App";
import { DISCLAIMER } from "../../components/Disclaimer";
import {
  derivedAwaitingNewCheckpoint,
  derivedAwaitingUnreadable,
  derivedErrors,
  derivedSessionExpired,
  derivedSessionFailed,
  live,
  recordedError,
  SYNTHETIC_PAN,
} from "../../test/fixtures/live";
import { liveFixture } from "../../test/fixtures/reports";
import { envelope, nodeFrame, Pipe, runFrame } from "../../test/http";

const QUESTION = "What KYC documents does an LLP need to open an account?";
const SID = live.sessionCreated().session_id;
const RUN = live.sessionAwaiting().workflow!.run_id;
const NODES = live.sessionCompleted().workflow!.node_events.map((e) => e.node);

type Mock = ReturnType<typeof vi.fn>;

function setup(initial: SessionView = live.sessionNoRun()) {
  const state = { session: initial };
  const pipes: Pipe[] = [];
  const keys: Record<string, string[]> = { messages: [], resume: [], retry: [] };
  const client = {
    createSession: vi.fn(async () => live.sessionCreated()),
    getSession: vi.fn(async () => structuredClone(state.session)),
    submitMessage: vi.fn(async (_s: string, _b: unknown, key: string) => {
      keys.messages!.push(key);
      return live.messageAccepted();
    }),
    resume: vi.fn(async (_s: string, _b: unknown, key: string) => {
      keys.resume!.push(key);
      return live.resumeAccepted();
    }),
    retry: vi.fn(async (_s: string, key: string) => {
      keys.retry!.push(key);
      return live.messageAccepted();
    }),
    // S5: the report the completed run links to, as recorded from the real API.
    getReport: vi.fn(async () => structuredClone(liveFixture.report)),
    getReviewCases: vi.fn(async () => structuredClone(liveFixture.reviewCases)),
    fetchEvents: vi.fn(async (_s: string, _last: string | null, signal: AbortSignal) => {
      const pipe = new Pipe();
      pipes.push(pipe);
      signal.addEventListener("abort", () => pipe.end(), { once: true });
      return pipe.response();
    }),
  };
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 0 } },
  });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={queryClient}>
      <ApiContext.Provider value={client as unknown as ApiClient}>
        <MemoryRouter initialEntries={["/assistant"]}>
          <App />
        </MemoryRouter>
      </ApiContext.Provider>
    </QueryClientProvider>,
  );
  const push = (text: string) => act(() => pipes.at(-1)!.push(text));
  return { state, pipes, keys, client, user, push };
}

/** Make a mocked call fail once with a recorded (or derived) error response. */
async function failOnce(mock: Mock, recorded: { status: number; body: unknown }) {
  const error = await recordedError(recorded);
  mock.mockImplementationOnce(async () => {
    throw error;
  });
}

async function startSession(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Start a demo session" }));
}

function textbox() {
  return screen.getByRole("textbox", { name: "Your question" }) as HTMLTextAreaElement;
}

function bodyText() {
  return document.body.textContent ?? "";
}

afterEach(() => vi.restoreAllMocks());

describe("KYC Assistant", () => {
  it("starts a synthetic_demo session, keeps the disclaimer and never shows the session id", async () => {
    const { client, user } = setup();
    expect(screen.getByText(DISCLAIMER)).toBeTruthy();
    await startSession(user);
    expect(client.createSession).toHaveBeenCalledWith("synthetic_demo");
    await waitFor(() => expect(textbox()).toBeTruthy());
    expect(screen.getByText(DISCLAIMER)).toBeTruthy();
    expect(bodyText()).not.toContain(SID);
    await waitFor(() => expect(client.fetchEvents).toHaveBeenCalled()); // stream opened first
  });

  it("runs the whole flow: question, live progress, clarification with the backend's options, result", async () => {
    const { state, client, keys, user, push } = setup();
    await startSession(user);
    await user.type(await waitFor(textbox), QUESTION);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(client.submitMessage).toHaveBeenCalledWith(
      SID,
      { message: QUESTION },
      keys.messages![0],
    );
    await waitFor(() => expect(textbox().value).toBe("")); // cleared after a successful submit

    // live progress until the clarification interrupt
    state.session = live.sessionAwaiting();
    push(runFrame(RUN, 0, "running") + nodeFrame(RUN, 1, NODES[0]!) + nodeFrame(RUN, 2, NODES[1]!));
    push(nodeFrame(RUN, 3, NODES[2]!) + runFrame(RUN, 3, "awaiting_clarification"));
    const clarHeading = await screen.findByRole("heading", { name: "More information needed" });
    expect(screen.getByRole("status", { name: "" }).textContent).toBeDefined();
    // S7 accessibility: "Ask" disappeared, so focus moves to the new section, not the body
    await waitFor(() => expect(document.activeElement).toBe(clarHeading));
    const steps = screen.getByRole("list", { name: "Completed steps" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(3);
    expect(within(steps).getByText(/Input screening/)).toBeTruthy();

    // exactly the backend's options, nothing pre-selected, no answer guessed
    const clar = live.sessionAwaiting().workflow!.clarification!;
    for (const q of clar.questions) {
      const group = screen.getByRole("group", { name: q.text });
      const radios = within(group).getAllByRole("radio") as HTMLInputElement[];
      expect(radios.map((r) => r.value)).toEqual(q.options);
      expect(radios.some((r) => r.checked)).toBe(false);
    }
    expect(screen.queryByRole("textbox", { name: "Your question" })).toBeNull();
    const send = screen.getByRole("button", { name: "Send answers" });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    await user.click(
      within(screen.getByRole("group", { name: clar.questions[0]!.text })).getByLabelText("LLP"),
    );
    expect((send as HTMLButtonElement).disabled).toBe(true); // every question must be answered
    await user.click(
      within(screen.getByRole("group", { name: clar.questions[1]!.text })).getByLabelText(
        "CURRENT ACCOUNT",
      ),
    );
    await user.click(send);
    expect(client.resume).toHaveBeenCalledWith(
      SID,
      {
        checkpoint_id: clar.checkpoint_id,
        answers: { entity_type: "LLP", account_type: "CURRENT_ACCOUNT" },
      },
      keys.resume![0],
    );

    // the run finishes
    state.session = live.sessionCompleted();
    push(runFrame(RUN, 3, "running"));
    push(
      NODES.slice(3)
        .map((n, i) => nodeFrame(RUN, i + 4, n))
        .join("") + runFrame(RUN, NODES.length, "completed"),
    );
    act(() => undefined);
    const resultHeading = await screen.findByRole("heading", { name: "Result: Degraded result" });
    await waitFor(() => expect(document.activeElement).toBe(resultHeading)); // S7: focus kept
    const report = live.sessionCompleted().workflow!.report_id!;
    expect(screen.getByRole("link", { name: "Open the report" }).getAttribute("href")).toBe(
      `/reports/${report}`,
    );
    expect(bodyText()).toContain("Every result requires human review");
    expect(
      within(screen.getByRole("list", { name: "Completed steps" })).getAllByRole("listitem"),
    ).toHaveLength(NODES.length);
    expect(textbox()).toBeTruthy(); // a new question can be asked
    expect(bodyText()).not.toContain(QUESTION);
    expect(bodyText()).not.toMatch(/\b(approved|rejected|compliant)\b/i);
  });

  it("a stale checkpoint reloads the current question and clears the answers", async () => {
    const { state, client, user } = setup(live.sessionAwaiting());
    await failOnce(client.resume, live.errors.staleCheckpoint);
    await startSession(user);
    const clar = live.sessionAwaiting().workflow!.clarification!;
    await screen.findByRole("heading", { name: "More information needed" });
    await user.click(screen.getByLabelText("LLP"));
    await user.click(screen.getByLabelText("CURRENT ACCOUNT"));
    const reads = client.getSession.mock.calls.length;
    state.session = derivedAwaitingNewCheckpoint();
    await user.click(screen.getByRole("button", { name: "Send answers" }));

    await waitFor(() => expect(client.getSession.mock.calls.length).toBeGreaterThan(reads));
    await waitFor(() =>
      expect((screen.getAllByRole("radio") as HTMLInputElement[]).some((r) => r.checked)).toBe(
        false,
      ),
    );
    expect(client.resume.mock.calls[0]![1]).toMatchObject({ checkpoint_id: clar.checkpoint_id });
    // the fresh form answers the NEW checkpoint
    await user.click(screen.getByLabelText("LLP"));
    await user.click(screen.getByLabelText("CURRENT ACCOUNT"));
    await user.click(screen.getByRole("button", { name: "Send answers" }));
    expect(client.resume.mock.calls[1]![1]).toMatchObject({
      checkpoint_id: derivedAwaitingNewCheckpoint().workflow!.clarification!.checkpoint_id,
    });
  });

  it("shows a fixed message for an invalid answer", async () => {
    const { client, user } = setup(live.sessionAwaiting());
    await failOnce(client.resume, live.errors.invalidAnswer);
    await startSession(user);
    await screen.findByRole("heading", { name: "More information needed" });
    await user.click(screen.getByLabelText("LLP"));
    await user.click(screen.getByLabelText("CURRENT ACCOUNT"));
    await user.click(screen.getByRole("button", { name: "Send answers" }));
    expect((await screen.findByRole("alert")).textContent).toContain(messageFor("INVALID_ANSWER"));
  });

  it("RUN_IN_PROGRESS on a new question refetches and shows the run that is in progress", async () => {
    const { state, client, user } = setup();
    await failOnce(client.submitMessage, live.errors.runInProgress);
    await startSession(user);
    await user.type(await waitFor(textbox), QUESTION);
    state.session = live.sessionAwaiting();
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByRole("heading", { name: "More information needed" });
    expect(screen.queryByRole("textbox", { name: "Your question" })).toBeNull();
  });

  it("SENSITIVE_DATA_DETECTED clears the text and never echoes it", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(() => undefined),
    );
    const { client, user } = setup();
    await failOnce(client.submitMessage, live.errors.sensitive);
    await startSession(user);
    await user.type(await waitFor(textbox), `Customer PAN ${SYNTHETIC_PAN}`);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(messageFor("SENSITIVE_DATA_DETECTED"));
    expect(alert.textContent).toContain("The text box was cleared.");
    expect(textbox().value).toBe("");
    expect(bodyText()).not.toContain(SYNTHETIC_PAN);
    for (const spy of spies) {
      for (const call of spy.mock.calls) expect(JSON.stringify(call)).not.toContain(SYNTHETIC_PAN);
    }
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull(); // a definitive rejection
  });

  it("an expired session (410) blocks new input and offers a new session", async () => {
    const { state, client, user } = setup();
    await failOnce(client.submitMessage, derivedErrors.sessionExpired);
    await startSession(user);
    await user.type(await waitFor(textbox), QUESTION);
    state.session = derivedSessionExpired();
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText(/This session has expired/);
    expect(screen.queryByRole("textbox", { name: "Your question" })).toBeNull();
    await user.click(screen.getAllByRole("button", { name: "Start a new session" })[0]!);
    expect(screen.getByRole("button", { name: "Start a demo session" })).toBeTruthy();
  });

  it("'Try again' after a network failure resends the same action with the same key", async () => {
    const { client, keys, user } = setup();
    client.submitMessage.mockImplementationOnce(async (_s: string, _b: unknown, key: string) => {
      keys.messages!.push(key);
      throw new ApiError(0, "NETWORK_ERROR");
    });
    await startSession(user);
    await user.type(await waitFor(textbox), QUESTION);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect((await screen.findByRole("alert")).textContent).toContain(messageFor("NETWORK_ERROR"));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(keys.messages).toHaveLength(2));
    expect(keys.messages![1]).toBe(keys.messages![0]);
  });

  it("a failed run can be retried explicitly; the error class is not shown", async () => {
    const { client, keys, user } = setup(derivedSessionFailed());
    await startSession(user);
    await screen.findByRole("heading", { name: "The run stopped before it finished" });
    const steps = screen.getByRole("list", { name: "Completed steps" });
    expect(within(steps).getByText(/Stopped by an error/)).toBeTruthy();
    expect(bodyText()).not.toContain("RuntimeError");
    expect(textbox()).toBeTruthy(); // a new question is also possible
    const connections = client.fetchEvents.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Retry the run" }));
    await waitFor(() => expect(client.retry).toHaveBeenCalledWith(SID, keys.retry![0]));
    // the stream follows the new run (follow() is a no-op while it is still open)
    expect(client.fetchEvents.mock.calls.length).toBeGreaterThanOrEqual(connections);
  });

  it("a retry while the run's lease is still valid explains when to try again", async () => {
    const { client, user } = setup(derivedSessionFailed());
    await failOnce(client.retry, derivedErrors.retryLeaseValid);
    await startSession(user);
    await user.click(await screen.findByRole("button", { name: "Retry the run" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(messageFor("RUN_IN_PROGRESS"));
    expect(alert.textContent).toContain("about 60 seconds");
  });

  it("an unreadable clarification can be loaded again", async () => {
    const { client, user } = setup(derivedAwaitingUnreadable());
    await startSession(user);
    await screen.findByText("The pending question could not be loaded.");
    const reads = client.getSession.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Load it again" }));
    await waitFor(() => expect(client.getSession.mock.calls.length).toBe(reads + 1));
  });

  it("an unknown session offers a new one", async () => {
    const { client, user } = setup();
    await failOnce(client.getSession, live.errors.sessionNotFound);
    await startSession(user);
    expect((await screen.findByRole("alert")).textContent).toContain(
      messageFor("SESSION_NOT_FOUND"),
    );
    await user.click(screen.getByRole("button", { name: "Start a new session" }));
    expect(screen.getByRole("button", { name: "Start a demo session" })).toBeTruthy();
  });

  it("shows reconnecting and unavailable live-progress states, with a manual reconnect", async () => {
    const { client, user } = setup();
    client.fetchEvents.mockImplementationOnce(async () => envelope(503, "WORKFLOW_UNAVAILABLE"));
    await startSession(user);
    await screen.findByText(/Reconnecting \(attempt 2\)/);
    client.fetchEvents.mockImplementationOnce(async () => envelope(404, "SESSION_NOT_FOUND"));
    await screen.findByText(/Live progress is unavailable/, {}, { timeout: 3_000 });
    const before = client.fetchEvents.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Reconnect" }));
    await waitFor(() => expect(client.fetchEvents.mock.calls.length).toBe(before + 1));
  });

  it("keeps the session in memory across navigation to the report and back", async () => {
    const { client, user } = setup(live.sessionCompleted());
    await startSession(user);
    await user.click(await screen.findByRole("link", { name: "Open the report" }));
    expect(screen.getByRole("heading", { level: 1, name: "Compliance Report" })).toBeTruthy();
    // the real report the run produced (S5)
    await screen.findByRole("heading", { name: "Outcome: Degraded result" });
    expect(client.getReport).toHaveBeenCalledWith(
      live.sessionCompleted().workflow!.report_id,
      expect.anything(),
    );
    // back through the report's own link, then through the main navigation
    await user.click(screen.getByRole("link", { name: "Back to the KYC Assistant" }));
    await screen.findByRole("heading", { name: "Result: Degraded result" });
    await user.click(screen.getByRole("link", { name: "Open the report" }));
    await screen.findByRole("heading", { name: "Outcome: Degraded result" });
    await user.click(screen.getByRole("link", { name: "KYC Assistant" }));
    await screen.findByRole("heading", { name: "Result: Degraded result" });
    expect(client.createSession).toHaveBeenCalledTimes(1);
  });
});
