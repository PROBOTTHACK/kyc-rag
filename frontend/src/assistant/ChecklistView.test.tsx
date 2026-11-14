import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";

import type { AssistantAnswer, AssistantItem } from "../api/types";
import { chat, setup, startConversation } from "../test/assistantHarness";
import { recorded } from "../test/fixtures/assistant";
import { ChecklistView } from "./ChecklistView";

function show(answer: AssistantAnswer) {
  const checklist = answer.checklist!;
  render(
    <ChecklistView
      checklist={checklist}
      notice={answer.notice}
      headingRef={createRef<HTMLHeadingElement>()}
    />,
  );
  return checklist;
}

const allItems = (a: AssistantAnswer): AssistantItem[] => {
  const g = a.checklist!.groups;
  return [...g.documents, ...g.information, ...g.not_confirmed];
};

describe("the checklist renders exactly what the API sent", () => {
  it("shows every item once, in the API's order, in its group", () => {
    const answer = recorded.nriChecklist().answer;
    const checklist = show(answer);
    const titles = screen.getAllByRole("heading", { level: 4 }).map((h) => h.textContent);
    expect(titles).toEqual(allItems(answer).map((i) => i.title));
    for (const [group, heading] of [
      ["documents", "Documents and declarations"],
      ["information", "Information to provide"],
    ] as const) {
      const section = screen.getByRole("region", { name: new RegExp(heading) });
      expect(within(section).getAllByRole("article")).toHaveLength(checklist.groups[group].length);
    }
    expect(screen.queryByRole("region", { name: /Could not be confirmed/ })).toBeNull();
  });

  it("never shows internal identifiers", () => {
    const answer = recorded.nriChecklist().answer;
    const { container } = render(
      <ChecklistView
        checklist={answer.checklist!}
        notice={answer.notice}
        headingRef={createRef()}
      />,
    );
    const html = container.innerHTML;
    for (const item of allItems(answer)) expect(container.textContent).not.toContain(item.id);
    expect(html).not.toContain("hdfc_nri_account_opening"); // document keys
    expect(html.toLowerCase()).not.toMatch(/chunk|score|diagnostic/);
  });

  it("quotes reliable text and shows the source document, page and passage on expand", async () => {
    const user = userEvent.setup();
    const answer = recorded.nriChecklist().answer;
    show(answer);
    const item = allItems(answer).find((i) => i.source_wording)!;
    const card = screen.getByRole("article", { name: item.title });
    await user.click(within(card).getByRole("button", { name: `Details: ${item.title}` }));
    expect(within(card).getByText(`“${item.source_wording}”`)).toBeTruthy();
    const cite = item.citations![0]!;
    const toggle = within(card).getAllByRole("button", {
      name: new RegExp(cite.document_title),
    })[0]!;
    expect(toggle.textContent).toContain(`page ${cite.page}`);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!)!;
    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain(cite.date_basis);
    expect(panel.textContent).toContain(cite.passage.slice(0, 40));
  });

  it("shows the plain condition note and the applies-if note", () => {
    const answer = recorded.nriChecklist().answer;
    show(answer);
    const withNote = allItems(answer).find((i) => i.condition_note)!;
    const card = screen.getByRole("article", { name: withNote.title });
    expect(card.textContent).toContain(withNote.condition_note);
    const joint = allItems(answer).find((i) => (i.applies_if ?? []).length > 0)!;
    expect(screen.getByRole("article", { name: joint.title }).textContent).toContain(
      joint.applies_if![0],
    );
  });

  it("presents scanned-form text as an approximate reading with the OCR warning, never quoted", async () => {
    const user = userEvent.setup();
    const answer = recorded.llpChecklist().answer;
    show(answer);
    for (const item of allItems(answer)) {
      const card = screen.getByRole("article", { name: item.title });
      expect(item.source_wording).toBeNull();
      expect(card.querySelector("blockquote")).toBeNull();
      if (item.approximate_reading) {
        expect(card.textContent).toContain(item.approximate_reading);
        expect(card.textContent).toContain(item.uncertainty_warning);
      }
    }
    const card = screen.getAllByRole("article")[0]!;
    await user.click(within(card).getByRole("button", { name: /^Details:/ }));
    await user.click(within(card).getAllByRole("button", { name: /Source:/ })[0]!);
    expect(card.textContent).toContain("Text read automatically from the scanned page");
    expect(
      screen.getByRole("region", { name: "Documents this checklist uses" }).textContent,
    ).toContain("Scanned form: the text may be inaccurate.");
  });

  it("shows Group C only when the API sends it, without citations", () => {
    const answer = recorded.llpChecklistPanCopy().answer;
    show(answer);
    const groupC = screen.getByRole("region", { name: /Could not be confirmed/ });
    const entries = answer.checklist!.groups.not_confirmed;
    expect(within(groupC).getAllByRole("article")).toHaveLength(entries.length);
    const card = within(groupC).getByRole("article", { name: entries[0]!.title });
    expect(card.textContent).toContain(entries[0]!.does_not_establish);
    expect(card.textContent).toContain("Could not be confirmed");
    expect(within(card).queryByRole("button", { name: /Source/ })).toBeNull();
  });

  it("shows the coverage note, the documents used and the dated-document notice", () => {
    const answer = recorded.nriChecklist().answer;
    const checklist = show(answer);
    const section = screen.getByRole("region", { name: "Your checklist" });
    expect(section.textContent).toContain(checklist.coverage_note);
    expect(section.textContent).toContain(answer.notice);
    for (const source of checklist.sources)
      expect(section.textContent).toContain(source.date_basis);
  });
});

