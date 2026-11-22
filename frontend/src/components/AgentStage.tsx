import type { CSSProperties, ComponentType, SVGProps } from "react";
import { Ban, Calculator, Check, Landmark, Route, ShieldAlert, Ticket, UserCheck, Wallet, Workflow, X } from "lucide-react";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import {
  EDGES,
  edgeId,
  nodeDisplay,
  shortTool,
  type GraphNodeId,
  type GraphState,
  type NodeDisplay,
  type NodeView,
} from "../runGraph";
import { StatusMark, type MarkTone } from "./StatusMark";

type Glyph = ComponentType<SVGProps<SVGSVGElement>>;

interface NodeSpec {
  label: string;
  role: string;
  icon: Glyph;
  /** Centre on the stage, in % of its width / height. */
  x: number;
  y: number;
}

/**
 * Spatial story, left to right, in graph order: the supervisor routes from above, the work flows
 * along the track to the human gate, booking holds the chosen seats only after it, and only then
 * crosses the dashed trust boundary into payments.
 */
export const NODE_SPECS: Record<GraphNodeId, NodeSpec> = {
  supervisor: { label: "Supervisor", role: "routes every step", icon: Workflow, x: 38.5, y: 19 },
  planner: { label: "Planner", role: "agent", icon: Route, x: 10, y: 62 },
  pricing: { label: "Pricing", role: "agent", icon: Calculator, x: 29, y: 62 },
  approval: { label: "Approval", role: "human", icon: UserCheck, x: 48, y: 62 },
  booking: { label: "Booking", role: "agent", icon: Ticket, x: 67, y: 62 },
  payments: { label: "Payments", role: "institution", icon: Landmark, x: 89.5, y: 62 },
  // A third institution in its own boundary, above payments: booking -> wallet -> payments.
  wallet: { label: "Wallet", role: "institution", icon: Wallet, x: 89.5, y: 14 },
};

/** Nodes and edges drawn without the wallet (simulated provider, or Checkout only). */
const WALLET_PARTS = new Set<string>(["wallet"]);

const STATE_LABEL: Record<NodeDisplay, string> = {
  idle: "idle",
  running: "running",
  done: "done",
  paused: "waiting for you",
  blocked: "blocked by policy",
  failed: "failed",
  flagged: "flagged",
};

const STATE_TONE: Partial<Record<NodeDisplay, MarkTone>> = {
  running: "accent",
  done: "success",
  paused: "accent",
  blocked: "refused",
  failed: "error",
  flagged: "warning",
};

/** The shaped badge on a medallion, so state never rests on colour alone. */
const STATE_BADGE: Partial<Record<NodeDisplay, { icon: Glyph; tone: string }>> = {
  done: { icon: Check, tone: "success" },
  flagged: { icon: ShieldAlert, tone: "warning" },
  blocked: { icon: Ban, tone: "error" },
  failed: { icon: X, tone: "error" },
};

function badgeFor(node: NodeView, display: NodeDisplay) {
  if (node.id === "approval" && node.decision === "rejected") return { icon: X, tone: "warning" };
  return STATE_BADGE[display];
}

/** Short forms for narrow stages, where the long words would wrap under a medallion. */
const SHORT_LABEL: Partial<Record<NodeDisplay, string>> = {
  paused: "waiting",
  blocked: "blocked",
};

/** The supervisor only routes: it is "routing" while the run moves, never done mid-run. */
const SUPERVISOR_LABEL: Partial<Record<NodeDisplay, string>> = {
  running: "routing",
  paused: "paused",
  failed: "stopped",
};

export function stateLabel(node: NodeView): string {
  if (node.id === "approval" && node.decision) return node.decision;
  const display = nodeDisplay(node);
  if (node.id === "supervisor") return SUPERVISOR_LABEL[display] ?? STATE_LABEL[display];
  return STATE_LABEL[display];
}

function shortLabel(node: NodeView): string {
  if (node.id === "supervisor" || (node.id === "approval" && node.decision)) return stateLabel(node);
  return SHORT_LABEL[nodeDisplay(node)] ?? stateLabel(node);
}

