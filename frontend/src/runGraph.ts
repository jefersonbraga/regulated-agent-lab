/**
 * The agent graph as a pure reducer over run events, so the stage can be driven by the live
 * server stream (or by events rebuilt from a RunView when streaming is unavailable).
 */
import type { RunEvent, RunStatus, RunView } from "./types";

/**
 * Stage order follows the graph: planner -> pricing -> approval (human) -> booking -> payments,
 * with the wallet (a third institution) beside payments: booking asks it for a scoped token and
 * payments redeems that token there.
 */
export const GRAPH_NODES = ["supervisor", "planner", "pricing", "approval", "booking", "payments", "wallet"] as const;
export type GraphNodeId = (typeof GRAPH_NODES)[number];

export type NodePhase = "idle" | "running" | "done" | "paused" | "blocked" | "failed";

/** What the stage draws for a node; "flagged" wins over "done" but never hides a block. */
export type NodeDisplay = NodePhase | "flagged";

export interface ToolTally {
  /** Full tool name, e.g. "travel_search.search_trains". */
  tool: string;
  calls: number;
  flagged: number;
  errors: number;
  denied: number;
}

export interface NodeView {
  id: GraphNodeId;
  phase: NodePhase;
  /** A tool result of this node was flagged by the injection guard. */
  flagged: boolean;
  visits: number;
  tools: ToolTally[];
  /** Approval node only: the human decision, once known. */
  decision: "approved" | "rejected" | null;
}

export const EDGES = [
  ["supervisor", "planner"],
  ["supervisor", "pricing"],
  ["supervisor", "approval"],
  ["supervisor", "booking"],
  ["planner", "pricing"],
  ["pricing", "approval"],
  ["approval", "booking"],
  ["booking", "payments"],
  // Wallet model: booking gets a single-use token from the wallet; payments redeems it there.
  ["booking", "wallet"],
  ["wallet", "payments"],
] as const satisfies readonly (readonly [GraphNodeId, GraphNodeId])[];

export type EdgeId = `${GraphNodeId}-${GraphNodeId}`;
export type EdgeState = "idle" | "active" | "done";

export function edgeId(from: GraphNodeId, to: GraphNodeId): EdgeId {
  return `${from}-${to}`;
}

export interface GraphState {
  runId: string | null;
  status: RunStatus | null;
  nodes: Record<GraphNodeId, NodeView>;
  edges: Partial<Record<EdgeId, EdgeState>>;
  /** The step (not the supervisor) that ran last; the stage's focal node. */
  current: GraphNodeId | null;
  /** Last tool called, for the one-line live caption. */
  lastTool: string | null;
  error: string | null;
  /** The policy gate's reason for the last refused call, if any. */
  denyReason: string | null;
  /** The tool call sent and not yet answered. */
  inFlight: { node: GraphNodeId; tool: string } | null;
  /** The tool call in flight when the stream failed, if any. */
  failedTool: string | null;
  /** Number of events applied; 0 means the stage has never seen a run. */
  events: number;
}

function blankNode(id: GraphNodeId): NodeView {
  return { id, phase: "idle", flagged: false, visits: 0, tools: [], decision: null };
}

export function initialGraph(): GraphState {
  const nodes = Object.fromEntries(GRAPH_NODES.map((id) => [id, blankNode(id)])) as Record<GraphNodeId, NodeView>;
  return { runId: null, status: null, nodes, edges: {}, current: null, lastTool: null, error: null, denyReason: null, inFlight: null, failedTool: null, events: 0 };
}

/** Server node name -> stage node. booking_hold and booking_pay are both the booking agent. */
export function graphNodeFor(serverNode: string): GraphNodeId | null {
  switch (serverNode) {
    case "supervisor":
    case "planner":
    case "pricing":
    case "approval":
      return serverNode;
    case "booking_hold":
    case "booking_pay":
      return "booking";
    // Hosted Checkout: the run waits at the payments boundary while the customer pays there.
    case "await_payment":
      return "payments";
    default:
      return null;
  }
}

