import { useEffect, useState, type CSSProperties, type ComponentType, type SVGProps } from "react";
import { X } from "lucide-react";
import { ArrowDownLeft, ArrowUpRight, Ban, CreditCard, Landmark, ShieldAlert, Sparkles, UserCheck, Circle, TriangleAlert, Wallet } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { Icon } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Table, TableBody, TableCell, TableHeader, TableHeaderCell, TableRow } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import type { AuditEntry, VerifyResult } from "../types";
import type { GraphNodeId } from "../runGraph";
import { shortHash } from "../format";
import { NODE_SPECS } from "./AgentStage";
import { HashChip } from "./HashChip";

type Glyph = ComponentType<SVGProps<SVGSVGElement>>;

export function isFlagged(entry: AuditEntry): boolean {
  return entry.event === "injection_flagged";
}

/** A call the policy gate refused, or a step that failed. */
export function isDenied(entry: AuditEntry): boolean {
  return entry.event === "policy_denied" || entry.event === "node_error";
}

/** The human approval decision: the HITL evidence the whole demo is about. */
export function isApproval(entry: AuditEntry): boolean {
  return entry.event === "approval_decision";
}

function toolOf(entry: AuditEntry): string | null {
  const tool = entry.data["tool"];
  return typeof tool === "string" ? tool : null;
}

export function isPayment(entry: AuditEntry): boolean {
  return toolOf(entry) === "payments.charge";
}

/** What the card processor reported after the charge (a webhook event or reconciliation). */
export function isProcessorEvent(entry: AuditEntry): boolean {
  return entry.actor === "processor";
}

/**
 * The wallet institution's facts (token issued or redeemed, a fallback to the hosted page, a card
 * saved or removed, a limit changed) and the booking agent's calls to it.
 */
export function isWalletEvent(entry: AuditEntry): boolean {
  return entry.actor === "wallet" || (toolOf(entry)?.startsWith("wallet.") ?? false);
}

/** Processor events that need attention: money went back, or is contested. */
const PROCESSOR_ALERTS = new Set(["payment_refunded", "dispute_opened", "payment_failed", "livemode_event_rejected"]);

/**
 * The audit entries behind one stage node. Actors are supervisor, planner, pricing, booking and
 * human; the payments node is the booking agent's payments.charge rows.
 */
export function entriesForNode(entries: AuditEntry[], node: GraphNodeId | null): AuditEntry[] {
  if (!node) return entries;
  if (node === "payments") return entries.filter((e) => isPayment(e) || isProcessorEvent(e));
  if (node === "wallet") return entries.filter(isWalletEvent);
  if (node === "approval") return entries.filter((e) => e.actor === "human");
  if (node === "booking") return entries.filter((e) => e.actor === "booking" && !isPayment(e) && !isWalletEvent(e));
  return entries.filter((e) => e.actor === node);
}

export function blockId(seq: number): string {
  return `audit-block-${seq}`;
}

/**
 * Scrolls to an audit block, focuses it and opens its detail (unless `open` is false); false if
 * it is not rendered.
 */
export function focusAuditBlock(seq: number, open = true): boolean {
  const el = document.getElementById(blockId(seq));
  if (!el) return false;
  el.scrollIntoView?.({ behavior: "smooth", block: "center", inline: "center" });
  el.focus({ preventScroll: true });
  if (open && el.getAttribute("aria-pressed") !== "true") el.click();
  return true;
}

function eventGlyph(e: AuditEntry): Glyph {
  if (isFlagged(e)) return ShieldAlert;
  if (e.event === "policy_denied") return Ban;
  if (e.event === "node_error") return TriangleAlert;
  if (isApproval(e)) return UserCheck;
  if (isPayment(e)) return CreditCard;
  if (isProcessorEvent(e)) return Landmark;
  if (isWalletEvent(e)) return Wallet;
  if (e.event === "tool_call") return ArrowUpRight;
  if (e.event === "tool_result") return ArrowDownLeft;
  if (e.event === "intent") return Sparkles;
  return Circle;
}

/** Block and link widths in px (index.css: .chain-block, .chain-link min width). */
const BLOCK_PX = 38;
const LINK_PX = 9;

/**
 * Columns for `n` blocks in a row of `width` px, balanced so the last row is never a widow:
 * 26 blocks that fit 20 a row become 13 + 13, not 20 + 6.
 */
