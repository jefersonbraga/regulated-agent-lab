import { describe, expect, it } from "vitest";
import {
  GRAPH_NODES,
  agentNode,
  eventsFromView,
  graphNodeFor,
  initialGraph,
  liveFlags,
  nodeDisplay,
  reduceAll,
  reduceGraph,
  shortTool,
} from "./runGraph";
import * as fx from "./fixtures/events";

describe("node mapping", () => {
  it("orders the stage like the graph: the human gate before booking", () => {
    expect(GRAPH_NODES).toEqual(["supervisor", "planner", "pricing", "approval", "booking", "payments", "wallet"]);
  });

  it("maps server nodes and agent names onto the six stage nodes", () => {
    expect(graphNodeFor("booking_hold")).toBe("booking");
    expect(graphNodeFor("booking_pay")).toBe("booking");
    expect(graphNodeFor("unknown")).toBeNull();
    // Tool events name the agent, not the node.
    expect(agentNode("booking")).toBe("booking");
    expect(agentNode("human")).toBe("approval");
    expect(agentNode("planner")).toBe("planner");
  });

  it("shortens tool names", () => {
    expect(shortTool("travel_search.search_trains")).toBe("search_trains");
    expect(shortTool("plain")).toBe("plain");
  });
});

describe("reduceGraph", () => {
  it("plays a poisoned run up to the human gate", () => {
    const s = reduceAll(fx.createEvents);
    expect(s.runId).toBe(fx.RUN_ID);
    expect(s.status).toBe("awaiting_approval");
    expect(s.events).toBe(fx.createEvents.length);
    expect(s.nodes.planner.phase).toBe("done");
    expect(nodeDisplay(s.nodes.planner)).toBe("flagged");
    expect(s.nodes.planner.tools).toEqual([
      { tool: "travel_search.search_trains", calls: 2, flagged: 2, errors: 0, denied: 0 },
    ]);
    expect(s.nodes.pricing.tools[0]).toMatchObject({ tool: "travel_search.get_fares", calls: fx.FARE_LOOKUPS });
    // Nothing is held before the human decides.
    expect(s.nodes.booking.phase).toBe("idle");
    expect(s.nodes.booking.tools).toEqual([]);
    expect(s.nodes.approval.phase).toBe("paused");
    expect(s.nodes.approval.decision).toBeNull();
    expect(s.nodes.payments.phase).toBe("idle");
    expect(s.edges["pricing-approval"]).toBe("done");
    expect(s.edges["supervisor-approval"]).toBe("active");
    expect(s.current).toBe("approval");
  });

  it("holds seats and crosses into payments only after the approval", () => {
    const s = reduceAll(fx.approveEvents, reduceAll(fx.createEvents));
    expect(s.status).toBe("completed");
    expect(s.nodes.approval).toMatchObject({ phase: "done", decision: "approved" });
    expect(s.nodes.booking).toMatchObject({ phase: "done", visits: 2 });
    expect(s.nodes.booking.tools[0]).toMatchObject({ tool: "travel_search.hold_seat", calls: 2 });
    expect(s.edges["approval-booking"]).toBe("done");
    expect(s.nodes.payments.phase).toBe("done");
    expect(s.nodes.payments.tools[0]).toMatchObject({ tool: "payments.charge", calls: 1 });
    expect(s.edges["booking-payments"]).toBe("done");
    // Nothing is left moving.
    expect(Object.values(s.edges)).not.toContain("active");
    expect(Object.values(s.nodes).map((n) => n.phase)).not.toContain("running");
  });

  it("records a rejection on the approval node", () => {
    const s = reduceAll(fx.rejectEvents, reduceAll(fx.cleanCreateEvents));
    expect(s.nodes.approval).toMatchObject({ phase: "done", decision: "rejected" });
    expect(s.nodes.booking.phase).toBe("idle");
    expect(s.nodes.payments.phase).toBe("idle");
  });

  it("blocks the agent whose call the policy gate denied", () => {
    const s = reduceAll(fx.deniedCreateEvents);
    expect(s.status).toBe("refused");
    expect(s.nodes.booking.phase).toBe("blocked");
    expect(s.nodes.booking.tools).toContainEqual({ tool: "payments.charge", calls: 0, flagged: 0, errors: 0, denied: 1 });
    // The denied call never reached the payments institution.
    expect(s.nodes.payments.phase).toBe("idle");
    expect(s.lastTool).toBe("payments.charge");
  });

  it("fails whatever was running when the stream reports an error, and names the call in flight", () => {
    const s = reduceAll(fx.errorCreateEvents);
    expect(s.error).toBe("McpTransportError");
    expect(s.nodes.pricing.phase).toBe("failed");
    expect(s.nodes.supervisor.phase).toBe("failed");
    expect(s.failedTool).toBe("travel_search.get_fares");
    expect(s.nodes.pricing.tools).toEqual([
      { tool: "travel_search.get_fares", calls: 1, flagged: 0, errors: 1, denied: 0 },
    ]);
  });

  it("keeps the supervisor routing while steps run, pauses it at the gate and settles it at the end", () => {
    // Mid-run: pricing is working; the supervisor has routed several hops but is not done.
    const mid = reduceAll(fx.cleanCreateEvents.slice(0, 17));
    expect(mid.nodes.pricing.phase).toBe("running");
    expect(mid.nodes.supervisor.phase).toBe("running");
    const paused = reduceAll(fx.cleanCreateEvents);
    expect(paused.nodes.supervisor.phase).toBe("paused");
    const done = reduceAll(fx.cleanApproveEvents, paused);
    expect(done.nodes.supervisor.phase).toBe("done");
    // A refusal stops the booking agent, not the router.
    expect(reduceAll(fx.deniedCreateEvents).nodes.supervisor.phase).toBe("done");
  });

  it("starts over on a new run id, but not on a resumed one", () => {
    const paused = reduceAll(fx.createEvents);
    expect(reduceGraph(paused, { type: "run_started", run_id: fx.RUN_ID }).nodes.planner.visits).toBe(1);
    const fresh = reduceGraph(paused, { type: "run_started", run_id: "other" });
    expect(fresh.runId).toBe("other");
    expect(fresh.nodes.planner.visits).toBe(0);
  });

  it("ignores unknown nodes and agents", () => {
    const s = reduceAll([
      { type: "node_started", node: "mystery" },
      { type: "tool_call", agent: "mystery", tool: "x.y" },
    ]);
    expect(s.nodes).toEqual(initialGraph().nodes);
  });
});