describe("the document workspace", () => {
  it("shows a summary per item and hides the evidence until it is expanded", async () => {
    const user = userEvent.setup();
    const answer = recorded.nriChecklist().answer;
    show(answer);
    const item = allItems(answer).find((i) => i.source_wording)!;
    const card = screen.getByRole("article", { name: item.title });
    const cite = item.citations![0]!;
    // Always visible: the classification label and where it comes from (title and page).
    expect(card.textContent).toContain(item.classification_label);
    expect(card.textContent).toContain(`${cite.document_title} · page ${cite.page}`);
    const toggle = within(card).getByRole("button", { name: `Details: ${item.title}` });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const details = document.getElementById(toggle.getAttribute("aria-controls")!)!;
    expect(details.hidden).toBe(true);
    expect(within(card).queryByRole("button", { name: /Source:/ })).toBeNull();
    await user.click(toggle);
    expect(details.hidden).toBe(false);
    expect(details.textContent).toContain("Why this is here");
    expect(details.textContent).toContain(item.does_not_establish);
  });

  it("expands and collapses every item at once", async () => {
    const user = userEvent.setup();
    const answer = recorded.nriChecklist().answer;
    show(answer);
    await user.click(screen.getByRole("button", { name: "Expand all" }));
    const toggles = screen.getAllByRole("button", { name: /^Hide details:/ });
    expect(toggles).toHaveLength(allItems(answer).length);
    await user.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(screen.queryAllByRole("button", { name: /^Hide details:/ })).toHaveLength(0);
  });

  it("keeps a Group C explanation visible without expanding, and never as a requirement", () => {
    const answer = recorded.llpChecklistPanCopy().answer;
    show(answer);
    const entry = answer.checklist!.groups.not_confirmed[0]!;
    const card = screen.getByRole("article", { name: entry.title });
    const toggle = within(card).getByRole("button", { name: `Details: ${entry.title}` });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const summary = card.firstElementChild!.textContent ?? "";
    expect(summary).toContain(entry.does_not_establish);
    expect(summary).toContain("Could not be confirmed");
    expect(summary).not.toContain("Asked for on the HDFC Bank form");
  });

  it("flags scanned-form items in the summary", () => {
    const answer = recorded.llpChecklist().answer;
    show(answer);
    for (const card of screen.getAllByRole("article")) {
      expect(card.firstElementChild!.textContent).toContain("Scanned form");
    }
  });

  it("prints through the browser, with the evidence expanded for paper", async () => {
    const user = userEvent.setup();
    const print = vi.spyOn(window, "print").mockImplementation(() => {});
    const answer = recorded.nriChecklist().answer;
    show(answer);
    const section = screen.getByRole("region", { name: "Your checklist" });
    expect(section.getAttribute("data-print")).toBe("expand");
    await user.click(screen.getByRole("button", { name: "Print checklist" }));
    expect(print).toHaveBeenCalledTimes(1);
  });
});

describe("several citations", () => {
  it("are numbered so that identical-looking sources can be told apart", async () => {
    const user = userEvent.setup();
    const answer = recorded.llpChecklist().answer;
    show(answer);
    const item = allItems(answer).find((i) => (i.citations ?? []).length > 1)!;
    const card = screen.getByRole("article", { name: item.title });
    await user.click(within(card).getByRole("button", { name: `Details: ${item.title}` }));
    const toggles = within(card).getAllByRole("button", { name: /^Source \d:/ });
    expect(toggles.map((t) => t.textContent?.split(":")[0])).toEqual(
      item.citations!.map((_, i) => `Source ${i + 1}`),
    );
  });
});

describe("in the conversation", () => {
  it("appears when the answer is a checklist, and the bubble links to it", async () => {
    const { user } = setup([recorded.llpChecklist()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    expect(await chat().findByText("Your checklist is ready.")).toBeTruthy();
    const heading = screen.getByRole("heading", { name: "Your checklist", level: 2 });
    await user.click(chat().getByRole("button", { name: "View your checklist" }));
    await waitFor(() => expect(document.activeElement).toBe(heading));
  });

  it("is replaced when an answer changes the profile", async () => {
    const { user } = setup([recorded.llpChecklist(), recorded.llpChecklistPanCopy()]);
    await startConversation(user);
    await user.click(screen.getByRole("button", { name: /Current account/ }));
    await chat().findByText("Your checklist is ready.");
    expect(screen.queryByRole("region", { name: /Could not be confirmed/ })).toBeNull();
    await user.type(screen.getByRole("textbox"), "Do I need a copy of the PAN card?{Enter}");
    expect(await screen.findByRole("region", { name: /Could not be confirmed/ })).toBeTruthy();
    expect(chat().getAllByText("Your checklist is ready.")).toHaveLength(1);
    expect(chat().getByText("Your checklist has been updated.")).toBeTruthy();
  });
});
