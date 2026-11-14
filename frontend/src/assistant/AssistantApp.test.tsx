import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/errors";
import type { AssistantTurnResult } from "../api/types";
import { ASSISTANT_STORAGE_KEY } from "../storage/assistantSession";
import { chat, expectText, setup, startConversation } from "../test/assistantHarness";
import {
  FAKE_SESSION_ID,
  recorded,
  SYNTHETIC_PAN,
  TECHNICAL_MESSAGE,
  technicalErrorTurn,
} from "../test/fixtures/assistant";
import { REAL_DATA_WARNING } from "./labels";

afterEach(() => window.sessionStorage.clear());

describe("welcome", () => {
  it("shows the disclaimer and the real-data warning before anything can be typed", () => {
    setup();
    expectText(screen.getByLabelText("Prototype disclaimer"), /not an official HDFC Bank service/);
    expect(screen.getByRole("heading", { name: new RegExp(REAL_DATA_WARNING.title) })).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull(); // own shell
  });

  it("starts a conversation: privacy message, first question as choice cards", async () => {
    const { client, user } = setup();
    await startConversation(user);
    expect(client.assistantStart).toHaveBeenCalledTimes(1);
    expect(
      chat().getByText(/Please don't enter real personal or banking information/),
    ).toBeTruthy();
    const cards = within(
      screen.getByRole("group", { name: /Which account would you like/ }),
    ).getAllByRole("button");
    expect(cards.map((c) => c.textContent)).toContain("Savings account→");
    expect(screen.getByRole("textbox", { name: "Type your question or answer" })).toBeTruthy();
    expect(screen.getByText(REAL_DATA_WARNING.short)).toBeTruthy();
    // The id is kept in this tab's sessionStorage only: never in the URL or on the page.
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBe(FAKE_SESSION_ID);
    expect(window.location.href).not.toContain(FAKE_SESSION_ID);
    expect(document.body.innerHTML).not.toContain(FAKE_SESSION_ID);
  });

  it("shows a start failure and lets the customer try again", async () => {
    const { client, user } = setup();
    vi.mocked(client.assistantStart).mockRejectedValueOnce(new ApiError(0, "NETWORK_ERROR"));
    await user.click(screen.getByRole("button", { name: /I understand — start/ }));
    expectText(await screen.findByRole("alert"), /couldn't reach the assistant/);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("group", { name: /Which account would you like/ });
  });
});

describe("the conversation", () => {
  it("sends a choice card as a choose action and renders the next answer", async () => {
    const { bodies, user } = setup([recorded.residentFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    expect(bodies[0]).toEqual({ action: "choose", slot: "account_type", value: "SAVINGS" });
    expect(await chat().findByText(/I couldn't find this information/)).toBeTruthy();
  });

  it("sends free text with Enter, clears the box and shows a confirmation as Yes / No", async () => {
    const { bodies, user } = setup([recorded.nriConfirm(), recorded.nriQuestionScheme()]);
    await startConversation(user);
    const box = screen.getByRole("textbox", { name: "Type your question or answer" });
    await user.type(box, "I live in Singapore and want a savings account{Enter}");
    expect(bodies[0]).toEqual({ message: "I live in Singapore and want a savings account" });
    expect((box as HTMLTextAreaElement).value).toBe("");
    const confirm = await screen.findByRole("group", { name: /Just to check/ });
    await user.click(within(confirm).getByRole("button", { name: "Yes" }));
    expect(bodies[1]).toEqual({ action: "confirm_yes" });
    await screen.findByRole("group", { name: /NRE or an NRO/ });
  });

  it("offers not sure, skip and explain only when the API allows them", async () => {
    const { bodies, user } = setup([
      recorded.nriConfirm(),
      recorded.nriQuestionScheme(),
      recorded.nriQuestionHolding(),
    ]);
    await startConversation(user);
    // The essential first question: no Skip.
    expect(screen.queryByRole("button", { name: "Skip this question" })).toBeNull();
    await user.type(screen.getByRole("textbox"), "NRI savings{Enter}");
    await user.click(await screen.findByRole("button", { name: "Yes" }));
    await screen.findByRole("group", { name: /NRE or an NRO/ });
    await user.click(screen.getByRole("button", { name: "NRE account" }));
    await screen.findByRole("group", { name: /one person or jointly/ });
    expect(screen.getByRole("button", { name: "Skip this question" })).toBeTruthy();
    expect(bodies[2]).toEqual({ action: "choose", slot: "nri_scheme", value: "NRE" });
  });

  it("never shows a blocked message back and keeps the question answerable", async () => {
    const { user } = setup([recorded.blocked()]);
    await startConversation(user);
    await user.type(screen.getByRole("textbox"), `my PAN is ${SYNTHETIC_PAN}{Enter}`);
    expect(await chat().findByText(/Message not sent/)).toBeTruthy();
    expect(chat().getByText(/Please don't share personal identifiers/)).toBeTruthy();
    expect(document.body.innerHTML).not.toContain(SYNTHETIC_PAN);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    // The blocked turn changed nothing: the earlier question is still offered.
    expect(screen.getByRole("group", { name: /Which account would you like/ })).toBeTruthy();
  });

  it("explains why no checklist can be built (cannot_build)", async () => {
    const { user } = setup([recorded.cannotBuild()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: "I'm not sure" }));
    expect(await chat().findByText(/^I can't build a reliable checklist/)).toBeTruthy();
  });

  it("shows an out-of-scope fallback with the next question riding along", async () => {
    const { user } = setup([recorded.outOfScope()]);
    await startConversation(user);
    await user.type(screen.getByRole("textbox"), "what is the minimum balance?{Enter}");
    expect(await chat().findByText(/I can only help with the documents/)).toBeTruthy();
    expect(chat().getByText(/I couldn't find this information/)).toBeTruthy();
    expect(screen.getByRole("group", { name: /Which account would you like/ })).toBeTruthy();
  });

  it("shows the notice from the latest answer", async () => {
    const { user } = setup();
    await startConversation(user);
    expectText(
      screen.getByRole("complementary", { name: "About these answers" }),
      recorded.started().answer.notice,
    );
  });
});

describe("failures and retries", () => {
  it("retries a technical_error with the SAME key (nothing was stored)", async () => {
    const { keys, user } = setup([technicalErrorTurn(), recorded.residentFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    expectText(await screen.findByRole("alert"), TECHNICAL_MESSAGE);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await chat().findByText(/I couldn't find this information/)).toBeTruthy();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("retries an unknown outcome with the same key, and a new action gets a new key", async () => {
    const { keys, user } = setup([
      new ApiError(0, "NETWORK_ERROR"),
      recorded.nriConfirm(),
      recorded.nriQuestionScheme(),
    ]);
    await startConversation(user);
    await user.type(screen.getByRole("textbox"), "NRI savings{Enter}");
    expectText(await screen.findByRole("alert"), /couldn't reach the assistant/);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await user.click(await screen.findByRole("button", { name: "Yes" }));
    await screen.findByRole("group", { name: /NRE or an NRO/ });
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
  });

  it("does not offer a retry for a rejected request", async () => {
    const { user } = setup([new ApiError(422, "VALIDATION_ERROR")]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    expectText(await screen.findByRole("alert"), "That couldn't be sent");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ends the conversation when the server no longer has it", async () => {
    const { user } = setup([new ApiError(410, "SESSION_EXPIRED")]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    expect(
      await screen.findByRole("heading", { name: "This conversation has ended" }),
    ).toBeTruthy();
    expect(screen.getByText(/expired after 24 hours/)).toBeTruthy();
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Start a new conversation" }));
    expect(screen.getByRole("button", { name: /I understand — start/ })).toBeTruthy();
  });

  it("sends one turn at a time", async () => {
    let release: (value: AssistantTurnResult) => void = () => {};
    const { client, user } = setup();
    vi.mocked(client.assistantTurn).mockImplementationOnce(
      () => new Promise<AssistantTurnResult>((resolve) => (release = resolve)),
    );
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    for (const card of within(
      screen.getByRole("group", { name: /Which account would you like/ }),
    ).getAllByRole("button")) {
      expect((card as HTMLButtonElement).disabled).toBe(true);
    }
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    release(recorded.residentFallback());
    await chat().findByText(/I couldn't find this information/);
    expect(client.assistantTurn).toHaveBeenCalledTimes(1);
  });
});

describe("recovery", () => {
  it("rebuilds the conversation from this tab's stored id", async () => {
    const { client } = setup([], { stored: FAKE_SESSION_ID });
    await waitFor(() => expect(client.assistantGet).toHaveBeenCalledTimes(1));
    expect(
      await chat().findByText("I live in Singapore and want to open a savings account"),
    ).toBeTruthy();
    expect(chat().getByText("NRE account", { selector: "p" })).toBeTruthy(); // the choice label
    expect(client.assistantStart).not.toHaveBeenCalled();
  });

  it.each([
    [new ApiError(410, "SESSION_EXPIRED"), /expired after 24 hours/],
    [new ApiError(404, "SESSION_NOT_FOUND"), /could not be found/],
  ])("shows an ended conversation and forgets the id (%s)", async (error, text) => {
    setup([], { stored: FAKE_SESSION_ID, view: error });
    expect(await screen.findByText(text)).toBeTruthy();
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBeNull();
  });

  it("keeps the id when the reload fails for a transient reason", async () => {
    const { user, client } = setup([], {
      stored: FAKE_SESSION_ID,
      view: new ApiError(0, "NETWORK_ERROR"),
    });
    expectText(await screen.findByRole("alert"), /couldn't reach the assistant/);
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBe(FAKE_SESSION_ID);
    vi.mocked(client.assistantGet).mockResolvedValueOnce(recorded.nriSessionView());
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await chat().findByText("I live in Singapore and want to open a savings account"),
    ).toBeTruthy();
  });

  it("Start again forgets the id and returns to the welcome screen", async () => {
    const { user } = setup();
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: "Start again" }));
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBeNull();
    expect(screen.getByRole("button", { name: /I understand — start/ })).toBeTruthy();
  });
});
