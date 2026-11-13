import { describe, expect, it } from "vitest";

import { nodeFrame, runFrame } from "../test/http";
import { SseParser } from "./sse";

const RUN = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d";
const STREAM =
  runFrame(RUN, 0, "running") + ": keep-alive\n\n" + nodeFrame(RUN, 1, "input_security");

function parseAll(chunks: string[]) {
  const parser = new SseParser();
  return chunks.flatMap((c) => parser.push(c));
}

describe("SseParser", () => {
  it("parses the backend's frames and skips keep-alive comments", () => {
    const messages = parseAll([STREAM]);
    expect(messages.map((m) => [m.id, m.event])).toEqual([
      [`${RUN}.0.running`, "run"],
      [`${RUN}.1`, "node"],
    ]);
    expect(JSON.parse(messages[1]!.data)).toMatchObject({ kind: "node", seq: 1 });
  });

  it("gives the same result for every split point", () => {
    const whole = parseAll([STREAM]);
    for (let i = 1; i < STREAM.length; i++) {
      expect(parseAll([STREAM.slice(0, i), STREAM.slice(i)])).toEqual(whole);
    }
  });

  it("handles CRLF and CR line endings, including a CRLF split across chunks", () => {
    const whole = parseAll([STREAM]);
    const crlf = STREAM.replaceAll("\n", "\r\n");
    expect(parseAll([crlf])).toEqual(whole);
    expect(parseAll([STREAM.replaceAll("\n", "\r")])).toEqual(whole);
    for (let i = 1; i < crlf.length; i++) {
      expect(parseAll([crlf.slice(0, i), crlf.slice(i)])).toEqual(whole);
    }
  });

  it("joins multi-line data and holds an unfinished message until its blank line", () => {
    const parser = new SseParser();
    expect(parser.push("id: 1\nevent: x\ndata: a\ndata: b\n")).toEqual([]);
    expect(parser.push("\n")).toEqual([{ id: "1", event: "x", data: "a\nb" }]);
  });
});
