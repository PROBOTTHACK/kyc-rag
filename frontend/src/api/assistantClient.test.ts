import { describe, expect, it, vi } from "vitest";

import { envelope, jsonResponse } from "../test/http";
import { FAKE_SESSION_ID, recorded } from "../test/fixtures/assistant";
import { createApiClient } from "./client";
import { ApiError } from "./errors";

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
  const client = createApiClient({
    fetch: fetchFn as unknown as typeof fetch,
    sleep: async () => {},
  });
  return { client, calls };
}

const header = (call: Call, name: string) => new Headers(call.init.headers).get(name);

describe("assistant endpoints (A5 contract)", () => {
  it("start, reload and turn use /api/v2/assistant with the right method, body and key", async () => {
    const { client, calls } = scripted(
      jsonResponse(201, recorded.started()),
      jsonResponse(200, recorded.nriSessionView()),
      jsonResponse(200, recorded.nriConfirm()),
    );
    const started = await client.assistantStart();
    expect(started.answer.kind).toBe("question");
    await client.assistantGet(FAKE_SESSION_ID);
    await client.assistantTurn(FAKE_SESSION_ID, { message: "savings" }, "key-1");

    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ["POST", "/api/v2/assistant/sessions"],
      ["GET", `/api/v2/assistant/sessions/${FAKE_SESSION_ID}`],
      ["POST", `/api/v2/assistant/sessions/${FAKE_SESSION_ID}/turns`],
    ]);
    expect(calls[0]!.init.body).toBeNull();
    expect(header(calls[0]!, "Idempotency-Key")).toBeNull();
    expect(header(calls[2]!, "Idempotency-Key")).toBe("key-1");
    expect(JSON.parse(String(calls[2]!.init.body))).toEqual({ message: "savings" });
    for (const c of calls) {
      expect(c.init.referrerPolicy).toBe("no-referrer");
      expect(c.init.cache).toBe("no-store");
    }
  });

  it("retries a turn after a transient failure with the SAME key", async () => {
    const { client, calls } = scripted(
      new TypeError("network down"),
      envelope(503, "ASSISTANT_UNAVAILABLE"),
      jsonResponse(200, recorded.nriConfirm()),
    );
    await client.assistantTurn(FAKE_SESSION_ID, { message: "x" }, "same");
    expect(calls).toHaveLength(3);
    expect(calls.map((c) => header(c, "Idempotency-Key"))).toEqual(["same", "same", "same"]);
  });

  it("never retries starting a conversation (no key)", async () => {
    const { client, calls } = scripted(new TypeError("network down"));
    await expect(client.assistantStart()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(calls).toHaveLength(1);
  });

  it("turns recorded error envelopes into code-only errors", async () => {
    const { client } = scripted(
      jsonResponse(404, recorded.errors.notFound.body),
      jsonResponse(409, recorded.errors.idempotencyMismatch.body),
      jsonResponse(422, recorded.errors.validation.body),
    );
    const codes: string[] = [];
    for (const call of [
      () => client.assistantGet(FAKE_SESSION_ID),
      () => client.assistantTurn(FAKE_SESSION_ID, { message: "x" }, "k"),
      () => client.assistantTurn(FAKE_SESSION_ID, { message: "x" }, "k2"),
    ]) {
      const error = await call().catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).message).not.toContain(FAKE_SESSION_ID);
      codes.push((error as ApiError).code);
    }
    expect(codes).toEqual(["SESSION_NOT_FOUND", "IDEMPOTENCY_MISMATCH", "VALIDATION_ERROR"]);
  });

  it("encodes the session id in the path", async () => {
    const { client, calls } = scripted(jsonResponse(200, recorded.nriSessionView()));
    await client.assistantGet("a/b?c");
    expect(calls[0]!.url).toBe("/api/v2/assistant/sessions/a%2Fb%3Fc");
  });
});
