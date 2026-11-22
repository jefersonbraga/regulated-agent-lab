import { useEffect, useId, useRef, useState, type Ref } from "react";
import { ArrowDownRight, Lock, RotateCcw } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Heading } from "@astryxdesign/core/Heading";
import { Icon } from "@astryxdesign/core/Icon";
import { InfoTip } from "@astryxdesign/core/InfoTip";
import { RadioList, RadioListItem } from "@astryxdesign/core/RadioList";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";
import type { AuditEntry, LegOffer, Offer, Option, PaymentModel, PaymentOptions, PaymentsFlow, Quote } from "../types";
import {
  durationMinutes,
  fareClassLabel,
  formatDuration,
  formatApproveBy,
  formatCents,
  formatCentsShort,
  formatClock,
  formatCountdown,
  formatDay,
  formatTime,
  parseInstant,
  stationCodes,
  stationName,
  windowLine,
} from "../format";
import { hasInjectionFlag } from "../flags";
import { FlagsBanner } from "./FlagsBanner";
import { cardLabel, chargeNotice, PROCESSOR, walletUnavailableLine } from "../processor";
import { HashChip } from "./HashChip";

/** The deadline chip turns to a warning when the offer expires within this window. */
export const EXPIRY_WARNING_MS = 5 * 60_000;

export interface ApprovalCardProps {
  /** The recommended quote from the interrupt (cheapest per leg). */
  quote: Quote;
  /**
   * The priced alternatives per leg. With it the approver picks one train per leg; without it
   * (an older payload) the card shows the recommended quote only.
   */
  offer?: Offer | null;
  /**
   * POST /runs/{run_id}/quote: the server's quote (total and hash) for a selection. Approve stays
   * locked until the server has answered for the current selection.
   */
  onQuote?: (fareIds: string[]) => Promise<Quote>;
  /** Backend payments mode, so the card can say what Approve will do. */
  payments?: "simulated" | "stripe_test";
  /** Which payment models the backend allows (PAYMENTS_FLOW). */
  paymentsFlow?: PaymentsFlow;
  /**
   * The run's payment options (RunView.payment_options): with both models allowed, the card
   * offers the choice between the processor's page and the agent paying from the wallet.
   */
  paymentOptions?: PaymentOptions | null;
  /** The signed-in user (or "local-dev"): the wallet reads "my wallet" when it is theirs. */
  viewer?: string | null;
  /**
   * Offer expiry from the interrupt (ISO datetime). The server rejects an approval at or after
   * it; null means the offer has no readable expiry. Undefined: not shown.
   */
  approveBefore?: string | null;
  /** Run-level flags; when present the card warns and asks for an explicit review first. */
  flags?: string[];
  /** The run's injection_flagged audit entries: what the reviewer checks. */
  flaggedEntries?: AuditEntry[];
  /** Jumps to the flagged blocks in the audit chain. */
  onShowFlagged?: () => void;
  /** The request the agents answered, so the approver sees what was asked. */
  request?: string | null;
  /** Sends the request again for a fresh quote (offered once the approval window has passed). */
  onRequestNew?: () => void;
  /**
   * Called once per decision with the hash of the quote on screen, its fare ids (one per leg) and,
   * when the run offers payment options, the chosen payment model; buttons stay disabled until
   * the promise settles.
   */
  onDecision: (
    approved: boolean,
    quoteHash: string,
    fareIds: string[],
    paymentModel?: PaymentModel,
  ) => Promise<void> | void;
  disabled?: boolean;
  /** The card's heading (focusable with tabIndex -1), so the page can move focus to the decision. */
  headingRef?: Ref<HTMLHeadingElement>;
  cardRef?: Ref<HTMLDivElement>;
}

/**
 * Why the wallet cannot pay `totalCents` (null: it can). The limit is checked against the total
 * on screen, so picking a dearer train can move the wallet out of reach.
 */
export function walletReason(options: PaymentOptions, totalCents: number | null | undefined): string | null {
  if (!options.card) return "no_saved_card";
  const other = options.wallet_unavailable;
  if (other && other !== "over_mandate") return other;
  if (totalCents != null && options.mandate_cents != null) {
    return totalCents > options.mandate_cents ? "over_mandate" : null;
  }
  return other ?? null;
}

