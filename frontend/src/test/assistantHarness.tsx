import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { expect, vi } from "vitest";

import type { ApiClient } from "../api/client";
import { ApiError } from "../api/errors";
import { ApiContext } from "../api/hooks";
import type { AssistantSessionView, AssistantTurnResult } from "../api/types";
import { App } from "../App";
import { ASSISTANT_STORAGE_KEY } from "../storage/assistantSession";
import { recorded } from "../test/fixtures/assistant";

/** Shared helpers for the `/open-account` tests (not a test file itself). */

type Reply = AssistantTurnResult | ApiError;

/** Text content check without extra matcher libraries. */
export function expectText(el: Element | null, expected: string | RegExp) {
  const text = el?.textContent ?? "";
  if (typeof expected === "string") expect(text).toContain(expected);
  else expect(text).toMatch(expected);
}

export function setup(
  replies: Reply[] = [],
  options: { view?: AssistantSessionView | ApiError; stored?: string } = {},
) {
  const keys: string[] = [];
  const bodies: unknown[] = [];
  const client = {
    assistantStart: vi.fn(async () => recorded.started()),
    assistantGet: vi.fn(async () => {
      const view = options.view ?? recorded.nriSessionView();
      if (view instanceof ApiError) throw view;
      return structuredClone(view);
    }),
    assistantTurn: vi.fn(async (_sid: string, body: unknown, key: string) => {
      keys.push(key);
      bodies.push(body);
      const next = replies.shift();
      if (next === undefined) throw new Error("unexpected turn");
      if (next instanceof ApiError) throw next;
      return structuredClone(next);
    }),
  } as unknown as ApiClient;
  if (options.stored) window.sessionStorage.setItem(ASSISTANT_STORAGE_KEY, options.stored);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={queryClient}>
      <ApiContext.Provider value={client}>
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </ApiContext.Provider>
    </QueryClientProvider>,
  );
  return { client, keys, bodies, user };
}

export async function startConversation(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /I understand — start/ }));
  await screen.findByRole("group", { name: /Which account would you like/ });
}

/** The visible conversation (the live region repeats the latest answer for screen readers). */
export const chat = () => within(screen.getByRole("list", { name: "Conversation" }));