/** Calls that cross into the payments institution. */
const PAYMENT_TOOLS = new Set(["payments.charge", "payments.create_checkout", "payments.checkout_status"]);

/**
 * Agent name in tool events -> stage node. Agents are supervisor, planner, pricing and booking
 * (both booking_hold and booking_pay run as "booking"); server node names are accepted too.
 */
export function agentNode(agent: string): GraphNodeId | null {
  if (agent === "booking") return "booking";
  if (agent === "human") return "approval";
  return graphNodeFor(agent);
}

/** Where a tool call is drawn: the charge crosses into the payments institution. */
function toolNode(agent: string, tool: string): GraphNodeId | null {
  if (PAYMENT_TOOLS.has(tool)) return "payments";
  if (tool.startsWith("wallet.")) return "wallet";
  return agentNode(agent);
}

/**
 * The edge a payments call travels: a charge after the wallet issued a token goes the token's
 * way (wallet -> payments, the redemption); the hosted Checkout goes straight from booking.
 */
function paymentEdge(s: GraphState, tool: string): EdgeId {
  return tool === "payments.charge" && s.nodes.wallet.phase !== "idle"
    ? edgeId("wallet", "payments")
    : edgeId("booking", "payments");
}

const PIPELINE_PREDECESSOR: Partial<Record<GraphNodeId, GraphNodeId>> = {
  pricing: "planner",
  approval: "pricing",
  booking: "approval",
  payments: "booking",
};

type Draft = GraphState;

function patchNode(s: Draft, id: GraphNodeId, patch: Partial<NodeView>): void {
  s.nodes = { ...s.nodes, [id]: { ...s.nodes[id], ...patch } };
}

function setEdge(s: Draft, id: EdgeId, state: EdgeState): void {
  s.edges = { ...s.edges, [id]: state };
}

function tally(s: Draft, id: GraphNodeId, tool: string, field: keyof Omit<ToolTally, "tool">): void {
  const tools = s.nodes[id].tools.map((t) => ({ ...t }));
  let t = tools.find((x) => x.tool === tool);
  if (!t) {
    t = { tool, calls: 0, flagged: 0, errors: 0, denied: 0 };
    tools.push(t);
  }
  t[field] += 1;
  patchNode(s, id, { tools });
}

/**
 * Settles every running node (and active edge) once the run stops moving. The supervisor only
 * routes, so a refusal elsewhere leaves it done; a failure stops it.
 */
function settle(s: Draft, phase: "done" | "failed" | "blocked"): void {
  for (const id of GRAPH_NODES) {
    const node = s.nodes[id];
    if (id === "supervisor") {
      if (node.phase === "running" || node.phase === "paused") {
        patchNode(s, id, { phase: phase === "blocked" ? "done" : phase });
      }
    } else if (node.phase === "running") patchNode(s, id, { phase });
  }
  for (const [k, v] of Object.entries(s.edges)) if (v === "active") setEdge(s, k as EdgeId, "done");
}

function isTerminal(status: RunStatus): boolean {
  return status !== "running" && status !== "awaiting_approval" && status !== "awaiting_payment";
}