/** "Agent pays from my wallet (Visa ····4242, limit CHF 200)". */
export function walletChoiceLabel(options: PaymentOptions, viewer?: string | null): string {
  const whose = !viewer || viewer === options.payer ? "my wallet" : `${options.payer}'s wallet`;
  const parts = [
    options.card ? cardLabel(options.card) : null,
    options.mandate_cents != null ? `limit ${formatCentsShort(options.mandate_cents)}` : null,
  ].filter(Boolean);
  return `Agent pays from ${whose}${parts.length ? ` (${parts.join(", ")})` : ""}`;
}

/** The model the backend uses without a choice on screen. */
function flowModel(payments: string | undefined, flow: PaymentsFlow | undefined): PaymentModel {
  if (payments !== "stripe_test") return "agent";
  return flow === "wallet" ? "wallet" : "checkout";
}

/** True once `approveBefore` has passed: the server would refuse the approval. */
export function isExpired(approveBefore: string | null | undefined, now: number): boolean {
  if (!approveBefore) return false;
  const at = parseInstant(approveBefore);
  return !Number.isNaN(at) && at <= now;
}

/** Current time, refreshed every `everyMs` so the deadline chip stays roughly current. */
function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}

/** One leg as a route line: ZUE 07:02 ──▸ BER 07:58 (full station names in the title). */
export function Leg({ option, currency }: { option: Option; currency: string }) {
  const codes = stationCodes(option.train_id);
  const legName = option.leg === "outbound" ? "Outbound" : "Return";
  const route = codes ? `${stationName(codes[0])} → ${stationName(codes[1])}` : option.train_id;
  return (
    <li className="leg" aria-label={`${legName} leg, ${route}`} title={route} data-leg={option.leg}>
      <span className="leg-day">
        <Text type="supporting" color="secondary">
          {formatDay(option.depart)}
        </Text>
      </span>
      <span className="leg-route">
        <span className="leg-stop">
          {codes && <Text type="code">{codes[0]}</Text>}
          <Text weight="semibold" hasTabularNumbers>
            {formatTime(option.depart)}
          </Text>
        </span>
        <span className="leg-line" aria-hidden="true" />
        <span className="leg-stop">
          {codes && <Text type="code">{codes[1]}</Text>}
          <Text weight="semibold" hasTabularNumbers>
            {formatTime(option.arrive)}
          </Text>
        </span>
      </span>
      <span className="leg-price">
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {fareClassLabel(option.fare_id)}
          {option.fare_type === "saver" ? " saver" : ""} · {formatCents(option.price_cents, currency)}
        </Text>
      </span>
    </li>
  );
}

const LEG_NAME = { outbound: "Outbound", return: "Return" } as const;

/** "Outbound · Zurich HB → Bern · Tue 1 Dec 2026" for a leg's radio group. */
function legHeading(leg: LegOffer): string {
  const first = leg.options[0];
  const codes = first ? stationCodes(first.train_id) : null;
  const route = codes ? ` · ${stationName(codes[0])} → ${stationName(codes[1])}` : "";
  const day = first ? ` · ${formatDay(first.depart)}` : "";
  return `${LEG_NAME[leg.leg]}${route}${day}`;
}

/** How many trains a leg shows before "Show all". */
export const COLLAPSED_OPTIONS = 4;

const minutesOf = (o: Option) => durationMinutes(o.depart, o.arrive) ?? Number.POSITIVE_INFINITY;
const byDepart = (a: Option, b: Option) => (a.depart < b.depart ? -1 : a.depart > b.depart ? 1 : 0);

/** The shortest journey (ties: the earliest); null when there is nothing to compare. */
export function fastestOption(options: Option[]): Option | null {
  return [...options].sort((a, b) => minutesOf(a) - minutesOf(b) || byDepart(a, b))[0] ?? null;
}

/** True when some option is strictly faster than another: only then is "fastest" worth saying. */
function durationsDiffer(options: Option[]): boolean {
  return new Set(options.map(minutesOf)).size > 1;
}

/**
 * The rows of a collapsed leg, in departure order: always the recommended (cheapest) one, the
 * fastest one and the one currently selected, then the earliest others up to `limit`.
 */
export function collapsedOptions(
  options: Option[],
  recommendedFareId: string,
  selectedFareId?: string,
  limit: number = COLLAPSED_OPTIONS,
): Option[] {
  const sorted = [...options].sort(byDepart);
  const keep = new Set<string>();
  for (const id of [recommendedFareId, fastestOption(sorted)?.fare_id, selectedFareId]) {
    if (id && sorted.some((o) => o.fare_id === id)) keep.add(id);
  }
  for (const o of sorted) {
    if (keep.size >= limit) break;
    keep.add(o.fare_id);
  }
  return sorted.filter((o) => keep.has(o.fare_id));
}