describe("liveFlags", () => {
  it("rebuilds the API's flag strings from the stream so far", () => {
    expect(liveFlags(reduceAll(fx.createEvents.slice(0, 6)))).toEqual(["injection_flagged:travel_search.search_trains"]);
    expect(liveFlags(reduceAll(fx.createEvents))).toEqual(fx.FLAGS);
    expect(liveFlags(reduceAll(fx.deniedCreateEvents))).toEqual(["policy_denied:payments.charge"]);
    expect(liveFlags(reduceAll(fx.cleanCreateEvents))).toEqual([]);
  });
});

describe("eventsFromView", () => {
  it("tells the same story as the stream when only a RunView is available", () => {
    const streamed = reduceAll(fx.createEvents);
    const rebuilt = reduceAll(eventsFromView(fx.awaiting));
    expect(rebuilt.status).toBe("awaiting_approval");
    expect(rebuilt.nodes.approval.phase).toBe("paused");
    for (const id of ["planner", "pricing"] as const) {
      expect(rebuilt.nodes[id].tools).toEqual(streamed.nodes[id].tools);
      expect(nodeDisplay(rebuilt.nodes[id])).toBe(nodeDisplay(streamed.nodes[id]));
    }
  });

  it("replays only the steps after the previous view of the same run", () => {
    const events = eventsFromView(fx.completed, fx.awaiting);
    const tools = events.filter((e) => e.type === "tool_call").map((e) => (e.type === "tool_call" ? e.tool : ""));
    expect(tools).toEqual(["travel_search.hold_seat", "travel_search.hold_seat", "payments.charge"]);
    // The flags were already counted before the pause.
    expect(events.some((e) => e.type === "tool_result" && e.flagged)).toBe(false);
    const s = reduceAll(events, reduceAll(eventsFromView(fx.awaiting)));
    expect(s.status).toBe("completed");
    expect(s.nodes.payments.phase).toBe("done");
    expect(liveFlags(s)).toEqual(fx.FLAGS);
  });
});

describe("hosted Checkout pause", () => {
  it("waits at the payments boundary, then finishes there", () => {
    const paused = reduceAll([
      { type: "run_started", run_id: "r1" },
      { type: "node_started", node: "supervisor" },
      { type: "node_started", node: "booking_pay" },
      { type: "tool_call", agent: "booking", tool: "payments.create_checkout" },
      { type: "tool_result", agent: "booking", tool: "payments.create_checkout", flagged: false },
      { type: "node_finished", node: "booking_pay", interrupted: false },
      { type: "node_started", node: "await_payment" },
      { type: "node_finished", node: "await_payment", interrupted: true },
      { type: "status", status: "awaiting_payment" },
    ]);
    expect(paused.nodes.payments.phase).toBe("paused");
    expect(paused.nodes.payments.tools.map((t) => t.tool)).toEqual(["payments.create_checkout"]);
    expect(paused.nodes.supervisor.phase).toBe("paused");

    const done = reduceAll(
      [
        { type: "node_started", node: "await_payment" },
        { type: "tool_call", agent: "booking", tool: "payments.checkout_status" },
        { type: "tool_result", agent: "booking", tool: "payments.checkout_status", flagged: false },
        { type: "node_finished", node: "await_payment", interrupted: false },
        { type: "status", status: "completed" },
      ],
      paused,
    );
    expect(done.nodes.payments.phase).toBe("done");
    expect(done.status).toBe("completed");
  });
});
