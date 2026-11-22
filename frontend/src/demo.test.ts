import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { demoClient, demoDeadline, parseDemo } from "./demo";
import { QUOTE_HASH, QUOTE_HASHES, RECOMMENDED_FARE_IDS, RUN_ID, createEvents } from "./fixtures/events";
import type { RunEvent } from "./types";

describe("parseDemo", () => {
  it("reads the scenario, hold and pace from the query string", () => {
    expect(parseDemo("?demo=flagged&hold=12&pace=0", true)).toEqual({ scenario: "flagged", hold: 12, paceMs: 0 });
    expect(parseDemo("?demo=clean", true)).toEqual({ scenario: "clean", hold: undefined, paceMs: undefined });
    expect(parseDemo("?demo=denied&hold=-1&pace=x", true)).toEqual({
      scenario: "denied",
      hold: undefined,
      paceMs: undefined,
    });
  });

  it("stays off without a known scenario, or when the demo is disabled", () => {
    expect(parseDemo("", true)).toBeNull();
    expect(parseDemo("?demo=nope", true)).toBeNull();
    expect(parseDemo("?demo=flagged", false)).toBeNull();
  });
});

describe("demoClient", () => {
  const body = (approved: boolean) => ({ approved, approver: "a", quote_hash: QUOTE_HASH });

  it("has no run history", async () => {
    expect(await demoClient({ scenario: "clean" }).listRuns?.()).toEqual([]);
  });

  it("replays the flagged scenario and grows the audit trail like the API", async () => {
    const c = demoClient({ scenario: "flagged" });
    const seen: RunEvent[] = [];
    expect(await c.getAudit(RUN_ID)).toEqual([]);
    const paused = await c.streamRun("trip", (e) => seen.push(e));
    // The recorded stream, with the approval deadline moved to 15 minutes from now.
    expect(seen.map((e) => e.type)).toEqual(createEvents.map((e) => e.type));
    const view = seen[seen.length - 1];
    expect(view.type === "run_view" && view.interrupt?.approve_before).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00\+00:00$/);
    expect(paused.status).toBe("awaiting_approval");
    expect(await c.getAudit(RUN_ID)).toHaveLength(43);
    const done = await c.streamApproval(RUN_ID, body(true), () => {});
    expect(done.status).toBe("completed");
    const audit = await c.getAudit(RUN_ID);
    expect(audit).toHaveLength(50);
    expect(audit.map((e) => e.seq)).toEqual(audit.map((_, i) => i));
    // The decision comes before the seat holds: nothing is held until a human approves.
    const decision = audit.findIndex((e) => e.event === "approval_decision");
    const firstHold = audit.findIndex((e) => e.data["tool"] === "travel_search.hold_seat");
    expect(decision).toBeLessThan(firstHold);
    expect(audit[decision].data["fare_ids"]).toEqual(RECOMMENDED_FARE_IDS);
    expect(await c.verifyAudit()).toEqual({ ok: true, entries: 50, first_bad_seq: null, reason: null });
  });

  it("prices a selection like POST /runs/{run_id}/quote and refuses one outside the offer", async () => {
    const c = demoClient({ scenario: "clean" });
    const ids = ["DR-ZUE-BER-20261201-0802-2nd", RECOMMENDED_FARE_IDS[1]];
    const q = await c.getQuote(RUN_ID, ids);
    expect(q.total_cents).toBe(8400);
    expect(q.quote_hash).toBe(QUOTE_HASHES[ids.join("|")]);
    expect(q.options.map((o) => o.fare_id)).toEqual(ids);
    await expect(c.getQuote(RUN_ID, ["DR-ZUE-BER-20261201-1802-2nd", ids[1]])).rejects.toMatchObject({ status: 422 });
  });

  it("charges the approver's selection, and rejects a hash that does not match it", async () => {
    const c = demoClient({ scenario: "clean" });
    await c.streamRun("trip", () => {});
    const ids = ["DR-ZUE-BER-20261201-0602-2nd", "DR-BER-ZUE-20261202-0902-2nd-saver"];
    const done = await c.streamApproval(
      RUN_ID,
      { approved: true, approver: "a", quote_hash: QUOTE_HASHES[ids.join("|")], fare_ids: ids },
      () => {},
    );
    expect(done.payment?.amount_cents).toBe(8400);
    expect(done.quote?.options.map((o) => o.fare_id)).toEqual(ids);
    await c.streamRun("trip", () => {});
    const tampered = await c.streamApproval(RUN_ID, { approved: true, approver: "a", quote_hash: QUOTE_HASH, fare_ids: ids }, () => {});
    expect(tampered.status).toBe("rejected");
    expect(tampered.payment).toBeNull();
  });

  it("puts the demo deadline 15 minutes ahead, on a whole minute", () => {
    expect(demoDeadline(Date.parse("2026-11-21T16:20:30Z"))).toBe("2026-11-21T16:35:00+00:00");
  });

  it("keeps each clean chain contiguous", async () => {
    const c = demoClient({ scenario: "clean" });
    await c.streamRun("trip", () => {});
    expect((await c.streamApproval(RUN_ID, body(false), () => {})).status).toBe("rejected");
    const audit = await c.getAudit(RUN_ID);
    expect(audit.some((e) => e.event === "injection_flagged")).toBe(false);
    for (let i = 1; i < audit.length; i++) expect(audit[i].prev_hash).toBe(audit[i - 1].hash);
    expect(audit[audit.length - 1]).toMatchObject({ event: "approval_decision", data: { approved: false } });
  });

  it("throws the stream's error like the live client", async () => {
    const c = demoClient({ scenario: "error" });
    const seen: RunEvent[] = [];
    await expect(c.streamRun("trip", (e) => seen.push(e))).rejects.toBeInstanceOf(ApiError);
    expect(seen[seen.length - 1]).toEqual({ type: "error", error_type: "McpTransportError" });
    expect((await c.getAudit(RUN_ID)).length).toBeGreaterThan(0);
  });

  it("freezes a run mid-flight with hold", async () => {
    const c = demoClient({ scenario: "clean", hold: 5 });
    const seen: RunEvent[] = [];
    let settled = false;
    void c.streamRun("trip", (e) => seen.push(e)).finally(() => (settled = true));
    await new Promise((r) => setTimeout(r, 10));
    expect(seen).toHaveLength(5);
    expect(settled).toBe(false);
  });
});
