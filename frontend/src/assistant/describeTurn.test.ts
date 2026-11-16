import { describe, expect, it } from "vitest";

import type { AssistantTurnRequest } from "../api/types";
import { recorded } from "../test/fixtures/assistant";
import { describeTurn } from "./useConversation";

// A7.6 (found by the recorded demo): a scenario-offer choice showed an empty customer bubble
// ("Ask about a separate account" has no value) or its raw value ("0" for "Yes, go back to
// it"). The bubble now reads as the option the customer clicked. Recorded API answers.
const body = (b: Record<string, unknown>) => b as AssistantTurnRequest;

describe("describeTurn", () => {
  it("names a separate-account choice by its label, not an empty bubble", () => {
    const offer = recorded.contextOffer().answer;
    expect(describeTurn(body({ action: "new_scenario" }), offer)).toBe(
      "Ask about a separate account",
    );
    expect(describeTurn(body({ action: "change_application" }), offer)).toBe(
      "Change this application",
    );
  });

  it("names going back to an earlier account by its label, never the raw index", () => {
    const offer = recorded.contextSwitchOffer().answer;
    expect(describeTurn(body({ action: "switch_scenario", value: "0" }), offer)).toBe(
      "Yes, go back to it",
    );
    expect(describeTurn(body({ action: "confirm_no" }), offer)).toBe("No, stay with this one");
  });

  it("names a switch from 'Other accounts' after the account it returns to", () => {
    const separate = recorded.contextSeparate().answer;
    expect(describeTurn(body({ action: "switch_scenario", value: "0" }), separate)).toBe(
      "Go back to a current account for a company (private or public limited)",
    );
  });

  it("keeps the existing labels for messages, chips and confirmations", () => {
    expect(describeTurn(body({ message: "Hi" }), null)).toBe("Hi");
    expect(describeTurn(body({ action: "confirm_yes" }), null)).toBe("Yes");
    const q = recorded.nriQuestionHolding().answer;
    const chip = q.question!.chips[0]!;
    expect(describeTurn(body({ action: "choose", slot: "holding", value: chip.value }), q)).toBe(
      chip.label,
    );
  });
});
