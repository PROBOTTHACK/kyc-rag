import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { chat, setup, startConversation } from "../test/assistantHarness";
import { recorded } from "../test/fixtures/assistant";

describe("a fallback that explains and suggests (recorded from the real API)", () => {
  it("keeps the fixed fallback, adds the plain explanation and offers covered account types", async () => {
    const fallback = recorded.llpSavingsFallback().answer;
    const { user } = setup([recorded.llpSavingsFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    expect(await chat().findByText(/I couldn't find this information/)).toBeTruthy();
    expect(chat().getByText(fallback.explanation!)).toBeTruthy();
    const group = screen.getByRole("group", { name: /Covered by the documents/ });
    const labels = within(group)
      .getAllByRole("button")
      .map((b) => b.textContent);
    expect(labels).toEqual(fallback.suggestions!.map((s) => s.label));
    // The internal reason is never on the page.
    expect(document.body.textContent?.toLowerCase()).not.toContain("corpus");
  });

  it("sends a chosen suggestion as a correction and shows the checklist that follows", async () => {
    const { bodies, user } = setup([
      recorded.llpSavingsFallback(),
      recorded.llpSavingsTakeSuggestion(),
    ]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    const group = await screen.findByRole("group", { name: /Covered by the documents/ });
    await user.click(within(group).getByRole("button", { name: /Current account/ }));
    expect(bodies.at(-1)).toEqual({ action: "correct", slot: "account_type", value: "CURRENT" });
    expect(await chat().findByText(/Here's what the HDFC Bank form asks for/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Your checklist", level: 2 })).toBeTruthy();
    expect(screen.queryByRole("group", { name: /Covered by the documents/ })).toBeNull();
  });

  it("suggests the covered category for a resident savings account", async () => {
    const resident = recorded.residentFallback().answer;
    expect(resident.suggestions!.map((s) => s.value)).toEqual(["NRI_INDIVIDUAL"]);
    const { bodies, user } = setup([recorded.residentFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    expect(await chat().findByText(/converting a resident account to an NRO account/)).toBeTruthy();
    const group = screen.getByRole("group", { name: /Covered by the documents/ });
    await user.click(within(group).getByRole("button", { name: /living outside India/ }));
    expect(bodies.at(-1)).toEqual({
      action: "correct",
      slot: "customer_category",
      value: "NRI_INDIVIDUAL",
    });
  });
});