export function reduceGraph(state: GraphState, event: RunEvent): GraphState {
  const s: Draft = { ...state, events: state.events + 1 };
  switch (event.type) {
    case "run_started": {
      if (event.run_id !== state.runId) return { ...initialGraph(), runId: event.run_id, events: s.events };
      s.error = null;
      return s;
    }
    case "node_started": {
      const id = graphNodeFor(event.node);
      if (!id) return s;
      if (id !== "supervisor") {
        // The supervisor routed to this step; it keeps routing until the run pauses or ends.
        for (const [k, v] of Object.entries(s.edges)) {
          if (v === "active" && k.startsWith("supervisor-")) setEdge(s, k as EdgeId, "done");
        }
        setEdge(s, edgeId("supervisor", id), "active");
        const pred = PIPELINE_PREDECESSOR[id];
        if (pred && s.nodes[pred].phase !== "idle") setEdge(s, edgeId(pred, id), "active");
        s.current = id;
      }
      patchNode(s, id, { phase: "running", visits: s.nodes[id].visits + 1 });
      return s;
    }
    case "node_finished": {
      const id = graphNodeFor(event.node);
      if (!id) return s;
      const node = s.nodes[id];
      // One routing hop ended, not the supervisor's job: it stays live until the run settles.
      if (id === "supervisor" && !event.interrupted) return s;
      if (event.interrupted) patchNode(s, id, { phase: "paused" });
      else if (node.phase === "running" || node.phase === "paused") patchNode(s, id, { phase: "done" });
      if (id !== "supervisor") {
        const pred = PIPELINE_PREDECESSOR[id];
        if (pred && s.edges[edgeId(pred, id)] === "active") setEdge(s, edgeId(pred, id), "done");
        if (!event.interrupted && s.edges[edgeId("supervisor", id)] === "active") {
          setEdge(s, edgeId("supervisor", id), "done");
        }
      }
      return s;
    }
    case "tool_call": {
      const id = toolNode(event.agent, event.tool);
      if (!id) return s;
      s.lastTool = event.tool;
      s.inFlight = { node: id, tool: event.tool };
      tally(s, id, event.tool, "calls");
      if (id === "payments") {
        patchNode(s, "payments", { phase: "running", visits: s.nodes.payments.visits + 1 });
        setEdge(s, paymentEdge(s, event.tool), "active");
      }
      if (id === "wallet") {
        patchNode(s, "wallet", { phase: "running", visits: s.nodes.wallet.visits + 1 });
        setEdge(s, edgeId("booking", "wallet"), "active");
      }
      return s;
    }
    case "tool_result": {
      const id = toolNode(event.agent, event.tool);
      if (!id) return s;
      s.inFlight = null;
      if (event.flagged) {
        tally(s, id, event.tool, "flagged");
        patchNode(s, id, { flagged: true });
      }
      if (id === "payments") {
        patchNode(s, "payments", { phase: "done" });
        setEdge(s, paymentEdge(s, event.tool), "done");
      }
      if (id === "wallet") {
        patchNode(s, "wallet", { phase: "done" });
        setEdge(s, edgeId("booking", "wallet"), "done");
      }
      return s;
    }
    case "tool_error": {
      const id = toolNode(event.agent, event.tool);
      if (!id) return s;
      s.inFlight = null;
      tally(s, id, event.tool, "errors");
      if (id === "payments" || id === "wallet") patchNode(s, id, { phase: "failed" });
      return s;
    }
    case "policy_denied": {
      // The call never left the gate: the attempting agent is blocked, not the target.
      const id = agentNode(event.agent);
      if (!id) return s;
      s.lastTool = event.tool;
      tally(s, id, event.tool, "denied");
      s.denyReason = event.reason ?? s.denyReason;
      patchNode(s, id, { phase: "blocked" });
      return s;
    }
    case "status":
    case "run_view": {
      const status = event.status;
      s.status = status;
      if (event.type === "run_view") s.runId = event.run_id;
      const approval = s.nodes.approval;
      if (approval.visits > 0 || approval.phase !== "idle") {
        if (status === "rejected") patchNode(s, "approval", { decision: "rejected", phase: "done" });
        else if (
          (status === "running" || status === "completed" || status === "awaiting_payment") &&
          approval.phase !== "paused"
        ) {
          patchNode(s, "approval", { decision: "approved" });
        }
      }
      if ((status === "awaiting_approval" || status === "awaiting_payment") && s.nodes.supervisor.phase === "running") {
        patchNode(s, "supervisor", { phase: "paused" });
      } else if (status === "running" && s.nodes.supervisor.phase === "paused") {
        patchNode(s, "supervisor", { phase: "running" });
      }
      if (status === "failed") settle(s, "failed");
      else if (status === "refused") settle(s, "blocked");
      else if (isTerminal(status)) settle(s, "done");
      return s;
    }
    case "error": {
      s.error = event.detail ?? event.error_type ?? "stream error";
      // The call in flight when the run broke is the evidence: count it on the step that made it.
      if (s.inFlight) {
        tally(s, s.inFlight.node, s.inFlight.tool, "errors");
        s.failedTool = s.inFlight.tool;
        s.inFlight = null;
      }
      settle(s, "failed");
      return s;
    }
    default:
      return s;
  }
}

