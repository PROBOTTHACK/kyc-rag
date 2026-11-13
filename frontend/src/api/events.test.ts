import { describe, expect, it } from "vitest";

import { live } from "../test/fixtures/live";
import recordedCompletedStream from "../test/fixtures/live/events_completed.txt?raw";
import { envelope, nodeFrame, Pipe, runFrame, sseResponse, until } from "../test/http";
import { MAX_CONSECUTIVE_FAILURES, SessionEventStream, type StreamDeps } from "./events";

const SID = "5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f";
const A = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d";
const B = "1b2c3d4e-5f60-4b7c-8d9e-0f1a2b3c4d5e";

type Handler = (lastEventId: string | null, signal: AbortSignal) => Response | Promise<Response>;

/** fetchEvents answering each connection from a script; records the Last-Event-ID sent. */
function server(...handlers: Handler[]) {
  const sent: Array<string | null> = [];
  const delays: number[] = [];
  const statuses: string[] = [];
  const deps: StreamDeps = {
    fetchEvents: async (sessionId, lastEventId, signal) => {
      expect(sessionId).toBe(SID);
      sent.push(lastEventId);
      const handler = handlers.shift();
      if (handler === undefined) throw new Error("unexpected extra connection");
      return handler(lastEventId, signal);
    },
    sleep: async (ms) => {
      delays.push(ms);
    },
    onRunStatus: (runId, status) => statuses.push(`${runId === A ? "A" : "B"}:${status}`),
  };
  return { deps, sent, delays, statuses };
}

const fullRunA =
  runFrame(A, 0, "running") +
  nodeFrame(A, 1, "input_security") +
  nodeFrame(A, 2, "entity_intake") +
  nodeFrame(A, 3, "finalize") +
  runFrame(A, 3, "completed");

