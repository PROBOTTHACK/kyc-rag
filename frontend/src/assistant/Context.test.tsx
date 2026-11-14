import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AssistantTurnResult } from "../api/types";

import { chat, expectText, setup, startConversation } from "../test/assistantHarness";
import { recorded } from "../test/fixtures/assistant";

/** A company current-account checklist (recorded), the starting point for the context tests. */
async function withCompanyChecklist(...more: AssistantTurnResult[]) {
  const harness = setup([recorded.llpChecklist(), recorded.llpCorrected(), ...more]);
  await startConversation(harness.user);
  await harness.user.click(screen.getByRole("button", { name: /Current account/ }));
  await chat().findByText(/Here's what the HDFC Bank form asks for/);
  await harness.user.type(screen.getByRole("textbox"), "actually a company{Enter}");
  await chat().findByText(/I've updated your answer to: A company/);
  return harness;
}

/** The top-line control that opens the checklist (present only while one applies). */
const checklistButton = () => screen.queryByRole("button", { name: /^Checklist \d+$/ });

describe("context-aware conversation (recorded from the real API)", () => {
  it("answers a greeting without repeating the checklist", async () => {
    const { user } = await withCompanyChecklist(recorded.contextGreeting());
    await user.type(screen.getByRole("textbox"), "Hello{Enter}");
    const reply = await chat().findByText(/Hello again! We were looking at a current account/);
    expectText(reply.closest("li"), "What would you like to know?");
    expect(reply.closest("li")!.textContent).not.toContain("Here's what the HDFC Bank form");
    expect(checklistButton()).toBeTruthy(); // the checklist still applies
  });

  it("asks whether a child's account is separate, then keeps the company account aside", async () => {
    const { user, bodies } = await withCompanyChecklist(
      recorded.contextOffer(),
      recorded.contextSeparate(),
      recorded.contextDaughterIndia(),
    );
    await user.type(screen.getByRole("textbox"), "What about my child?{Enter}");
    const offer = await screen.findByRole("group", { name: /a separate account, or do you want/ });
    // The company checklist is still one tap away while the customer decides.
    expect(checklistButton()).toBeTruthy();
    await user.click(within(offer).getByRole("button", { name: "Ask about a separate account" }));
    expect(bodies.at(-1)).toEqual({ action: "new_scenario" });
    expect(await chat().findByText(/I'll keep a current account for a company/)).toBeTruthy();
    const who = screen.getByRole("group", { name: /Who is the account for/ });
    expectText(within(who).getAllByRole("button")[0]!, "An individual living in India");
    await user.type(screen.getByRole("textbox"), "No, we are in India.{Enter}");
    await chat().findByText(/couldn't find this information/);
    // The parked company account is listed in "Your answers", with a way back.
    await user.click(screen.getByRole("button", { name: "Your answers" }));
    const drawer = screen.getByRole("dialog");
    expectText(drawer, "Other accounts in this conversation");
    expect(
      within(drawer).getByRole("button", { name: /Go back to a current account for a company/ }),
    ).toBeTruthy();
  });

  it("offers to go back to an earlier account and sends the switch", async () => {
    const { user, bodies } = await withCompanyChecklist(
      recorded.contextOffer(),
      recorded.contextSeparate(),
      recorded.contextSwitchOffer(),
      recorded.contextSwitched(),
    );
    await user.type(screen.getByRole("textbox"), "What about my child?{Enter}");
    await user.click(await screen.findByRole("button", { name: "Ask about a separate account" }));
    await chat().findByText(/I'll keep a current account for a company/);
    await user.type(screen.getByRole("textbox"), "what about the company current account?{Enter}");
    await user.click(await screen.findByRole("button", { name: "Yes, go back to it" }));
    expect(bodies.at(-1)).toEqual({ action: "switch_scenario", value: "0" });
    expect(await chat().findByText(/We're back to a current account for a company/)).toBeTruthy();
    expect(checklistButton()).toBeTruthy();
  });

  it("a confirmed change of holder retires the old checklist", async () => {
    const { user } = await withCompanyChecklist(
      recorded.contextChange(),
      recorded.contextChangedToNri(),
    );
    await user.type(screen.getByRole("textbox"), "Actually, the account is for my child{Enter}");
    expect(await chat().findByText(/an account for a person rather than/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /living outside India/ }));
    expect(
      await chat().findByText(/I've updated your answer to: An individual living outside/),
    ).toBeTruthy();
    // The company checklist no longer applies to these answers.
    expect(checklistButton()).toBeNull();
  });

  it("a follow-up gets a focused answer, not the whole checklist again", async () => {
    const follow = recorded.contextFollowUp().answer;
    expect(follow.reply).toContain("the form asks you to bring:");
    expect(follow.reply).not.toContain("details to fill in");
  });
});

describe("the simplified workspace", () => {
  it("has one conversation and no permanent progress, checklist or side panels", async () => {
    const { user } = setup();
    await startConversation(user);
    expect(screen.getByRole("region", { name: "Conversation with the assistant" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Progress and your answers" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Progress" })).toBeNull(); // in the drawer
    expect(checklistButton()).toBeNull(); // nothing to open yet
    expect(screen.getByText("New conversation")).toBeTruthy();
  });

  it("folds the choices away to give the conversation room, and shows them again", async () => {
    const { user } = setup();
    await startConversation(user);
    const hide = screen.getByRole("button", { name: "Hide options" });
    expect(hide.getAttribute("aria-expanded")).toBe("true");
    await user.click(hide);
    expect(screen.queryByRole("group", { name: /Which account would you like/ })).toBeNull();
    const show = screen.getByRole("button", { name: /^Show options \(\d+\)$/ });
    expect(show.getAttribute("aria-expanded")).toBe("false");
    await user.click(show);
    expect(screen.getByRole("group", { name: /Which account would you like/ })).toBeTruthy();
  });

  it("switches views from the keyboard and keeps the checklist out of the conversation", async () => {
    const { user } = setup([recorded.llpChecklist()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText(/Here's what the HDFC Bank form asks for/);
    const checklistTab = checklistButton()!;
    checklistTab.focus();
    await user.keyboard("{Enter}");
    expect(checklistTab.getAttribute("aria-pressed")).toBe("true");
    const heading = screen.getByRole("heading", { name: "Your checklist", level: 2 });
    await new Promise((r) => requestAnimationFrame(() => r(null)));
    expect(document.activeElement).toBe(heading);
  });

  it("prints only the checklist (the conversation is hidden on paper)", async () => {
    const { user } = setup([recorded.llpChecklist()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText(/Here's what the HDFC Bank form asks for/);
    const conversation = screen.getByRole("region", { name: "Conversation with the assistant" });
    expect(conversation.getAttribute("data-print")).toBe("hide");
    const checklist = screen.getByRole("region", { name: "Your checklist" });
    expect(checklist.getAttribute("data-print")).toBe("expand");
    expect(checklist.parentElement!.className).toContain("print:!block");
  });
});