export function reduceAll(events: RunEvent[], from: GraphState = initialGraph()): GraphState {
  return events.reduce(reduceGraph, from);
}

/**
 * Run-level flags as the live stream has shown them so far, in the API's shape (one string per
 * occurrence), so the page can warn before the run's final view arrives.
 */
export function liveFlags(state: GraphState): string[] {
  const out: string[] = [];
  for (const id of GRAPH_NODES) {
    for (const t of state.nodes[id].tools) {
      for (let i = 0; i < t.flagged; i++) out.push(`injection_flagged:${t.tool}`);
      for (let i = 0; i < t.denied; i++) out.push(`policy_denied:${t.tool}`);
    }
  }
  return out;
}

export function nodeDisplay(node: NodeView): NodeDisplay {
  if (node.phase === "done" && node.flagged) return "flagged";
  return node.phase;
}

/** "travel_search.search_trains" -> "search_trains". */
export function shortTool(tool: string): string {
  const i = tool.lastIndexOf(".");
  return i < 0 ? tool : tool.slice(i + 1);
}

function isToolStep(step: string): boolean {
  return step.includes(".");
}

/** How many results of each tool the run-level flags mark as injection_flagged. */
function injectionCounts(flags: string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const f of flags) {
    if (!f.startsWith("injection_flagged:")) continue;
    const tool = f.slice("injection_flagged:".length);
    counts.set(tool, (counts.get(tool) ?? 0) + 1);
  }
  return counts;
}

const NODE_AGENT: Record<string, string> = {
  planner: "planner",
  pricing: "pricing",
  booking_hold: "booking",
  booking_pay: "booking",
  await_payment: "booking",
};

/**
 * Rebuilds the event sequence for a RunView from the non-streaming endpoints, so the stage tells
 * the same story without the stream. `previous` is the last view of the same run (an approval
 * resume): only the trajectory steps after it are replayed.
 */
export function eventsFromView(view: RunView, previous: RunView | null = null): RunEvent[] {
  const sameRun = previous?.run_id === view.run_id;
  const steps = view.trajectory.slice(sameRun ? previous.trajectory.length : 0);
  const pending = injectionCounts(view.flags);
  if (sameRun) {
    for (const [tool, n] of injectionCounts(previous.flags)) pending.set(tool, (pending.get(tool) ?? 0) - n);
  }
  const out: RunEvent[] = [{ type: "run_started", run_id: view.run_id }];
  let open: string | null = null;
  const close = () => {
    if (open) out.push({ type: "node_finished", node: open, interrupted: false });
    open = null;
  };
  for (const step of steps) {
    if (isToolStep(step)) {
      const agent = (open && NODE_AGENT[open]) ?? "booking";
      out.push({ type: "tool_call", agent, tool: step });
      const left = pending.get(step) ?? 0;
      if (left > 0) pending.set(step, left - 1);
      out.push({ type: "tool_result", agent, tool: step, flagged: left > 0 });
      continue;
    }
    close();
    out.push({ type: "node_started", node: step });
    open = step;
  }
  close();
  if (view.status === "awaiting_approval") {
    out.push({ type: "node_started", node: "approval" });
    out.push({ type: "node_finished", node: "approval", interrupted: true });
  }
  if (view.status === "awaiting_payment") {
    out.push({ type: "node_started", node: "await_payment" });
    out.push({ type: "node_finished", node: "await_payment", interrupted: true });
  }
  out.push({ type: "status", status: view.status });
  out.push({ type: "run_view", ...view });
  return out;
}