/** "IC 8 · direct": every train in this lab is direct (connections with a change are never offered). */
export function trainLine(option: Option): string {
  return `${option.product ?? "Train"} · direct`;
}

/** "12 trains in your window · showing 4". */
export function countLine(total: number, shown: number): string {
  const trains = `${total} train${total === 1 ? "" : "s"} in your window`;
  if (shown >= total) return total > COLLAPSED_OPTIONS ? `${trains} · showing all` : trains;
  return `${trains} · showing ${shown}`;
}

/** Everything a row shows, as one sentence for its radio's accessible name. */
export function optionName(option: Option, currency: string, cheapest: boolean, fastest = false): string {
  const duration = formatDuration(option.depart, option.arrive);
  return [
    `${formatTime(option.depart)} to ${formatTime(option.arrive)}`,
    duration,
    option.product ?? "",
    "direct",
    fareClassLabel(option.fare_id),
    option.fare_type,
    formatCents(option.price_cents, currency),
    cheapest ? "cheapest" : "",
    fastest ? "fastest" : "",
  ]
    .filter(Boolean)
    .join(", ");
}

/** One leg's alternatives as a radio group; the recommended (cheapest) one is labelled. */
function LegChoice({
  leg,
  currency,
  value,
  onChange,
  isDisabled,
}: {
  leg: LegOffer;
  currency: string;
  value: string;
  onChange: (fareId: string) => void;
  isDisabled: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const all = [...leg.options].sort(byDepart);
  const rows = expanded ? all : collapsedOptions(all, leg.recommended_fare_id, value);
  const fastest = durationsDiffer(all) ? fastestOption(all)?.fare_id : undefined;
  const hidden = all.length - rows.length;
  return (
    <div className="leg-choice" data-leg={leg.leg} data-testid={`leg-choice-${leg.leg}`}>
      <RadioList
        id={listId}
        label={legHeading(leg)}
        description={windowLine(leg.window, leg.window_defaulted)}
        value={value}
        onChange={onChange}
        isDisabled={isDisabled}
        size="sm"
      >
        {rows.map((o) => {
          const cheapest = o.fare_id === leg.recommended_fare_id;
          const isFastest = o.fare_id === fastest;
          return (
            <RadioListItem
              key={o.fare_id}
              value={o.fare_id}
              aria-label={optionName(o, currency, cheapest, isFastest)}
              label={
                <span className="leg-option">
                  <Text weight="semibold" hasTabularNumbers className="leg-option-time">
                    {formatTime(o.depart)}–{formatTime(o.arrive)}
                  </Text>
                  <Text type="supporting" color="secondary" hasTabularNumbers data-testid="train-line">
                    {formatDuration(o.depart, o.arrive)} · {trainLine(o)}
                  </Text>
                </span>
              }
              endContent={
                <span className="leg-option-end">
                  <span className="leg-option-badges">
                    <Badge
                      variant={o.fare_type === "saver" ? "info" : "neutral"}
                      label={o.fare_type}
                      data-testid="fare-type"
                    />
                    {cheapest && <Badge variant="success" label="cheapest" data-testid="cheapest" />}
                    {isFastest && <Badge variant="neutral" label="fastest" data-testid="fastest" />}
                  </span>
                  <Text hasTabularNumbers className="leg-option-price">
                    {formatCents(o.price_cents, currency)}
                  </Text>
                </span>
              }
            />
          );
        })}
      </RadioList>
      <div className="leg-count">
        <Text type="supporting" color="secondary" data-testid={`leg-count-${leg.leg}`}>
          {countLine(all.length, rows.length)}
        </Text>
        {(hidden > 0 || expanded) && (
          <Button
            label={expanded ? "Show fewer" : "Show all"}
            size="sm"
            variant="ghost"
            aria-expanded={expanded}
            aria-controls={listId}
            onClick={() => setExpanded((x) => !x)}
          />
        )}
      </div>
    </div>
  );
}

/** Selection key for the quote cache. */
const keyOf = (fareIds: string[]) => fareIds.join("|");

/** The server answered for exactly this selection: one option per leg, same fare ids, in order. */
function quoteMatches(quote: Quote, fareIds: string[]): boolean {
  return keyOf(quote.options.map((o) => o.fare_id)) === keyOf(fareIds);
}

/**
 * Source and trip shape, e.g. "Demo Rail · round trip · times local (Zurich)". Trains from the
 * live timetable (ids "LV-…") say so, and that their fares are still simulated.
 */
export function tripLine(quote: Quote): string {
  const legs = quote.options.map((o) => o.leg);
  const shape = legs.includes("outbound") && legs.includes("return") ? "round trip" : "one way";
  const live = quote.options.some((o) => o.train_id.startsWith("LV-"));
  const source = live ? "Live timetable · simulated fares" : "Demo Rail";
  return `${source} · ${shape} · times local (Zurich)`;
}

/** "[0].notes: ignore (all )?(previous|prior) instructions" -> where and which pattern. */
function splitFinding(f: string): { at: string; pattern: string } {
  const i = f.indexOf(": ");
  return i < 0 ? { at: "", pattern: f } : { at: f.slice(0, i), pattern: f.slice(i + 2) };
}

/**
 * A guard pattern in words: optional groups dropped, the first alternative kept, so
 * "ignore (all )?(previous|prior) instructions" reads "ignore previous instructions".
 */
export function readablePattern(pattern: string): string {
  return pattern
    .replace(/\([^()]*\)\?/g, "")
    .replace(/\(([^()|]*)(\|[^()]*)?\)/g, "$1")
    .replace(/\\(.)/g, "$1")
    .replace(/[?*+^$]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** The flagged tool results the reviewer is asked to check, from their audit entries. */
function FlaggedEvidence({ entries, onShow }: { entries: AuditEntry[]; onShow?: () => void }) {
  if (entries.length === 0) return null;
  return (
    <div className="flag-evidence" data-testid="flag-evidence">
      <ul>
        {entries.map((e) => {
          const findings = Array.isArray(e.data["findings"]) ? (e.data["findings"] as unknown[]).map(String) : [];
          const tool = typeof e.data["tool"] === "string" ? e.data["tool"] : "tool";
          return (
            <li key={e.seq}>
              <Text type="supporting" weight="semibold">
                #{e.seq} · {tool}
              </Text>
              {findings.map((f) => {
                const { at, pattern } = splitFinding(f);
                return (
                  <span key={f} className="flag-finding" title={f}>
                    <Text type="supporting" color="secondary">
                      {at && <span className="mono">{at}</span>}
                      {at ? " matched " : "matched "}“{readablePattern(pattern)}”
                    </Text>
                  </span>
                );
              })}
            </li>
          );
        })}
      </ul>
      {onShow && (
        <Button
          label={`Show flagged results in chain (${entries.map((e) => `#${e.seq}`).join(", ")})`}
          size="sm"
          variant="secondary"
          icon={<ArrowDownRight size="1em" aria-hidden="true" />}
          onClick={onShow}
        />
      )}
    </div>
  );
}

function DeadlineChip({ approveBefore, now }: { approveBefore: string | null; now: number }) {
  if (!approveBefore) {
    return <Badge data-testid="approve-before" variant="error" label="offer has no expiry: approval will fail" />;
  }
  const left = parseInstant(approveBefore) - now;
  if (left <= 0) {
    return (
      <span title={approveBefore} data-testid="approve-before" className="deadline">
        <Badge
          variant="error"
          icon={<Icon icon="clock" size="xsm" color="inherit" />}
          label={`expired at ${formatClock(approveBefore)}`}
        />
      </span>
    );
  }
  const urgent = left < EXPIRY_WARNING_MS;
  const label = urgent ? formatCountdown(approveBefore, now) : `approve by ${formatApproveBy(approveBefore, now)}`;
  return (
    <span title={approveBefore} data-testid="approve-before" className="deadline">
      <Badge
        variant={left <= 0 ? "error" : urgent ? "warning" : "neutral"}
        icon={<Icon icon="clock" size="xsm" color="inherit" />}
        label={label}
      />
    </span>
  );
}

export function ApprovalCard({
  quote,
  offer,
  onQuote,
  payments,
  paymentsFlow,
  paymentOptions,
  viewer,
  approveBefore,
  flags = [],
  flaggedEntries = [],
  onShowFlagged,
  request,
  onRequestNew,
  onDecision,
  disabled = false,
  headingRef,
  cardRef,
}: ApprovalCardProps) {
  // Which decision is in flight, if any: that button shows a spinner, the other is disabled.
  const [inFlight, setInFlight] = useState<boolean | null>(null);
  // Ref guards against a double click landing before React re-renders the disabled state.
  const inFlightRef = useRef(false);
  const flagged = flags.length > 0;
  const injections = flags.filter((f) => f.startsWith("injection_flagged:")).length;
  const reviewLabel = hasInjectionFlag(flags)
    ? `I checked the ${injections} flagged tool result${injections === 1 ? "" : "s"}; the quote uses only structured fields`
    : "I checked the flags raised on this run";
  const [reviewed, setReviewed] = useState(false);
  const now = useNow(15_000);
  const expired = isExpired(approveBefore, now);
  const helperId = useId();

  // The selection: one fare id per leg, the recommended ones first.
  const offered = offer && offer.legs.length > 0 ? offer : null;
  const recommended = offered ? offered.legs.map((l) => l.recommended_fare_id) : quote.options.map((o) => o.fare_id);
  const [selection, setSelection] = useState<string[]>(recommended);
  // Server quotes per selection; the recommended one came with the interrupt.
  const [quotes, setQuotes] = useState<Record<string, Quote>>(() => ({ [keyOf(recommended)]: quote }));
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const current: Quote | undefined = quotes[keyOf(selection)];
  const pricing = !current && quoteError === null;
  const latest = useRef(keyOf(selection));

  // How the total is paid: offered only when the backend allows both models for this run.
  const options = payments === "stripe_test" ? (paymentOptions ?? null) : null;
  const offersChoice = !!options && options.models.includes("wallet") && options.models.includes("checkout");
  // The approver's pick; until they pick, the server's proposal (which may arrive after mount).
  const [model, setModel] = useState<PaymentModel | null>(null);
  const blocked = options ? walletReason(options, (current ?? quote).total_cents) : null;
  const picked: PaymentModel = model ?? options?.default ?? flowModel(payments, paymentsFlow);
  const chosen: PaymentModel =
    picked === "wallet" && blocked && options?.models.includes("checkout") ? "checkout" : picked;
  const limitText = options?.mandate_cents != null ? formatCentsShort(options.mandate_cents, quote.currency) : null;
  const walletInfo =
    options?.card && options.mandate_cents != null && !blocked
      ? { card: options.card, limitCents: options.mandate_cents, currency: quote.currency }
      : null;

  async function choose(index: number, fareId: string) {
    const next = selection.map((f, i) => (i === index ? fareId : f));
    const key = keyOf(next);
    latest.current = key;
    setSelection(next);
    setQuoteError(null);
    if (quotes[key] || !onQuote) {
      if (!quotes[key]) setQuoteError("No quote for this selection");
      return;
    }
    try {
      const q = await onQuote(next);
      if (!quoteMatches(q, next)) throw new Error("the server quoted a different selection");
      setQuotes((all) => ({ ...all, [key]: q }));
    } catch (e) {
      if (latest.current === key) setQuoteError(e instanceof Error ? e.message : String(e));
    }
  }

  async function decide(approved: boolean) {
    if (disabled || inFlightRef.current) return;
    if (approved && ((flagged && !reviewed) || expired || !current)) return;
    // A rejection needs no quote: it is bound to the recommended one when none is on screen.
    const shown = current ?? quote;
    const fareIds = current ? selection : recommended;
    inFlightRef.current = true;
    setInFlight(approved);
    try {
      if (options && approved) await onDecision(approved, shown.quote_hash, fareIds, chosen);
      else await onDecision(approved, shown.quote_hash, fareIds);
    } finally {
      inFlightRef.current = false;
      setInFlight(null);
    }
  }

  const busy = inFlight !== null;
  const locked = flagged && !reviewed;
  const shown = current ?? null;
  return (
    <Card ref={cardRef} padding={4} aria-label="Approval required" role="region" className="gate-card">
      <div className="gate-card-body">
        <div className="gate-head">
          <StatusDot variant="accent" label="Pending" isPulsing />
          <Heading level={2} ref={headingRef} tabIndex={-1} className="focus-target">
            Approve payment?
          </Heading>
        </div>

        <Text type="supporting" color="secondary" data-testid="trip-line">
          {tripLine(quote)}
          {offered && offered.passengers > 1 ? ` · ${offered.passengers} passengers` : ""}
        </Text>
        {request && (
          <Text type="supporting" color="secondary" as="p" className="request-quote" data-testid="approval-request">
            for “{request}”
          </Text>
        )}

        {flagged && <FlagsBanner flags={flags} phase="review" />}
        {flagged && <FlaggedEvidence entries={flaggedEntries} onShow={onShowFlagged} />}

        {offered ? (
          <div className="leg-choices">
            {offered.legs.map((leg, i) => (
              <LegChoice
                key={leg.leg}
                leg={leg}
                currency={offered.currency}
                value={selection[i]}
                onChange={(fareId) => void choose(i, fareId)}
                isDisabled={disabled || busy}
              />
            ))}
          </div>
        ) : (
          <ul className="legs">
            {quote.options.map((o) => (
              <Leg key={`${o.leg}-${o.fare_id}`} option={o} currency={quote.currency} />
            ))}
          </ul>
        )}

        <div className="gate-total" aria-live="polite">
          <span className="amount">
            <Text type="supporting" color="secondary">
              Total to charge
            </Text>
            <Text type="display-2" weight="semibold" hasTabularNumbers data-testid="quote-total">
              {shown ? formatCents(shown.total_cents, shown.currency) : pricing ? "pricing…" : "unavailable"}
            </Text>
          </span>
          {shown && <InfoTip content={shown.rationale} label="Why this quote" size="sm" />}
        </div>
        {offersChoice && options && (
          <div className="payment-choice" data-testid="payment-choice">
            <RadioList
              label="How to pay"
              value={chosen}
              onChange={(v) => setModel(v as PaymentModel)}
              isDisabled={disabled || busy}
              size="sm"
            >
              <RadioListItem
                value="checkout"
                label={`Pay on ${PROCESSOR} now`}
                description={`You confirm, and authenticate if your bank asks, on ${PROCESSOR}'s page.`}
                data-testid="model-checkout"
              />
              <RadioListItem
                value="wallet"
                label={walletChoiceLabel(options, viewer)}
                description={
                  walletUnavailableLine(blocked, limitText) ??
                  "A single-use token for exactly this total; the card never reaches the agent."
                }
                isDisabled={!!blocked}
                data-testid="model-wallet"
              />
            </RadioList>
          </div>
        )}
        <Text type="supporting" color="secondary" as="p" data-testid="charge-notice">
          {chargeNotice(payments, chosen, walletInfo)}
        </Text>
        {quoteError && (
          <Text type="supporting" color="secondary" as="p" role="alert" data-testid="quote-error">
            Could not price this selection ({quoteError}). Pick another train or reject.
          </Text>
        )}

        <div className="gate-meta">
          {approveBefore !== undefined && <DeadlineChip approveBefore={approveBefore} now={now} />}
          {shown && (
            <span className="gate-hash" data-testid="quote-hash">
              <Text type="supporting" color="secondary">
                quote
              </Text>
              <HashChip hash={shown.quote_hash} label="quote hash" />
            </span>
          )}
        </div>

        {flagged && (
          <CheckboxInput
            label={reviewLabel}
            value={reviewed}
            onChange={(checked) => setReviewed(checked)}
            isDisabled={disabled || busy}
          />
        )}

        <div className="gate-actions">
          <Button
            label="Approve"
            variant="primary"
            size="lg"
            icon={locked ? <Lock size="1em" aria-hidden="true" data-testid="approve-lock" /> : undefined}
            aria-describedby={locked && !expired ? helperId : undefined}
            isLoading={inFlight === true}
            isDisabled={disabled || inFlight === false || locked || expired || !shown}
            onClick={() => void decide(true)}
          />
          <Button
            label="Reject"
            variant="secondary"
            size="lg"
            isLoading={inFlight === false}
            isDisabled={disabled || inFlight === true}
            onClick={() => void decide(false)}
          />
        </div>
        {expired && (
          <div className="gate-expired" data-testid="approval-expired">
            <Text type="supporting" color="secondary" as="p">
              The offer expired, so this quote can no longer be approved. No seat was held and nothing was charged.
            </Text>
            {onRequestNew && (
              <Button
                label="Request a new quote"
                size="sm"
                variant="secondary"
                icon={<RotateCcw size="1em" aria-hidden="true" />}
                onClick={onRequestNew}
              />
            )}
          </div>
        )}
        {locked && !expired && (
          <Text id={helperId} type="supporting" color="secondary" data-testid="approve-helper">
            Tick the review box to unlock
          </Text>
        )}
        {busy && (
          <Text type="supporting" color="secondary" role="status">
            Sending decision…
          </Text>
        )}
      </div>
    </Card>
  );
}
