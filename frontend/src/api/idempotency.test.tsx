import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { ApiClient } from "./client";
import { ApiError } from "./errors";
import { ApiContext, useResume, useRetryRun, useSubmitMessage } from "./hooks";
import { fingerprint, IdempotencyTracker } from "./idempotency";

const SID = "5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f";

function counter() {
  let n = 0;
  return () => `key-${++n}`;
}

describe("IdempotencyTracker", () => {
  it("reuses the key only while the same action's outcome is unknown", async () => {
    const t = new IdempotencyTracker(counter());
    const fp = fingerprint("messages", SID, { message: "q" });
    const keys: string[] = [];
    const send = (fail?: ApiError) => (key: string) => {
      keys.push(key);
      return fail ? Promise.reject(fail) : Promise.resolve("ok");
    };
    await t.run(fp, send(new ApiError(0, "NETWORK_ERROR"))).catch(() => undefined);
    await t.run(fp, send(new ApiError(503, "WORKFLOW_UNAVAILABLE"))).catch(() => undefined);
    await t.run(fp, send()); // the retried action succeeds with its original key
    await t.run(fp, send()); // the same text again is a NEW action
    expect(keys).toEqual(["key-1", "key-1", "key-1", "key-2"]);
  });

  it("retires the key after a definitive rejection and for a different body", async () => {
    const t = new IdempotencyTracker(counter());
    const a = fingerprint("messages", SID, { message: "a" });
    const b = fingerprint("messages", SID, { message: "b" });
    expect(t.keyFor(a)).toBe("key-1");
    t.settle(new ApiError(422, "VALIDATION_ERROR"));
    expect(t.keyFor(a)).toBe("key-2");
    t.settle(new ApiError(0, "TIMEOUT"));
    expect(t.keyFor(b)).toBe("key-3"); // a different action never inherits the pending key
  });

  it("fingerprints ignore key order but not values, endpoints or sessions", () => {
    const one = { checkpoint_id: "c", answers: { a: "1", b: "2" } };
    const two = { answers: { b: "2", a: "1" }, checkpoint_id: "c" };
    expect(fingerprint("resume", SID, one)).toBe(fingerprint("resume", SID, two));
    expect(fingerprint("resume", SID, one)).not.toBe(fingerprint("resume", "other", one));
    expect(fingerprint("resume", SID, one)).not.toBe(fingerprint("messages", SID, one));
    expect(fingerprint("resume", SID, one)).not.toBe(
      fingerprint("resume", SID, { ...one, answers: { a: "1", b: "3" } }),
    );
  });
});

// --- through the React Query mutation hooks ----------------------------------------------------

function harness(outcomes: Array<"ok" | ApiError>) {
  const keys: Array<[string, string]> = [];
  const respond = (endpoint: string, key: string) => {
    keys.push([endpoint, key]);
    const next = outcomes.shift();
    return next === "ok" || next === undefined
      ? Promise.resolve({ session_id: SID, run_id: "r", status: "running" as const })
      : Promise.reject(next);
  };
  const client = {
    submitMessage: (_s: string, _b: unknown, key: string) => respond("messages", key),
    resume: (_s: string, _b: unknown, key: string) => respond("resume", key),
    retry: (_s: string, key: string) => respond("retry", key),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: 0 } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ApiContext.Provider value={client}>{children}</ApiContext.Provider>
    </QueryClientProvider>
  );
  return { keys, wrapper };
}

describe("mutation hooks", () => {
  it("a user's 'try again' of the same message reuses the key; a new message gets a new one", async () => {
    const { keys, wrapper } = harness([new ApiError(0, "NETWORK_ERROR"), "ok", "ok"]);
    const { result } = renderHook(() => useSubmitMessage(SID), { wrapper });
    await act(() => result.current.mutateAsync({ message: "LLP?" }).catch(() => undefined));
    await act(() => result.current.mutateAsync({ message: "LLP?" }));
    await act(() => result.current.mutateAsync({ message: "LLP?" }));
    const [first, second, third] = keys.map(([, k]) => k);
    expect(first).toBe(second);
    expect(third).not.toBe(second);
  });

  it("a corrected answer after a rejection is a new action", async () => {
    const { keys, wrapper } = harness([new ApiError(422, "INVALID_ANSWER"), "ok"]);
    const { result } = renderHook(() => useResume(SID), { wrapper });
    const body = { checkpoint_id: "c", answers: { account_type: "X" } };
    await act(() => result.current.mutateAsync(body).catch(() => undefined));
    await act(() =>
      result.current.mutateAsync({ ...body, answers: { account_type: "CURRENT_ACCOUNT" } }),
    );
    expect(keys[0]![1]).not.toBe(keys[1]![1]);
  });

  it("two separate retries of a run never share a key (the backend replays by key)", async () => {
    const { keys, wrapper } = harness(["ok", "ok"]);
    const { result } = renderHook(() => useRetryRun(SID), { wrapper });
    await act(() => result.current.mutateAsync());
    await act(() => result.current.mutateAsync());
    expect(keys[0]![1]).not.toBe(keys[1]![1]);
  });

  it("each action kind keeps its own pending key", async () => {
    const { keys, wrapper } = harness([new ApiError(0, "TIMEOUT"), "ok", "ok"]);
    const message = renderHook(() => useSubmitMessage(SID), { wrapper });
    const retry = renderHook(() => useRetryRun(SID), { wrapper });
    await act(() => message.result.current.mutateAsync({ message: "q" }).catch(() => undefined));
    await act(() => retry.result.current.mutateAsync());
    await act(() => message.result.current.mutateAsync({ message: "q" }));
    expect(keys[2]![1]).toBe(keys[0]![1]);
    expect(keys[1]![1]).not.toBe(keys[0]![1]);
  });
});
