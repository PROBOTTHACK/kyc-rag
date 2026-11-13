/**
 * A minimal, incremental `text/event-stream` parser (the subset the backend sends:
 * `id`, `event`, `data`, comments). Chunks may split anywhere, including inside a line or a
 * CRLF pair. No dependency; tested against split points and line-ending variants.
 *
 * Deliberate difference from the EventSource spec: a message's `id` is only the id given in
 * THAT message (null otherwise), not inherited from earlier ones. The backend sends an id
 * with every event, and the stream controller ignores id-less events, so an event can never
 * be mistaken for an earlier one or skipped as its duplicate.
 */
export interface SseMessage {
  id: string | null;
  event: string;
  data: string;
}

export class SseParser {
  private buffer = "";
  private id: string | null = null;
  private event = "";
  private data: string[] = [];
  private pendingCR = false;

  /** Feed decoded text; returns every message completed by it. */
  push(text: string): SseMessage[] {
    let chunk = text;
    if (this.pendingCR) {
      // A CR at the end of the previous chunk already ended that line; skip its LF.
      if (chunk.startsWith("\n")) chunk = chunk.slice(1);
      this.pendingCR = false;
    }
    this.buffer += chunk;
    const out: SseMessage[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const ch = this.buffer[i];
      if (ch !== "\n" && ch !== "\r") continue;
      const line = this.buffer.slice(start, i);
      if (ch === "\r") {
        if (i + 1 < this.buffer.length) {
          if (this.buffer[i + 1] === "\n") i++;
        } else {
          this.pendingCR = true;
        }
      }
      start = i + 1;
      const message = this.line(line);
      if (message) out.push(message);
    }
    this.buffer = this.buffer.slice(start);
    return out;
  }

  private line(line: string): SseMessage | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.id = null;
        this.event = "";
        return null;
      }
      const message = { id: this.id, event: this.event || "message", data: this.data.join("\n") };
      this.id = null;
      this.event = "";
      this.data = [];
      return message;
    }
    if (line.startsWith(":")) return null; // comment (keep-alive)
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    else if (field === "event") this.event = value;
    else if (field === "id" && !value.includes("\0")) this.id = value;
    return null;
  }
}
