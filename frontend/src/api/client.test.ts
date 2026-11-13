import { describe, expect, it, vi } from "vitest";

import { envelope, jsonResponse } from "../test/http";
import { createApiClient } from "./client";
import { ApiError, messageFor } from "./errors";

const SID = "5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f";
const PAN = "ABCPE1234F";

type Call = { url: string; init: RequestInit };

function scripted(...responses: Array<Response | Error>) {
  const calls: Call[] = [];
  const fetchFn = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const next = responses.shift();
    if (next === undefined) throw new Error("unexpected extra request");
    if (next instanceof Error) throw next;
    return next;
  });
  const sleeps: number[] = [];
  const client = createApiClient({
    fetch: fetchFn as unknown as typeof fetch,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
  });
  return { client, calls, sleeps };
}

const header = (call: Call, name: string) => new Headers(call.init.headers).get(name);
const accepted = { session_id: SID, run_id: "r", status: "running" };

describe("endpoints", () => {
  it("call only the existing routes with the right method, body and headers", async () => {
    const { client, calls } = scripted(
      jsonResponse(201, { session_id: SID }),
      jsonResponse(200, { session_id: SID, workflow: null }),
      jsonResponse(202, accepted),
      jsonResponse(202, accepted),
      jsonResponse(202, accepted),
      jsonResponse(200, { report_id: "x" }),
      jsonResponse(200, { report_id: "x", review_cases: [] }),
    );
    await client.createSession();
    await client.getSession(SID);
    await client.submitMessage(SID, { message: "LLP current account?" }, "k-msg");
    await client.resume(
      SID,
      { checkpoint_id: "c", answers: { account_type: "CURRENT_ACCOUNT" } },
      "k-res",
    );
    await client.retry(SID, "k-ret");
    await client.getReport("a/b");
    await client.getReviewCases("a/b");

    expect(calls.map((c) => `${c.init.method} ${c.url}`)).toEqual([
      "POST /api/v1/sessions",
      `GET /api/v1/sessions/${SID}`,
      `POST /api/v1/sessions/${SID}/messages`,
      `POST /api/v1/sessions/${SID}/resume`,
      `POST /api/v1/sessions/${SID}/retry`,
      "GET /api/v1/reports/a%2Fb",
      "GET /api/v1/reports/a%2Fb/review-cases",
    ]);
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ data_class: "synthetic_demo" });
    expect(header(calls[0]!, "Idempotency-Key")).toBeNull();
    expect([2, 3, 4].map((i) => header(calls[i]!, "Idempotency-Key"))).toEqual([
      "k-msg",
      "k-res",
      "k-ret",
    ]);
    expect(JSON.parse(calls[4]!.init.body as string)).toEqual({});
    for (const call of calls) {
      expect(call.init.credentials).toBe("same-origin");
      expect(call.init.signal).toBeInstanceOf(AbortSignal); // every call has a timeout
    }
  });

  it("health returns the payload for 503 degraded instead of throwing", async () => {
    const degraded = {
      status: "degraded",
      checks: { redis: { ok: false, latency_ms: 2, error: "X" } },
    };
    const { client } = scripted(jsonResponse(503, degraded));
    await expect(client.health()).resolves.toEqual(degraded);
  });
});