describe("SessionEventStream", () => {
  it("replays a finished run and stops when the server closes on a terminal status", async () => {
    const s = server(() => sseResponse(fullRunA));
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    const snap = stream.getSnapshot();
    expect(snap.status).toBe("closed");
    expect(snap.runs).toHaveLength(1);
    expect(snap.runs[0]!.status).toBe("completed");
    expect(snap.runs[0]!.nodes.map((n) => n.node)).toEqual([
      "input_security",
      "entity_intake",
      "finalize",
    ]);
    expect(snap.lastEventId).toBe(`${A}.3.completed`);
    expect(s.sent).toEqual([null]); // one connection; no reconnect after a terminal close
    expect(s.statuses).toEqual(["A:running", "A:completed"]);
  });

  it("reconnects with Last-Event-ID when a non-terminal stream ends, and ignores replayed events", async () => {
    const s = server(
      // first connection: paused for clarification, then the server's idle timeout ends it
      () =>
        sseResponse(
          runFrame(A, 0, "running") +
            nodeFrame(A, 1, "input_security") +
            runFrame(A, 1, "awaiting_clarification"),
        ),
      // second connection: a replay overlapping what was already applied, then completion
      () =>
        sseResponse(
          nodeFrame(A, 1, "input_security") +
            runFrame(A, 1, "running") +
            nodeFrame(A, 2, "finalize") +
            runFrame(A, 2, "completed"),
        ),
    );
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    expect(s.sent).toEqual([null, `${A}.1.awaiting_clarification`]);
    const run = stream.getSnapshot().runs[0]!;
    expect(run.nodes.map((n) => n.seq)).toEqual([1, 2]); // no duplicate seq 1
    expect(run.status).toBe("completed"); // current status only: the latest wins
    expect(s.statuses).toEqual([
      "A:running",
      "A:awaiting_clarification",
      "A:running",
      "A:completed",
    ]);
    expect(stream.getSnapshot().status).toBe("closed");
    expect(s.delays).toEqual([1_000]);
  });

  it("reconnects after a dropped connection and resumes from the last event", async () => {
    const pipe = new Pipe();
    const s = server(
      () => pipe.response(),
      () => sseResponse(nodeFrame(A, 2, "finalize") + runFrame(A, 2, "completed")),
    );
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    pipe.push(runFrame(A, 0, "running") + nodeFrame(A, 1, "input_security"));
    await until(() => stream.getSnapshot().lastEventId === `${A}.1`);
    pipe.fail();
    await stream.settled();
    expect(s.sent).toEqual([null, `${A}.1`]);
    expect(stream.getSnapshot().status).toBe("closed");
    expect(stream.getSnapshot().runs[0]!.nodes).toHaveLength(2);
  });

  it("bounds consecutive failures with backoff, then follow() starts over", async () => {
    const down: Handler = () => Promise.reject(new TypeError("fetch failed"));
    const s = server(...Array.from({ length: MAX_CONSECUTIVE_FAILURES }, () => down), () =>
      sseResponse(fullRunA),
    );
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    const snap = stream.getSnapshot();
    expect(snap.status).toBe("error");
    expect(snap.error?.code).toBe("NETWORK_ERROR");
    expect(s.sent).toHaveLength(MAX_CONSECUTIVE_FAILURES);
    expect(s.delays).toEqual([1_000, 2_000, 4_000, 8_000]);
    stream.follow(); // a manual reconnect
    await stream.settled();
    expect(stream.getSnapshot().status).toBe("closed");
  });

  it("retries a 5xx but stops on a 4xx without retrying", async () => {
    const one = server(
      () => envelope(503, "WORKFLOW_UNAVAILABLE"),
      () => sseResponse(fullRunA),
    );
    const ok = new SessionEventStream(SID, one.deps);
    ok.follow();
    await ok.settled();
    expect([ok.getSnapshot().status, one.sent.length]).toEqual(["closed", 2]);

    const two = server(() => envelope(404, "SESSION_NOT_FOUND"));
    const gone = new SessionEventStream(SID, two.deps);
    gone.follow();
    await gone.settled();
    expect(gone.getSnapshot().status).toBe("error");
    expect(gone.getSnapshot().error?.code).toBe("SESSION_NOT_FOUND");
    expect(two.sent).toHaveLength(1);
  });

  it("refuses a response that is not an event stream", async () => {
    const s = server(
      () =>
        new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } }),
    );
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    expect(stream.getSnapshot().error?.code).toBe("BAD_RESPONSE");
  });

  it("drops a silent connection and reconnects; keep-alive comments keep it open", async () => {
    const silent = new Pipe();
    const lively = new Pipe();
    const s = server(
      () => silent.response(),
      () => lively.response(),
    );
    const stream = new SessionEventStream(SID, { ...s.deps, silenceTimeoutMs: 40 });
    stream.follow();
    await until(() => s.sent.length === 2); // the silent one was aborted by the watchdog
    for (let i = 0; i < 4; i++) {
      lively.push(": keep-alive\n\n");
      await new Promise((r) => setTimeout(r, 15));
    }
    lively.push(fullRunA);
    lively.end();
    await stream.settled();
    expect(s.sent).toHaveLength(2); // no third connection: keep-alives counted as life
    expect(stream.getSnapshot().status).toBe("closed");
  });

  it("follow() after a closed run continues into the next run from the last event id", async () => {
    const s = server(
      () => sseResponse(fullRunA),
      () =>
        sseResponse(
          runFrame(B, 0, "running") + nodeFrame(B, 1, "input_security") + runFrame(B, 1, "failed"),
        ),
    );
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    stream.follow(); // e.g. after the user asks another question
    await stream.settled();
    expect(s.sent).toEqual([null, `${A}.3.completed`]);
    expect(stream.getSnapshot().runs.map((r) => [r.runId, r.status])).toEqual([
      [A, "completed"],
      [B, "failed"],
    ]);
    expect(stream.getSnapshot().status).toBe("closed");
  });

  it("stays open with no run yet, and stop() releases the connection", async () => {
    const pipe = new Pipe();
    const s = server(() => pipe.response());
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await until(() => stream.getSnapshot().status === "open");
    stream.stop();
    await until(() => pipe.closed);
    expect(stream.getSnapshot().status).toBe("idle");
    expect(s.sent).toHaveLength(1);
  });

  it("replays a stream recorded from the real backend to a terminal close", async () => {
    const recorded = live.sessionCompleted().workflow!;
    const s = server(() => sseResponse(recordedCompletedStream));
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    const snap = stream.getSnapshot();
    expect(snap.status).toBe("closed");
    expect(s.sent).toEqual([null]); // closed on the terminal status: no reconnect
    expect(snap.runs).toHaveLength(1);
    expect(snap.runs[0]!.runId).toBe(recorded.run_id);
    expect(snap.runs[0]!.status).toBe("completed");
    // the replay carries exactly the durable node events of the session view
    expect(snap.runs[0]!.nodes.map((n) => [n.seq, n.node, n.status])).toEqual(
      recorded.node_events.map((e) => [e.seq, e.node, e.status]),
    );
  });

  it("ignores events outside the contract", async () => {
    const bad =
      `id: x.1\nevent: node\ndata: {not json\n\n` +
      `id: x.2\nevent: run\ndata: ${JSON.stringify({ kind: "node", run_id: A, seq: 1, node: "n", status: "completed", at: "t" })}\n\n` +
      `event: node\ndata: ${JSON.stringify({ kind: "node", run_id: A, seq: 9, node: "n", status: "completed", at: "t" })}\n\n` +
      fullRunA;
    const s = server(() => sseResponse(bad));
    const stream = new SessionEventStream(SID, s.deps);
    stream.follow();
    await stream.settled();
    expect(stream.getSnapshot().runs[0]!.nodes.map((n) => n.seq)).toEqual([1, 2, 3]);
  });
});
