import { screen, within } from "@testing-library/react";
import type userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { recorded } from "../test/fixtures/assistant";
import { chat, setup, startConversation } from "../test/assistantHarness";
import { progressOf } from "./ProfilePanel";

describe("progress (derived from the API answer only)", () => {
  it("follows the known answers and the answer kind", () => {
    expect(progressOf(recorded.started().answer)).toEqual([
      "current",
      "upcoming",
      "upcoming",
      "upcoming",
    ]);
    expect(progressOf(recorded.nriQuestionScheme().answer)).toEqual([
      "done",
      "done",
      "current",
      "upcoming",
    ]);
    expect(progressOf(recorded.nriChecklist().answer)).toEqual(["done", "done", "done", "done"]);
    expect(progressOf(recorded.residentFallback().answer)).toEqual([
      "done",
      "done",
      "unavailable",
      "unavailable",
    ]);
  });

  it("marks the current step for assistive technology", async () => {
    const { user } = setup();
    await startConversation(user);
    const nav = within(await openAnswers(user)).getByRole("navigation", { name: "Progress" });
    const current = within(nav)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("aria-current") === "step");
    expect(current?.textContent).toContain("Account type");
  });
});

/** Progress and your answers live in a drawer (a side panel or a bottom sheet). */
async function openAnswers(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Your answers" }));
  return screen.getByRole("dialog", { name: "Your answers" });
}

describe("progress on a fallback", () => {
  it("says the last steps are not available for these answers", async () => {
    const { user } = setup([recorded.residentFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    await chat().findByText(/I couldn't find this information/);
    const nav = within(await openAnswers(user)).getByRole("navigation", { name: "Progress" });
    expect(within(nav).getAllByText("Not available for these answers")).toHaveLength(2);
  });
});

describe("your answers and corrections", () => {
  it("lists the answers with how they were given", async () => {
    const { user } = setup([recorded.nriConfirm(), recorded.nriQuestionScheme()]);
    await startConversation(user);
    const panel = async () =>
      within(await openAnswers(user)).getByRole("region", { name: "Your answers" });
    expect((await panel()).textContent).toContain("Your answers will appear here.");
    await user.keyboard("{Escape}");
    await user.type(
      screen.getByRole("textbox"),
      "I live in Singapore and want a savings account{Enter}",
    );
    await user.click(await screen.findByRole("button", { name: "Yes" }));
    await screen.findByRole("group", { name: /NRE or an NRO/ });
    const text = (await panel()).textContent ?? "";
    expect(text).toContain("Savings account");
    expect(text).toContain("From your message");
    expect(text).toContain("An individual living outside India (NRI / PIO / OCI)");
    expect(text).toContain("You chose this");
  });

  it("offers the slot's valid choices from the API and sends a correct action", async () => {
    const { bodies, user } = setup([recorded.llpChecklist(), recorded.llpCorrected()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText(/Here's what the HDFC Bank form asks for/);
    // The LLP checklist answer: both essential answers are known.
    const change = within(await openAnswers(user)).getByRole("button", {
      name: "Change Account holder",
    });
    expect(change.getAttribute("aria-expanded")).toBe("false");
    await user.click(change);
    const group = screen.getByRole("group", { name: "Change: Account holder" });
    const options = within(group).getAllByRole("button");
    const labels = options.map((o) => o.textContent);
    const expected = recorded
      .llpChecklist()
      .answer.profile.customer_category!.choices!.map((c) => c.label);
    expect(labels.map((l) => l?.replace(" (current answer)", ""))).toEqual(expected);
    const current = options.find((o) => o.getAttribute("aria-current") === "true")!;
    expect((current as HTMLButtonElement).disabled).toBe(true);
    await user.click(within(group).getByRole("button", { name: /A company/ }));
    expect(bodies.at(-1)).toEqual({
      action: "correct",
      slot: "customer_category",
      value: "COMPANY",
    });
    // The drawer closes on a change; the conversation shows the result.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await chat().findByText(/I've updated your answer/)).toBeTruthy();
    const region = within(await openAnswers(user)).getByRole("region", { name: "Your answers" });
    expect(region.textContent).toContain("You changed this");
  });

  it("can cancel a change without sending anything", async () => {
    const { bodies, user } = setup([recorded.residentFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    await chat().findByText(/I couldn't find this information/);
    const drawer = await openAnswers(user);
    await user.click(within(drawer).getByRole("button", { name: "Change Account type" }));
    await user.click(screen.getByRole("button", { name: "Cancel Account type" }));
    expect(screen.queryByRole("group", { name: "Change: Account type" })).toBeNull();
    expect(bodies).toHaveLength(1);
  });
});
