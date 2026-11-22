/**
 * Minimal server-sent events reader for fetch() streams (the run endpoints are POST, so
 * EventSource cannot be used). Only `data:` fields matter: the API sends one compact JSON
 * object per event and no `event:` or `id:` lines.
 */

/** Incremental parser: feed it text chunks in any split; it calls `onData` once per event. */
export class SseParser {
  private buffer = "";
  private data: string[] = [];

  constructor(private readonly onData: (data: string) => void) {}

  feed(chunk: string): void {
    this.buffer += chunk;
    let nl: number;
    while ((nl = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      // A lone "\r" at the very end may be the first half of "\r\n": wait for the next chunk.
      if (this.buffer[nl] === "\r" && nl === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, nl);
      const width = this.buffer.startsWith("\r\n", nl) ? 2 : 1;
      this.buffer = this.buffer.slice(nl + width);
      this.line(line);
    }
  }

  /** Flushes a final event that was not followed by a blank line. */
  end(): void {
    if (this.buffer) {
      this.line(this.buffer.replace(/\r$/, ""));
      this.buffer = "";
    }
    this.dispatch();
  }

  private line(line: string): void {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) return; // comment / keep-alive
    const i = line.indexOf(":");
    const field = i < 0 ? line : line.slice(0, i);
    let value = i < 0 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
  }

  private dispatch(): void {
    if (this.data.length === 0) return;
    const data = this.data.join("\n");
    this.data = [];
    this.onData(data);
  }
}

/** Reads a byte stream to the end and yields each event's data string. */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const ready: string[] = [];
  const parser = new SseParser((d) => ready.push(d));
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
      while (ready.length) yield ready.shift()!;
    }
    parser.feed(decoder.decode());
    parser.end();
    while (ready.length) yield ready.shift()!;
  } finally {
    reader.releaseLock();
  }
}
