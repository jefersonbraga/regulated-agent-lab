import { describe, expect, it } from "vitest";
import { readSse, SseParser } from "./sse";

function parseAll(chunks: string[]): string[] {
  const out: string[] = [];
  const p = new SseParser((d) => out.push(d));
  for (const c of chunks) p.feed(c);
  p.end();
  return out;
}

function streamOf(chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(typeof c === "string" ? enc.encode(c) : c);
      controller.close();
    },
  });
}

describe("SseParser", () => {
  const body = 'data: {"type":"run_started","run_id":"r1"}\n\ndata: {"type":"status","status":"running"}\n\n';
  const expected = ['{"type":"run_started","run_id":"r1"}', '{"type":"status","status":"running"}'];

  it("yields one data string per event", () => {
    expect(parseAll([body])).toEqual(expected);
  });

  it("gives the same events for every possible chunk boundary", () => {
    for (let i = 0; i <= body.length; i++) {
      expect(parseAll([body.slice(0, i), body.slice(i)])).toEqual(expected);
    }
    // One character at a time.
    expect(parseAll([...body])).toEqual(expected);
  });

  it("accepts CRLF and CR line endings, split across chunks", () => {
    expect(parseAll(["data: a\r", "\n\r", "\ndata: b\r\r"])).toEqual(["a", "b"]);
  });

  it("joins multi-line data, skips comments and other fields, and flushes a final event", () => {
    expect(parseAll([": keep-alive\n", "event: x\nid: 1\ndata: one\ndata: two\n\n", "data:three"])).toEqual([
      "one\ntwo",
      "three",
    ]);
  });
});

describe("readSse", () => {
  it("decodes UTF-8 split inside a multi-byte character", async () => {
    const bytes = new TextEncoder().encode('data: {"t":"ZUE ⇄ BER"}\n\n');
    const cut = bytes.indexOf(0xe2) + 1; // inside the 3-byte arrow
    const out: string[] = [];
    for await (const d of readSse(streamOf([bytes.slice(0, cut), bytes.slice(cut)]))) out.push(d);
    expect(out).toEqual(['{"t":"ZUE ⇄ BER"}']);
  });
});
