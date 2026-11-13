/** Test helpers: canned HTTP responses and a controllable SSE body (no network). */

export function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export function envelope(
  status: number,
  code: string,
  extra: Record<string, unknown> = {},
  headers: Record<string, string> = {},
) {
  return jsonResponse(status, { error: { code, message: "server text", ...extra } }, headers);
}

/** A response body the test writes to chunk by chunk, ends, or breaks. */
export class Pipe {
  readonly stream: ReadableStream<Uint8Array>;
  private controller!: ReadableStreamDefaultController<Uint8Array>;
  private readonly encoder = new TextEncoder();
  closed = false;

  constructor() {
    this.stream = new ReadableStream<Uint8Array>({
      start: (c) => {
        this.controller = c;
      },
      cancel: () => {
        this.closed = true;
      },
    });
  }

  push(text: string): void {
    if (!this.closed) this.controller.enqueue(this.encoder.encode(text));
  }

  end(): void {
    if (!this.closed) {
      this.closed = true;
      this.controller.close();
    }
  }

  fail(): void {
    if (!this.closed) {
      this.closed = true;
      this.controller.error(new TypeError("network connection lost"));
    }
  }

  response(): Response {
    return new Response(this.stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8" },
    });
  }
}

export function sseResponse(text: string): Response {
  const pipe = new Pipe();
  pipe.push(text);
  pipe.end();
  return pipe.response();
}

const AT = "2026-11-13T10:00:00+00:00";

/** One SSE frame exactly as backend/app/workflow/stream.py writes it. */
export function nodeFrame(runId: string, seq: number, node: string, status = "completed"): string {
  const data = JSON.stringify({ kind: "node", run_id: runId, seq, node, status, at: AT });
  return `id: ${runId}.${seq}\nevent: node\ndata: ${data}\n\n`;
}

export function runFrame(runId: string, seq: number, status: string): string {
  const data = JSON.stringify({
    kind: "run",
    run_id: runId,
    seq: null,
    node: null,
    status,
    at: AT,
  });
  return `id: ${runId}.${seq}.${status}\nevent: run\ndata: ${data}\n\n`;
}

/** Wait until `check` passes (polls the microtask/timer queues briefly). */
export async function until(check: () => boolean, timeoutMs = 2_000): Promise<void> {
  const start = Date.now();
  while (!check()) {
    if (Date.now() - start > timeoutMs) throw new Error("condition not reached in time");
    await new Promise((r) => setTimeout(r, 2));
  }
}
