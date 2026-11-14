import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AssistantTurnResult } from "../api/types";

import { chat, setup, startConversation } from "../test/assistantHarness";
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

const showGroup = () => screen.queryByRole("group", { name: "Show" });

describe("context-aware conversation (recorded from the real API)", () => {
  it("clarifies a question about a child without dropping the current checklist", async () => {
    const clarify = recorded.contextClarify().answer;
    const { user } = await withCompanyChecklist(recorded.contextClarify());
    await user.type(screen.getByRole("textbox"), "What about my child?{Enter}");
    expect(await chat().findByText(/I don't want to mix the two applications/)).toBeTruthy();
    // One question, individuals first; the company answer is still the profile.
    const group = screen.getByRole("group", { name: /Who is the account for/ });
    const labels = within(group)
      .getAllByRole("button")
      .map((b) => b.textContent);
    expect(labels[0]).toContain("An individual living in India");
    expect(clarify.profile.customer_category!.value).toBe("COMPANY");
    // The checklist built for these same answers is still one tap away.
    expect(showGroup()).toBeTruthy();
  });

  it("a separate request keeps the checklist and offers a separate one", async () => {
    const { user } = await withCompanyChecklist(recorded.contextSeparate());
    await user.type(screen.getByRole("textbox"), "I also want an account for my child{Enter}");
    const start = await chat().findByRole("button", { name: "Start a separate checklist" });
    expect(showGroup()).toBeTruthy();
    await user.click(start);
    expect(screen.getByRole("button", { name: /I understand — start/ })).toBeTruthy();
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
    expect(showGroup()).toBeNull();
  });

  it("a follow-up gets a focused answer, not the whole checklist again", async () => {
    const follow = recorded.contextFollowUp().answer;
    expect(follow.reply).toContain("the form asks you to bring:");
    expect(follow.reply).not.toContain("details to fill in");
  });
});

describe("the simplified workspace", () => {
  it("has one conversation, a compact progress line and no permanent side panels", async () => {
    const { user } = setup();
    await startConversation(user);
    expect(screen.getByRole("region", { name: "Conversation with the assistant" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Progress and your answers" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Progress" })).toBeNull(); // in the drawer
    expect(screen.getByText("1/4")).toBeTruthy();
  });

  it("switches views from the keyboard and keeps the checklist out of the conversation", async () => {
    const { user } = setup([recorded.llpChecklist()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText(/Here's what the HDFC Bank form asks for/);
    const checklistTab = within(showGroup()!).getByRole("button", { name: /Checklist/ });
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
