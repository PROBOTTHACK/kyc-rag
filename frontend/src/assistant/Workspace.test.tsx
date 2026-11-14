import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { chat, setup, startConversation } from "../test/assistantHarness";
import { recorded } from "../test/fixtures/assistant";

describe("the workspace on narrow screens", () => {
  it("opens your answers in a sheet, closes it with Escape and returns focus", async () => {
    const { user } = setup([recorded.residentFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    await chat().findByText(/I couldn't find this information/);
    const opener = screen.getByRole("button", { name: "Your answers" });
    await user.click(opener);
    const sheet = screen.getByRole("dialog", { name: "Your answers" });
    expect(sheet.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(within(sheet).getByRole("button", { name: "Close" }));
    expect(sheet.textContent).toContain("An individual living in India");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("a change made in the sheet is sent as a correction and closes the sheet", async () => {
    const { bodies, user } = setup([recorded.residentFallback(), recorded.llpChecklist()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    await chat().findByText(/I couldn't find this information/);
    await user.click(screen.getByRole("button", { name: "Your answers" }));
    const sheet = screen.getByRole("dialog", { name: "Your answers" });
    await user.click(within(sheet).getByRole("button", { name: "Change Account type" }));
    await user.click(within(sheet).getByRole("button", { name: /Current account/ }));
    expect(bodies.at(-1)).toEqual({ action: "correct", slot: "account_type", value: "CURRENT" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("switches between the conversation and the checklist once a checklist exists", async () => {
    const { user } = setup([recorded.llpChecklist()]);
    await startConversation(user);
    expect(screen.queryByRole("button", { name: /^Checklist \d+$/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText(/Here's what the HDFC Bank form asks for/);
    const checklistTab = screen.getByRole("button", { name: /^Checklist \d+$/ });
    // The conversation stays in front: the chat shows the summary and an "Open checklist".
    expect(checklistTab.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByText(/Current account · A limited liability partnership/)).toBeTruthy();
    const g = recorded.llpChecklist().answer.checklist!.groups;
    expect(checklistTab.textContent).toContain(
      String(g.documents.length + g.information.length + g.not_confirmed.length),
    );
    await user.click(checklistTab);
    expect(checklistTab.getAttribute("aria-pressed")).toBe("true");
    const section = screen.getByRole("region", { name: "Conversation with the assistant" });
    expect(section.classList.contains("hidden")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Back to the conversation" }));
    expect(checklistTab.getAttribute("aria-pressed")).toBe("false");
    expect(section.classList.contains("hidden")).toBe(false);
  });
});