export function chainColumns(n: number, width: number | null): number {
  if (n <= 0) return 1;
  const fit = width === null ? 20 : Math.max(1, Math.floor((width - BLOCK_PX) / (BLOCK_PX + LINK_PX)) + 1);
  const rows = Math.ceil(n / fit);
  return Math.ceil(n / rows);
}

/** Grid columns: a bare first block, then link+block cells that stretch only when rows wrap. */
function chainGrid(cols: number, wraps: boolean): CSSProperties {
  const rest = cols > 1 ? ` repeat(${cols - 1}, minmax(${BLOCK_PX + LINK_PX}px, ${wraps ? "1fr" : `${BLOCK_PX + LINK_PX}px`}))` : "";
  return { gridTemplateColumns: `${BLOCK_PX}px${rest}` };
}

/** Content width of an element, tracked; null until measured (or without ResizeObserver). */
function useWidth(el: HTMLElement | null): number | null {
  const [width, setWidth] = useState<number | null>(null);
  useEffect(() => {
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return width;
}

/** The block's state in words, for screen readers and the tooltip (not colour alone). */
export function toneWords(e: AuditEntry): string {
  switch (tone(e)) {
    case "flagged":
      return "flagged by injection guard";
    case "denied":
      return e.event === "node_error" ? "step failed" : "denied by policy";
    case "approved":
      return "human approval";
    case "rejected":
      return "human rejection";
    case "payment":
      return "payment";
    case "processor":
      return `processor: ${e.event.replace(/_/g, " ")}`;
    case "processor-alert":
      return `processor: ${e.event.replace(/_/g, " ")}`;
    case "wallet":
      return `wallet: ${e.event.replace(/_/g, " ")}`;
    default:
      return "";
  }
}

function tone(e: AuditEntry): string | undefined {
  if (isFlagged(e)) return "flagged";
  if (isDenied(e)) return "denied";
  if (isApproval(e)) return e.data["approved"] === true ? "approved" : "rejected";
  if (isPayment(e)) return "payment";
  if (isProcessorEvent(e)) return PROCESSOR_ALERTS.has(e.event) ? "processor-alert" : "processor";
  if (e.actor === "wallet") return "wallet";
  return undefined;
}

/** Same zone as every other stamp on the page (approval, deadline): UTC. */
function clock(ms: number): string {
  return `${new Date(ms).toISOString().slice(11, 16)} UTC`;
}

export function VerifyBadge({ verify, verifiedAt }: { verify: VerifyResult | null; verifiedAt?: number | null }) {
  if (!verify) return <Badge data-testid="verify-badge" variant="neutral" label="not verified" />;
  const at = verifiedAt ? ` · ${clock(verifiedAt)}` : "";
  if (verify.ok) {
    return <Badge data-testid="verify-badge" variant="success" label={`Whole chain verified · ${verify.entries} entries${at}`} />;
  }
  return (
    <Badge
      data-testid="verify-badge"
      variant="error"
      label={`Chain broken at seq ${verify.first_bad_seq ?? "?"}${verify.reason ? `: ${verify.reason}` : ""}${at}`}
    />
  );
}

/** The badge needs something to vouch for; a broken chain always shows. */
export function showVerifyBadge(entries: AuditEntry[], verify: VerifyResult | null): boolean {
  if (entries.length > 0) return true;
  return verify !== null && (!verify.ok || verify.entries > 0);
}

function Block({
  e,
  index,
  rowStart,
  broken,
  after,
  selected,
  onSelect,
}: {
  e: AuditEntry;
  index: number;
  rowStart: boolean;
  broken: boolean;
  after: boolean;
  selected: boolean;
  onSelect: (seq: number) => void;
}) {
  const tool = toolOf(e);
  const state = [toneWords(e), broken ? "chain broken here" : after ? "after the break" : ""].filter(Boolean).join(", ");
  const tip = (
    <span className="block-tip">
      <span>
        #{e.seq} · {e.actor} · {e.event}
        {state ? ` · ${state}` : ""}
      </span>
      {tool && <span className="mono">{tool}</span>}
      <span className="mono">
        {shortHash(e.prev_hash)} → {shortHash(e.hash)}
      </span>
    </span>
  );
  return (
    <li className="chain-item" style={{ ["--i" as string]: index }}>
      {!rowStart && <span className="chain-link" aria-hidden="true" data-broken={broken ? "true" : undefined} />}
      <Tooltip content={tip}>
        <button
          type="button"
          id={blockId(e.seq)}
          className="chain-block"
          data-actor={e.actor}
          data-tone={tone(e)}
          data-event={e.event}
          data-broken={broken ? "true" : undefined}
          data-after-break={after ? "true" : undefined}
          aria-pressed={selected}
          onClick={() => onSelect(e.seq)}
          aria-label={`seq ${e.seq}, ${e.actor}, ${e.event}${tool ? `, ${tool}` : ""}${state ? `, ${state}` : ""}`}
        >
          <span className="chain-seq">{e.seq}</span>
          <span className="chain-glyph" aria-hidden="true">
            <Icon icon={eventGlyph(e)} size="xsm" color="inherit" />
          </span>
          <span className="chain-actor" aria-hidden="true" />
        </button>
      </Tooltip>
    </li>
  );
}

const LEGEND: { icon: Glyph; text: string }[] = [
  { icon: ArrowUpRight, text: "tool call" },
  { icon: ArrowDownLeft, text: "tool result" },
  { icon: ShieldAlert, text: "flagged" },
  { icon: Ban, text: "denied" },
  { icon: UserCheck, text: "approval" },
  { icon: CreditCard, text: "payment" },
  { icon: Landmark, text: "processor event" },
  { icon: Wallet, text: "wallet" },
];

const LEGEND_ACTORS = ["supervisor", "planner", "pricing", "booking", "human", "wallet", "processor"] as const;

/** What the glyphs and the actor dots on a block mean. */
function ChainLegend() {
  return (
    <ul className="chain-legend" aria-label="Legend" data-testid="chain-legend">
      {LEGEND.map((l) => (
        <li key={l.text}>
          <Icon icon={l.icon} size="xsm" color="secondary" />
          <Text type="supporting" color="secondary">
            {l.text}
          </Text>
        </li>
      ))}
      <li className="chain-legend-gap" aria-hidden="true" />
      {LEGEND_ACTORS.map((a) => (
        <li key={a} data-actor={a}>
          <span className="chain-actor" aria-hidden="true" />
          <Text type="supporting" color="secondary">
            {a}
          </Text>
        </li>
      ))}
    </ul>
  );
}

function value(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** One block opened: who did what, and the hash link to the block before it. */
function BlockDetail({ e, onClose }: { e: AuditEntry; onClose: () => void }) {
  const fields = Object.entries(e.data).filter(([k]) => k !== "tool");
  const tool = toolOf(e);
  return (
    <div className="chain-detail" data-testid="block-detail" data-tone={tone(e)} role="group" aria-label={`Block ${e.seq}`}>
      <div className="chain-detail-head">
        <Text weight="semibold">
          #{e.seq} · {e.actor} · {e.event}
        </Text>
        {tool && (
          <Text type="code" size="sm">
            {tool}
          </Text>
        )}
        <span className="chain-detail-close">
          <IconButton label="Close block detail" size="sm" variant="ghost" icon={<Icon icon={X} size="sm" color="inherit" />} onClick={onClose} />
        </span>
      </div>
      <dl className="chain-detail-facts">
        <div>
          <dt>prev hash</dt>
          <dd>
            <HashChip hash={e.prev_hash} label={`previous hash of seq ${e.seq}`} />
          </dd>
        </div>
        <div>
          <dt>hash</dt>
          <dd>
            <HashChip hash={e.hash} label={`hash of seq ${e.seq}`} />
          </dd>
        </div>
        {fields.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd className="mono">{value(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function DetailTable({ entries }: { entries: AuditEntry[] }) {
  return (
    <div className="chain-table">
      <Table density="compact" dividers="rows" verticalAlign="middle">
        <TableHeader>
          <TableRow isHeaderRow>
            <TableHeaderCell scope="col">seq</TableHeaderCell>
            <TableHeaderCell scope="col">actor</TableHeaderCell>
            <TableHeaderCell scope="col">event</TableHeaderCell>
            <TableHeaderCell scope="col">tool</TableHeaderCell>
            <TableHeaderCell scope="col">hash</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map((e) => (
            <TableRow key={e.seq} data-event={e.event} data-tone={tone(e)} className="chain-row">
              <TableCell>
                <Text hasTabularNumbers>{e.seq}</Text>
              </TableCell>
              <TableCell>
                <span className="actor-cell" data-actor={e.actor}>
                  <span className="chain-actor" aria-hidden="true" />
                  {e.actor}
                </span>
              </TableCell>
              <TableCell>{e.event}</TableCell>
              <TableCell>
                <Text type="code" size="sm">
                  {toolOf(e) ?? ""}
                </Text>
              </TableCell>
              <TableCell>
                <HashChip hash={e.hash} label={`hash of seq ${e.seq}`} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export interface EvidenceChainProps {
  /** Entries of the current run (GET /audit?run_id=…). */
  entries: AuditEntry[];
  /** GET /audit/verify, which checks the whole chain (all runs). */
  verify: VerifyResult | null;
  verifiedAt?: number | null;
  /** Stage node whose entries are shown; null for all. */
  filter: GraphNodeId | null;
  onClearFilter: () => void;
  onVerify: () => Promise<void> | void;
  /** A run is in progress: its entries load when it pauses or ends. */
  recording?: boolean;
}

/** The run's audit trail as linked blocks: the evidence end of the story. */
export function EvidenceChain({
  entries,
  verify,
  verifiedAt,
  filter,
  onClearFilter,
  onVerify,
  recording = false,
}: EvidenceChainProps) {
  const [table, setTable] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const shown = entriesForNode(entries, filter);
  const opened = open === null ? undefined : shown.find((e) => e.seq === open);
  const badSeq = verify && !verify.ok ? verify.first_bad_seq : null;
  const [chainEl, setChainEl] = useState<HTMLOListElement | null>(null);
  const width = useWidth(chainEl);
  const cols = chainColumns(shown.length, width);
  const wraps = shown.length > cols;
  return (
    <section className="evidence" aria-label="Audit chain" data-broken={badSeq !== null ? "true" : undefined}>
      <div className="evidence-head">
        <div className="evidence-title">
          <Heading level={2}>Audit chain</Heading>
          <Text type="supporting" color="secondary" data-testid="audit-scope">
            {filter ? `${shown.length} of ${entries.length}` : `${entries.length}`} entries
          </Text>
          {recording && <Badge variant="info" label="recording…" data-testid="audit-recording" />}
          {filter && (
            <Button
              label={`${NODE_SPECS[filter].label} ×`}
              size="sm"
              variant="secondary"
              aria-label={`Clear filter ${NODE_SPECS[filter].label}`}
              onClick={onClearFilter}
            />
          )}
        </div>
        <div className="evidence-verify">
          {showVerifyBadge(entries, verify) && <VerifyBadge verify={verify} verifiedAt={verifiedAt} />}
          <Button
            label="Verify chain"
            size="sm"
            variant="secondary"
            isDisabled={entries.length === 0 || recording}
            tooltip={entries.length === 0 || recording ? "Nothing to verify yet" : undefined}
            onClick={() => void onVerify()}
          />
        </div>
        <span className="evidence-table-toggle">
          <Button
            label={table ? "Hide table" : "Table"}
            size="sm"
            variant="ghost"
            aria-expanded={table}
            onClick={() => setTable((t) => !t)}
          />
        </span>
      </div>
      {shown.length === 0 ? (
        <Text type="supporting" color="secondary" as="p" className="evidence-empty" data-testid="audit-empty">
          {entries.length > 0
            ? "No entries for this node."
            : recording
              ? "Recording… the chain loads when the run pauses for approval or ends."
              : "Blocks appear here once a run starts."}
        </Text>
      ) : (
        <>
          <Text type="supporting" color="secondary" as="p" className="chain-caption">
            Each block stores the previous block's hash, so editing any entry breaks every link after it. Select a
            block for its detail.
          </Text>
          <ChainLegend />
          {badSeq !== null && (
            <Text as="p" className="chain-break-note" data-testid="chain-break-note">
              Block #{badSeq} no longer matches its stored hash: it, or the entry before it, was edited after it was
              written. Nothing from #{badSeq} on can be trusted until the log is restored.
            </Text>
          )}
          <ol className="chain" aria-label="Audit blocks" ref={setChainEl} style={chainGrid(cols, wraps)}>
            {shown.map((e, i) => (
              <Block
                key={e.seq}
                e={e}
                index={i}
                rowStart={i % cols === 0}
                broken={badSeq !== null && e.seq === badSeq}
                after={badSeq !== null && e.seq > badSeq}
                selected={open === e.seq}
                onSelect={(seq) => setOpen((cur) => (cur === seq ? null : seq))}
              />
            ))}
          </ol>
        </>
      )}
      {opened && <BlockDetail e={opened} onClose={() => setOpen(null)} />}
      {table && shown.length > 0 && <DetailTable entries={shown} />}
    </section>
  );
}