describe("errors", () => {
  it("map the envelope to codes and fixed messages, never the server's text", async () => {
    const { client } = scripted(
      envelope(422, "SENSITIVE_DATA_DETECTED", { categories: ["pan", "<script>"] }),
    );
    const error = await client
      .submitMessage(SID, { message: `my PAN is ${PAN}` }, "k")
      .catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(422);
    expect(error.code).toBe("SENSITIVE_DATA_DETECTED");
    expect(error.categories).toEqual(["pan"]); // only code-shaped values are kept
    expect(error.userMessage).toBe(messageFor("SENSITIVE_DATA_DETECTED"));
    expect(error.userMessage).not.toContain("server text");
  });

  it("never carry the request body or user input", async () => {
    const { client } = scripted(envelope(422, "INPUT_REJECTED"));
    const error = await client.submitMessage(SID, { message: `PAN ${PAN}` }, "k").catch((e) => e);
    const everything = JSON.stringify(error) + String(error) + error.stack + error.userMessage;
    expect(everything).not.toContain(PAN);
  });

  it("read retry_after from the envelope or the Retry-After header", async () => {
    const { client } = scripted(
      envelope(409, "RUN_IN_PROGRESS", { retry_after: 60 }),
      envelope(409, "RUN_IN_PROGRESS", {}, { "Retry-After": "7" }),
    );
    expect((await client.retry(SID, "k1").catch((e) => e)).retryAfter).toBe(60);
    expect((await client.retry(SID, "k2").catch((e) => e)).retryAfter).toBe(7);
  });

  it("treat an unknown or malformed code as UNKNOWN_ERROR", async () => {
    const { client } = scripted(envelope(400, "not a code; <b>x</b>"));
    const error = await client.getSession(SID).catch((e) => e);
    expect(error.code).toBe("UNKNOWN_ERROR");
    expect(error.userMessage).toBe(messageFor("UNKNOWN_ERROR"));
  });

  it("a non-JSON gateway error is BAD_RESPONSE and transient", async () => {
    const html = () => new Response("<html>Bad Gateway</html>", { status: 502 });
    const { client, calls } = scripted(html(), html(), html());
    const error = await client.getSession(SID).catch((e) => e);
    expect([error.code, error.status, error.transient]).toEqual(["BAD_RESPONSE", 502, true]);
    expect(calls).toHaveLength(3);
  });
});

describe("bounded retries", () => {
  it("retry a transient failure with the SAME Idempotency-Key", async () => {
    const { client, calls, sleeps } = scripted(
      new TypeError("fetch failed"),
      envelope(503, "WORKFLOW_UNAVAILABLE"),
      jsonResponse(202, accepted),
    );
    await expect(client.submitMessage(SID, { message: "q" }, "same-key")).resolves.toEqual(
      accepted,
    );
    expect(calls.map((c) => header(c, "Idempotency-Key"))).toEqual([
      "same-key",
      "same-key",
      "same-key",
    ]);
    expect(sleeps).toEqual([500, 1_000]);
  });

  it("stop after three attempts with NETWORK_ERROR", async () => {
    const down = () => new TypeError("fetch failed");
    const { client, calls } = scripted(down(), down(), down());
    const error = await client.getSession(SID).catch((e) => e);
    expect([error.code, error.status, error.outcomeUnknown]).toEqual(["NETWORK_ERROR", 0, true]);
    expect(calls).toHaveLength(3);
  });

  it("never retry a definitive rejection or a server error", async () => {
    const one = scripted(envelope(409, "STALE_CHECKPOINT"));
    await one.client.resume(SID, { checkpoint_id: "c", answers: {} }, "k").catch(() => undefined);
    expect(one.calls).toHaveLength(1);
    const two = scripted(envelope(500, "INTERNAL_ERROR"));
    const error = await two.client.submitMessage(SID, { message: "q" }, "k").catch((e) => e);
    expect(two.calls).toHaveLength(1);
    expect(error.outcomeUnknown).toBe(true); // a manual retry must reuse the key
  });

  it("never retry POST /sessions (it has no Idempotency-Key)", async () => {
    const { client, calls } = scripted(new TypeError("fetch failed"));
    await expect(client.createSession()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(calls).toHaveLength(1);
  });

  it("time out a request that never answers", async () => {
    const hanging = vi.fn(
      (_url: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) =>
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          ),
        ),
    );
    const client = createApiClient({
      fetch: hanging as unknown as typeof fetch,
      timeoutMs: 20,
      sleep: async () => undefined,
    });
    await expect(client.getReport("r")).rejects.toMatchObject({ code: "TIMEOUT", status: 0 });
    expect(hanging).toHaveBeenCalledTimes(3);
  });

  it("let a caller's cancellation through without retrying", async () => {
    const controller = new AbortController();
    const { client, calls } = scripted(new DOMException("aborted", "AbortError"));
    controller.abort();
    await expect(client.getSession(SID, controller.signal)).rejects.not.toBeInstanceOf(ApiError);
    expect(calls).toHaveLength(1);
  });
});