/** Edge geometry in the stage's 0–100 coordinate space. */
function edgePath(from: GraphNodeId, to: GraphNodeId): string {
  const a = NODE_SPECS[from];
  const b = NODE_SPECS[to];
  if (from === "supervisor") {
    const midY = (a.y + b.y) / 2;
    return `M ${a.x} ${a.y} C ${a.x} ${midY}, ${b.x} ${midY}, ${b.x} ${b.y}`;
  }
  return `M ${a.x} ${a.y} L ${b.x} ${b.y}`;
}

function ToolChips({ node }: { node: NodeView }) {
  if (node.tools.length === 0) return null;
  return (
    <ul className="stage-chips" aria-label={`${NODE_SPECS[node.id].label} tool calls`}>
      {node.tools.map((t) => (
        <li
          key={t.tool}
          className="stage-chip"
          title={t.tool}
          data-flagged={t.flagged > 0 ? "true" : undefined}
          data-denied={t.denied > 0 || t.errors > 0 ? "true" : undefined}
        >
          <span className="stage-chip-text">
            {shortTool(t.tool)}
            {t.calls > 1 ? ` ×${t.calls}` : ""}
            {t.flagged > 0 ? (t.flagged === t.calls ? " flagged" : ` · ${t.flagged} flagged`) : ""}
            {t.denied > 0 ? " denied" : ""}
            {t.errors > 0 ? " · failed" : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

function StageNode({
  node,
  selected,
  onSelect,
}: {
  node: NodeView;
  selected: boolean;
  onSelect: (id: GraphNodeId) => void;
}) {
  const spec = NODE_SPECS[node.id];
  const display = nodeDisplay(node);
  // The supervisor's pause is the approval node's moment: no second accent on the stage.
  const tone = node.id === "supervisor" && display === "paused" ? undefined : STATE_TONE[display];
  const badge = badgeFor(node, display);
  const calls = node.tools.reduce((n, t) => n + t.calls + t.denied, 0);
  const style = { "--x": `${spec.x}%`, "--y": `${spec.y}%` } as CSSProperties;
  return (
    <div className="stage-slot" style={style} data-node={node.id}>
      <button
        type="button"
        className="stage-node"
        data-node={node.id}
        data-state={display}
        data-decision={node.decision ?? undefined}
        aria-pressed={selected}
        aria-label={`${spec.label}: ${stateLabel(node)}`}
        onClick={() => onSelect(node.id)}
      >
        <span className="stage-node-icon" aria-hidden="true">
          <Icon icon={spec.icon} size="sm" color="inherit" />
          {badge && (
            <span className="stage-node-check" data-tone={badge.tone}>
              <Icon icon={badge.icon} size="xsm" color="inherit" />
            </span>
          )}
          {calls > 0 && <span className="stage-node-count">{calls}</span>}
        </span>
        <span className="stage-node-text">
          <Text weight="semibold" size="sm">
            {spec.label}
          </Text>
          <span className="stage-node-state">
            {tone && (
              <StatusMark tone={tone} label={stateLabel(node)} pulse={display === "running" || display === "paused"} />
            )}
            <Text type="supporting" color="secondary" className="stage-node-label">
              <span className="label-long">{stateLabel(node)}</span>
              <span className="label-short" aria-hidden="true">
                {shortLabel(node)}
              </span>
            </Text>
          </span>
        </span>
      </button>
      <ToolChips node={node} />
    </div>
  );
}

export interface AgentStageProps {
  graph: GraphState;
  selected: GraphNodeId | null;
  onSelect: (id: GraphNodeId | null) => void;
  /** Who processes payments, shown inside the trust boundary, e.g. "Stripe · test mode". */
  processor?: string | null;
  /** Who moves the money: "Customer pays on Stripe", "Agent pays with a wallet token", … */
  paymentStep?: string | null;
  /** Draw the wallet institution (the wallet payment model is allowed). */
  showWallet?: boolean;
  /** Under the wallet's name, e.g. "Visa ····4242 · limit CHF 200" or "no card saved". */
  walletNote?: string | null;
  /** The payment model the run used, once known: the stage marks its path. */
  paymentModel?: string | null;
}

/**
 * Narrow screens hide the per-node tool chips; the ones that carry a warning (flagged output,
 * a refused call) are listed under the stage instead, so the reason a run stopped stays visible.
 */
export function StageNotes({ graph }: { graph: GraphState }) {
  const notes = (Object.keys(NODE_SPECS) as GraphNodeId[]).flatMap((id) =>
    graph.nodes[id].tools
      .filter((t) => t.flagged > 0 || t.denied > 0 || t.errors > 0)
      .map((t) => ({
        key: `${id}-${t.tool}`,
        tone: t.denied > 0 || t.errors > 0 ? "denied" : "flagged",
        text: `${NODE_SPECS[id].label} · ${shortTool(t.tool)} · ${
          t.denied > 0
            ? `${t.denied} denied by policy`
            : t.errors > 0
              ? "failed"
              : `${t.flagged} result${t.flagged === 1 ? "" : "s"} flagged`
        }`,
      })),
  );
  if (notes.length === 0) return null;
  return (
    <ul className="stage-notes" aria-label="Notable tool calls">
      {notes.map((n) => (
        <li key={n.key} className="stage-chip" data-flagged={n.tone === "flagged" ? "true" : undefined} data-denied={n.tone === "denied" ? "true" : undefined}>
          <span className="stage-chip-text">{n.text}</span>
        </li>
      ))}
    </ul>
  );
}

/** The run as a live graph: the page's focal point. */
export function AgentStage({
  graph,
  selected,
  onSelect,
  processor,
  paymentStep,
  showWallet = false,
  walletNote,
  paymentModel,
}: AgentStageProps) {
  const toggle = (id: GraphNodeId) => onSelect(selected === id ? null : id);
  // The wallet is drawn when it is allowed, or when this run used it (a reopened run).
  const wallet = showWallet || graph.nodes.wallet.phase !== "idle";
  const ids = (Object.keys(NODE_SPECS) as GraphNodeId[]).filter((id) => wallet || !WALLET_PARTS.has(id));
  const edges = EDGES.filter(([from, to]) => wallet || (!WALLET_PARTS.has(from) && !WALLET_PARTS.has(to)));
  return (
    <div
      className="stage"
      role="group"
      aria-label="Agent graph"
      data-testid="agent-stage"
      data-status={graph.status ?? "idle"}
      data-empty={graph.events === 0 ? "true" : undefined}
      data-wallet={wallet ? "true" : undefined}
      data-model={paymentModel ?? undefined}
    >
      {wallet && (
        <div className="stage-boundary" data-boundary="wallet" aria-hidden="true" data-testid="stage-wallet-boundary">
          <Text type="supporting" color="secondary" weight="medium">
            Wallet
          </Text>
          <Text type="supporting" color="secondary" className="stage-boundary-note">
            separate institution
          </Text>
          {walletNote && (
            <Text type="supporting" color="secondary" className="stage-boundary-note" data-testid="stage-wallet-note">
              {walletNote}
            </Text>
          )}
        </div>
      )}
      <div className="stage-boundary" data-boundary="payments" aria-hidden="true">
        <Text type="supporting" color="secondary" weight="medium">
          Payments institution
        </Text>
        <Text type="supporting" color="secondary" className="stage-boundary-note">
          separate trust boundary
        </Text>
        <Text type="supporting" color="secondary" className="stage-boundary-note">
          after approval only
        </Text>
        {processor && (
          <Text type="supporting" weight="semibold" className="stage-boundary-note" data-testid="stage-processor">
            {processor}
          </Text>
        )}
        {paymentStep && (
          <Text type="supporting" color="secondary" className="stage-boundary-note" data-testid="stage-payment-step">
            {paymentStep}
          </Text>
        )}
      </div>
      <svg className="stage-edges" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {edges.map(([from, to]) => {
          const id = edgeId(from, to);
          return (
            <path
              key={id}
              d={edgePath(from, to)}
              className="stage-edge"
              data-edge={id}
              data-kind={from === "supervisor" ? "route" : "flow"}
              data-state={graph.edges[id] ?? "idle"}
              vectorEffect="non-scaling-stroke"
            />
          );
        })}
      </svg>
      {ids.map((id) => (
        <StageNode key={id} node={graph.nodes[id]} selected={selected === id} onSelect={toggle} />
      ))}
    </div>
  );
}
