import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  cancelPayment,
  getMe,
  getQuote,
  listRuns,
  login,
  logout,
  refreshPayment,
  streamApproval,
  streamRun,
} from "./api";
import { approveEvents, awaiting, completed, createEvents, quote, sseBody } from "./fixtures/events";
import type { RunEvent } from "./types";

function sse(events: RunEvent[] | string, status = 200): Response {
  const text = typeof events === "string" ? events : sseBody(events);
  return new Response(text, { status, headers: { "Content-Type": "text/event-stream" } });
}
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

const fetchMock = vi.fn<typeof fetch>();
vi.stubGlobal("fetch", fetchMock);
afterEach(() => fetchMock.mockReset());

const url = (i: number) => String(fetchMock.mock.calls[i][0]);

describe("streamRun", () => {
  it("hands every server event on and resolves with the final view", async () => {
    fetchMock.mockResolvedValueOnce(sse(createEvents));
    const seen: RunEvent[] = [];
    const view = await streamRun("trip", (e) => seen.push(e));
    expect(url(0)).toMatch(/\/runs\/stream$/);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST", body: JSON.stringify({ message: "trip" }) });
    expect(seen).toEqual(createEvents);
    expect(view).toEqual(awaiting);
  });

  it("falls back to POST /runs and replays the view when there is no stream endpoint", async () => {
    fetchMock.mockResolvedValueOnce(new Response("", { status: 404 })).mockResolvedValueOnce(json(awaiting));
    const seen: RunEvent[] = [];
    const view = await streamRun("trip", (e) => seen.push(e));
    expect(url(1)).toMatch(/\/runs$/);
    expect(view).toEqual(awaiting);
    expect(seen[0]).toEqual({ type: "run_started", run_id: awaiting.run_id });
    expect(seen[seen.length - 1]).toEqual({ type: "run_view", ...awaiting });
  });

  it("throws a stream error event as ApiError after passing it on", async () => {
    fetchMock.mockResolvedValueOnce(
      sse([{ type: "run_started", run_id: "r9" }, { type: "error", error_type: "Boom" }]),
    );
    const seen: RunEvent[] = [];
    const err = await streamRun("trip", (e) => seen.push(e)).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("Boom");
    expect(seen[seen.length - 1]).toEqual({ type: "error", error_type: "Boom" });
    // Never resent: one request only.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reads the outcome when the connection drops after the run started", async () => {
    fetchMock
      .mockResolvedValueOnce(sse(createEvents.slice(0, 5)))
      .mockResolvedValueOnce(json(awaiting));
    const seen: RunEvent[] = [];
    const view = await streamRun("trip", (e) => seen.push(e));
    expect(url(1)).toMatch(/\/runs\/demo-run$/);
    expect(view).toEqual(awaiting);
    expect(seen[seen.length - 1]).toEqual({ type: "run_view", ...awaiting });
  });

  it("treats a 4xx answer as final", async () => {
    fetchMock.mockResolvedValueOnce(new Response("bad", { status: 422, statusText: "Unprocessable" }));
    await expect(streamRun("", () => {})).rejects.toMatchObject({ status: 422 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("streamApproval", () => {
  const body = { approved: true, approver: "a", quote_hash: "h" };

  it("streams the resumed run", async () => {
    fetchMock.mockResolvedValueOnce(sse(approveEvents));
    const view = await streamApproval("demo-run", body, () => {});
    expect(url(0)).toMatch(/\/runs\/demo-run\/approval\/stream$/);
    expect(view).toEqual(completed);
  });

  it("never retries a 409", async () => {
    fetchMock.mockResolvedValueOnce(new Response("not awaiting approval", { status: 409 }));
    await expect(streamApproval("demo-run", body, () => {})).rejects.toMatchObject({ status: 409 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("falls back to the plain endpoint and replays only the new steps", async () => {
    fetchMock.mockResolvedValueOnce(new Response("", { status: 405 })).mockResolvedValueOnce(json(completed));
    const seen: RunEvent[] = [];
    await streamApproval("demo-run", body, (e) => seen.push(e), awaiting);
    expect(url(1)).toMatch(/\/runs\/demo-run\/approval$/);
    const tools = seen.flatMap((e) => (e.type === "tool_call" ? [e.tool] : []));
    expect(tools).toEqual(["travel_search.hold_seat", "travel_search.hold_seat", "payments.charge"]);
  });

  it("sends the chosen fare ids with the hash", async () => {
    fetchMock.mockResolvedValueOnce(sse(approveEvents));
    await streamApproval("demo-run", { ...body, fare_ids: ["a", "b"] }, () => {});
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ ...body, fare_ids: ["a", "b"] });
  });
});

describe("getQuote", () => {
  it("posts the selection and returns the server's quote", async () => {
    fetchMock.mockResolvedValueOnce(json(quote));
    expect(await getQuote("demo-run", ["a", "b"])).toEqual(quote);
    expect(url(0)).toMatch(/\/runs\/demo-run\/quote$/);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST", body: JSON.stringify({ fare_ids: ["a", "b"] }) });
  });

  it("throws the 422 of a selection outside the offer", async () => {
    fetchMock.mockResolvedValueOnce(new Response("invalid selection", { status: 422, statusText: "Unprocessable" }));
    await expect(getQuote("demo-run", ["x"])).rejects.toMatchObject({ status: 422 });
  });
});

describe("listRuns", () => {
  it("reads GET /runs with the limit and returns the rows as sent", async () => {
    const rows = [{ run_id: "r1", status: "completed", total_cents: 5600 }];
    fetchMock.mockResolvedValueOnce(json(rows));
    expect(await listRuns(5)).toEqual(rows);
    expect(url(0)).toMatch(/\/runs\?limit=5$/);
  });
});

describe("sessions and hosted Checkout", () => {
  it("sends the session cookie with every call, streams included", async () => {
    fetchMock.mockResolvedValueOnce(json([])).mockResolvedValueOnce(sse(createEvents));
    await listRuns();
    await streamRun("trip", () => undefined);
    for (const call of fetchMock.mock.calls) expect(call[1]).toMatchObject({ credentials: "include" });
  });

  it("logs in, reads the session and logs out", async () => {
    const me = { name: "alice", role: "both", auth_enabled: true, require_separate_approver: false };
    fetchMock.mockResolvedValueOnce(json(me)).mockResolvedValueOnce(json(me)).mockResolvedValueOnce(json({}));
    expect(await login("alice", "pw")).toEqual(me);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({ name: "alice", password: "pw" }),
    });
    expect(await getMe()).toEqual(me);
    await logout();
    expect(url(2)).toMatch(/\/auth\/logout$/);
  });

  it("surfaces 401 as ApiError so the console can ask for a login", async () => {
    fetchMock.mockResolvedValueOnce(new Response("not signed in", { status: 401 }));
    const err = await getMe().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
  });

  it("asks the processor about a checkout and cancels it", async () => {
    fetchMock.mockResolvedValueOnce(json(completed)).mockResolvedValueOnce(json(completed));
    await refreshPayment("r 1");
    await cancelPayment("r1");
    expect(url(0)).toMatch(/\/runs\/r%201\/payment\/refresh$/);
    expect(url(1)).toMatch(/\/runs\/r1\/payment\/cancel$/);
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "POST" });
  });
});
