import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { chat, setup, startConversation } from "../test/assistantHarness";
import { recorded } from "../test/fixtures/assistant";
import { ReplyText } from "./Transcript";

describe("the assistant's reply in the chat (recorded from the real API)", () => {
  it("renders paragraphs and bullet lines as a list", () => {
    render(<ReplyText text={["Intro line.", "- First", "- Second", "Outro."].join("\n")} />);
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "First",
      "Second",
    ]);
    expect(screen.getByText("Intro line.")).toBeTruthy();
    expect(screen.getByText("Outro.")).toBeTruthy();
  });

  it("summarises the checklist in the chat with the API's item titles", async () => {
    const answer = recorded.llpChecklist().answer;
    const { user } = setup([recorded.llpChecklist()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    const intro = await chat().findByText(/Here's what the HDFC Bank form asks for/);
    const bubble = intro.closest("div.rounded-2xl")!;
    for (const item of answer.checklist!.groups.documents) {
      expect(
        within(bubble as HTMLElement).getByText(new RegExp(item.title.slice(0, 20))),
      ).toBeTruthy();
    }
    expect(bubble.textContent).toContain("form page it comes from");
  });

  it("does not repeat the question text: the reply carries it", async () => {
    const { user } = setup([recorded.nriConfirm(), recorded.nriQuestionScheme()]);
    await startConversation(user);
    await user.type(
      screen.getByRole("textbox"),
      "I live in Singapore and want a savings account{Enter}",
    );
    await user.click(await screen.findByRole("button", { name: "Yes" }));
    await screen.findByRole("group", { name: /NRE or an NRO/ });
    expect(chat().getAllByText(/Is it an NRE or an NRO account\?/)).toHaveLength(1);
  });

  it("an off-topic message keeps the fixed fallback and never claims an update", async () => {
    const answer = recorded.afterChecklistBleeh().answer;
    expect(answer.kind).toBe("checklist");
    const { user } = setup([recorded.llpChecklist(), recorded.afterChecklistBleeh()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText(/Here's what the HDFC Bank form asks for/);
    await user.type(screen.getByRole("textbox"), "bleeh{Enter}");
    expect(await chat().findByText(answer.reply!)).toBeTruthy();
    expect(chat().getAllByText(answer.fallback!).length).toBeGreaterThan(0);
    expect(chat().queryByText(/has been updated/)).toBeNull();
  });

  it("answers thanks without repeating the fixed fallback already shown", async () => {
    const answer = recorded.contextThanksFallback().answer;
    expect(answer.fallback).toBeTruthy(); // the answer still carries it
    const { user } = setup([recorded.residentFallback(), recorded.contextThanksFallback()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Savings account/ }));
    await chat().findAllByText(answer.fallback!);
    const before = chat().getAllByText(answer.fallback!).length;
    await user.type(screen.getByRole("textbox"), "thanks{Enter}");
    expect(await chat().findByText(/You're welcome!/)).toBeTruthy();
    expect(chat().getAllByText(answer.fallback!)).toHaveLength(before);
  });
});
