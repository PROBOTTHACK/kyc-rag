import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { nodeFrame, Pipe, runFrame } from "../test/http";
import type { ApiClient } from "./client";
import { ApiContext, queryKeys, useSession, useSessionEvents } from "./hooks";

const SID = "5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f";
const RUN = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d";

function setup(pipe: Pipe) {
  const signals: AbortSignal[] = [];
  const getSession = vi.fn(async () => ({
    session_id: SID,
    status: "active" as const,
    data_class: "synthetic_demo" as const,
    expires_at: "2026-11-14T00:00:00+00:00",
    workflow: null,
  }));
  const client = {
    getSession,
    fetchEvents: async (_sid: string, _last: string | null, signal: AbortSignal) => {
      signals.push(signal);
      return pipe.response();
    },
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ApiContext.Provider value={client}>{children}</ApiContext.Provider>
    </QueryClientProvider>
  );
  return { wrapper, getSession, signals, queryClient };
}

describe("useSessionEvents", () => {
  it("refetches the session view on every run-status change and exposes progress", async () => {
    const pipe = new Pipe();
    const { wrapper, getSession } = setup(pipe);
    const { result } = renderHook(
      () => ({ session: useSession(SID), events: useSessionEvents(SID) }),
      { wrapper },
    );
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.events.status).toBe("open"));

    act(() => pipe.push(runFrame(RUN, 0, "running") + nodeFrame(RUN, 1, "input_security")));
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(2));
    act(() => pipe.push(runFrame(RUN, 1, "awaiting_clarification")));
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(3));
    expect(result.current.events.runs[0]!.status).toBe("awaiting_clarification");
    expect(result.current.events.runs[0]!.nodes).toHaveLength(1);

    act(() => pipe.push(nodeFrame(RUN, 2, "finalize"))); // node events alone do not refetch
    await waitFor(() => expect(result.current.events.runs[0]!.nodes).toHaveLength(2));
    expect(getSession).toHaveBeenCalledTimes(3);
  });

  it("closes the connection on unmount and does nothing without a session", async () => {
    const pipe = new Pipe();
    const { wrapper, signals } = setup(pipe);
    const { result, unmount } = renderHook(() => useSessionEvents(SID), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("open"));
    unmount();
    expect(signals.at(-1)!.aborted).toBe(true);

    const idle = renderHook(() => useSessionEvents(null), { wrapper });
    expect(idle.result.current.status).toBe("idle");
    expect(signals).toHaveLength(1);
  });

  it("uses the session query key the stream invalidates", () => {
    expect(queryKeys.session(SID)).toEqual(["session", SID]);
  });
});
