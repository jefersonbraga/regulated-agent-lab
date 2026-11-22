import type { Ref } from "react";
import { ArrowDownRight, RotateCcw } from "lucide-react";
import { Badge, type BadgeVariant } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import type { AuditEntry, RunView, Settlement } from "../types";
import { formatCents, formatCentsShort, formatClock } from "../format";
import { Leg, tripLine } from "./ApprovalCard";
import { HashChip } from "./HashChip";
import { cardLabel, dashboardUrl, isProcessorPayment, isReceiptUrl, PROCESSOR } from "../processor";
import { LINKS, NETWORK_STATIONS, formatMinutes } from "../network";
import { StatusMark, type MarkTone } from "./StatusMark";

export interface OutcomeCardProps {
  run: RunView;
  /** The run's approval_decision audit entry, once the audit log has loaded. */
  approval?: AuditEntry;
  /** Jumps to the approval block in the audit chain. */
  onViewApproval?: () => void;
  /** The run's policy_denied audit entry, if the gate refused a call. */
  denial?: AuditEntry;
  /** Jumps to the denial block in the audit chain. */
  onViewDenial?: () => void;
  /** The run's needs_clarification audit entry: why the request could not be planned. */
  clarification?: AuditEntry;
  /** Why the run failed, in readable words (failed runs only). */
  failure?: string | null;
  /** Audit entries recorded for this run, for the failed ending's reassurance line. */
  auditCount?: number;
  /** The run's latest processor audit entry (webhook or reconciliation), for its time. */
  settlementEntry?: AuditEntry;
  /** Resends the request that failed. */
  onRetry?: () => void;
  /** Confirms a pending processor capture now, by reading it back from the processor. */
  onCheckProcessor?: () => void;
  /** The booking agent's checkout_ended audit entry: a hosted payment page that closed unpaid. */
  checkoutEnd?: AuditEntry;
  headingRef?: Ref<HTMLHeadingElement>;
}

const ENDINGS: Record<string, { title: string; line: string; tone: MarkTone }> = {
  rejected: { title: "Not approved", line: "No charge was made.", tone: "warning" },
  refused: { title: "Refused by policy", line: "The policy gate stopped the run. No charge was made.", tone: "refused" },
  failed: { title: "Run failed", line: "A technical error ended the run.", tone: "error" },
  answered: { title: "Routes you can book", line: "", tone: "success" },
  needs_clarification: {
    title: "Can't book that trip",
    line: "Give origin, destination and date for a train between Zurich HB, Bern, Geneva and Basel.",
    tone: "warning",
  },
};

function str(entry: AuditEntry | undefined, key: string): string | null {
  const v = entry?.data[key];
  return typeof v === "string" ? v : null;
}

const STATION_LIST = "Zurich HB, Bern, Geneva and Basel";

/**
 * Why a request could not be planned, from the audited reason code (never from free text), with
 * what to change. Null when the reason is unknown, so the card falls back to its generic line.
 */
export function clarificationLine(entry: AuditEntry): string | null {
  const reason = str(entry, "reason");
  const errors = Array.isArray(entry.data["errors"]) ? (entry.data["errors"] as { loc?: unknown }[]) : [];
  const about = (field: string) => errors.some((e) => Array.isArray(e.loc) && e.loc.includes(field));
  switch (reason) {
    case "unsupported intent":
      return `This console only books train trips between ${STATION_LIST}. Ask for a train between two of them.`;
    case "unknown or same station":
      return `Pick two different stations among ${STATION_LIST}.`;
    case "invalid passenger count":
      return "A booking is for 1 to 9 passengers.";
    case "passenger count unclear":
      return "The number of travellers was unclear. Say how many people travel (1 to 9).";
    case "invalid extraction":
      if (about("passengers")) return "A booking is for 1 to 9 passengers.";
      if (about("trip")) return "Check the dates: the return must be on or after the outbound day.";
      return `Say where from, where to and the date, for a train between ${STATION_LIST}.`;
    case "empty message":
      return "Type a trip request first.";
    case "no direct train":
      return "No direct train on that route and date. Try another date or another pair of stations.";
    case "timetable unavailable":
      return "The timetable could not be reached. Nothing was booked; try again in a moment.";
    case "no eligible fare":
      return "No 2nd-class fare in the requested time window on that route and date. Try another time or date.";
    case "no train in window":
      return "No train departs in the requested time window on that route and date. Try a wider window or another date.";
    case "fares unavailable":
      return "Fares could not be loaded. Nothing was booked; try again in a moment.";
    default:
      return null;
  }
}

/** The answer to "routes please": every station and direct link, from the fixed network table. */
export function routeLines(): string[] {
  return [
    ...NETWORK_STATIONS.map((s) => `${s.name}: ${s.blurb}.`),
    ...LINKS.map(([a, b, minutes]) => `${a} ⇄ ${b} · typical journey about ${formatMinutes(minutes)}`),
    "Pick two stations on the map above, or type a request such as “One way from Bern to Geneva on 3 December 2026, 1 adult.”",
  ];
}

/** The refusal in words: who tried what, and why the gate said no. */
export function denialLine(denial: AuditEntry): string {
  const tool = str(denial, "tool") ?? "a tool";
  const reason = str(denial, "reason");
  const who = denial.actor === "booking" ? "The booking agent" : `The ${denial.actor} agent`;
  const when = tool === "payments.charge" ? " before approval" : "";
  const charge = tool === "payments.charge" ? " No charge was made." : "";
  return `${who} tried ${tool}${when}. The gate refused it${reason ? `: ${reason}` : ""}.${charge}`;
}

const SETTLEMENT: Record<Settlement, { text: string; variant: BadgeVariant }> = {
  confirmed: { text: "Confirmed by processor", variant: "success" },
  refunded: { text: "Refunded", variant: "warning" },
  disputed: { text: "Dispute opened", variant: "error" },
  failed: { text: "Payment failed at processor", variant: "error" },
  pending: { text: "Awaiting processor confirmation", variant: "neutral" },
};

/**
 * What the card processor reported after the charge, in words: "Confirmed by processor
 * 21:34 UTC", "Refunded CHF 56.00 · 21:40 UTC". The time and refunded amount come from the
 * processor's audit entry; the state itself from the run (the server's settlement).
 */
export function settlementLine(settlement: Settlement, entry?: AuditEntry, currency = "CHF"): string {
  const base = SETTLEMENT[settlement].text;
  if (settlement === "pending" || !entry) return base;
  const amount = entry.data["amount_cents"];
  const refunded =
    settlement === "refunded" && entry.event === "payment_refunded" && Number.isInteger(amount)
      ? ` ${formatCents(amount as number, currency)}`
      : "";
  const time = formatClock(entry.ts);
  return settlement === "confirmed" ? `${base} ${time}` : `${base}${refunded} · ${time}`;
}

/** What the wallet could not do before the customer paid on the processor's page. */
const FALLBACK_OUTCOME: Record<string, string> = {
  authentication_required: "The agent tried your wallet first; your bank asked for authentication.",
  over_mandate: "The total was above your wallet limit.",
  no_saved_card: "No card was saved in your wallet.",
  card_expired: "The card in your wallet had expired.",
  card_declined: "Your saved card was declined.",
};

/**
 * Who paid and how, for a processor payment: the agent from the wallet (under the limit), or
 * the customer on the processor's page (and why, when the wallet was tried first).
 */
export function paidLine(run: RunView): { headline: string; detail: string } {
  if (run.payment_model === "wallet" && run.wallet_payment) {
    const w = run.wallet_payment;
    return {
      headline: `Paid by the agent from your wallet (${cardLabel(w)}) under your ${formatCentsShort(
        w.mandate_cents,
        run.payment?.currency ?? "CHF",
      )} limit.`,
      detail: `A single-use token for exactly this total, ${PROCESSOR} test mode. No real money moved.`,
    };
  }
  if (run.payment_model === "checkout") {
    const why = run.payment_fallback ? (FALLBACK_OUTCOME[run.payment_fallback] ?? "The wallet could not pay.") : null;
    return {
      headline: `Paid by you on ${PROCESSOR}'s page.`,
      detail: `${why ? `${why} ` : ""}Test mode. No real money moved.`,
    };
  }
  return { headline: `Paid through ${PROCESSOR}`, detail: "in test mode. No real money moved." };
}

function Head({ tone, title, headingRef }: { tone: MarkTone; title: string; headingRef?: Ref<HTMLHeadingElement> }) {
  return (
    <div className="gate-head">
      <StatusMark tone={tone} label={title} />
      <Heading level={2} ref={headingRef} tabIndex={-1} className="focus-target">
        {title}
      </Heading>
    </div>
  );
}

/** The run's end state: the booking with its proof, or why it stopped. */
export function OutcomeCard({
  run,
  approval,
  onViewApproval,
  denial,
  onViewDenial,
  clarification,
  failure,
  auditCount,
  settlementEntry,
  onRetry,
  onCheckProcessor,
  checkoutEnd,
  headingRef,
}: OutcomeCardProps) {
  const payment = run.status === "completed" ? run.payment : null;
  if (!payment) {
    const closed = run.status === "failed" ? str(checkoutEnd, "reason") : null;
    const mismatch = run.status === "refused" && run.flags.includes("amount_mismatch");
    const base = ENDINGS[run.status] ?? { title: run.status.replace(/_/g, " "), line: "", tone: "warning" as const };
    const end = closed
      ? { ...base, title: "Payment not completed", tone: "warning" as const }
      : mismatch
        ? { ...base, title: "Payment refused" }
        : base;
    const lines: string[] = [];
    if (closed) {
      lines.push(
        closed === "checkout canceled"
          ? `The payment was cancelled before anyone paid on ${PROCESSOR}'s page. No money moved.`
          : `The ${PROCESSOR} payment page expired before anyone paid. No money moved.`,
      );
      lines.push("The seat holds lapse on their own. Send the request again for a new quote.");
    } else if (mismatch) {
      lines.push(
        `${PROCESSOR} reported a different amount or currency than the approved quote, so the booking was not completed and the run is flagged amount_mismatch.`,
      );
      lines.push("An operator must reconcile it in the processor's dashboard.");
    } else if (run.status === "refused") {
      lines.push(denial ? denialLine(denial) : end.line);
      lines.push(
        "This is the gate working: payments only run after a human approves a quote. Send the request again to get a quote to review.",
      );
    }
    else if (run.status === "failed") {
      lines.push(failure ? `${failure} The run stopped.` : end.line);
      lines.push(
        auditCount
          ? `Nothing was booked or charged. The ${auditCount} audit entr${auditCount === 1 ? "y" : "ies"} below record what ran.`
          : "Nothing was booked or charged.",
      );
    } else if (run.status === "rejected" && run.flags.includes("quote_mismatch")) {
      lines.push(
        "The approval did not match a selection from the offer and its quote hash, so the run stopped before any seat was held.",
      );
      lines.push(end.line);
    } else if (run.status === "answered") {
      lines.push(...routeLines());
    } else if (run.status === "needs_clarification") {
      lines.push((clarification && clarificationLine(clarification)) ?? end.line);
    } else if (end.line) lines.push(end.line);
    return (
      <Card padding={4} role="region" aria-label={end.title} className="gate-card" data-outcome={run.status}>
        <div className="gate-card-body">
          <Head tone={end.tone} title={end.title} headingRef={headingRef} />
          {lines.map((l) => (
            <Text key={l} color="secondary" as="p" data-testid="outcome-line">
              {l}
            </Text>
          ))}
          {(denial && onViewDenial) || onRetry ? (
            <div className="outcome-actions">
              {denial && onViewDenial && (
                <Button
                  label="Show denial in chain"
                  size="sm"
                  variant="secondary"
                  icon={<ArrowDownRight size="1em" aria-hidden="true" />}
                  onClick={onViewDenial}
                />
              )}
              {onRetry && (
                <Button
                  label={run.status === "refused" ? "Send request again" : "Retry request"}
                  size="sm"
                  variant="secondary"
                  icon={<RotateCcw size="1em" aria-hidden="true" />}
                  onClick={onRetry}
                />
              )}
            </div>
          ) : null}
        </div>
      </Card>
    );
  }
  const approver = str(approval, "approver");
  const hash = str(approval, "quote_hash") ?? run.quote?.quote_hash;
  const flagged = run.flags.filter((f) => f.startsWith("injection_flagged:")).length;
  const proof = [
    approver ? `Approved by ${approver}` : "Approved",
    approval ? ` at ${formatClock(approval.ts)}` : "",
    flagged > 0 ? ` after reviewing ${flagged} flagged tool result${flagged === 1 ? "" : "s"}` : "",
    ".",
  ].join("");
  return (
    <Card padding={4} role="region" aria-label="Booked" className="gate-card" data-outcome="completed">
      <div className="gate-card-body">
        <Head tone="success" title="Booked" headingRef={headingRef} />
        {run.quote && (
          <Text type="supporting" color="secondary">
            {tripLine(run.quote)}
          </Text>
        )}
        {run.quote && (
          <ul className="legs" aria-label="Itinerary">
            {run.quote.options.map((o) => (
              <Leg key={`${o.leg}-${o.fare_id}`} option={o} currency={run.quote!.currency} />
            ))}
          </ul>
        )}
        <div className="amount">
          <Text type="supporting" color="secondary">
            Charged
          </Text>
          <Text type="display-2" weight="semibold" hasTabularNumbers data-testid="payment-amount">
            {formatCents(payment.amount_cents, payment.currency)}
          </Text>
          {run.settlement && (
            <span className="settlement" data-testid="settlement" data-settlement={run.settlement}>
              <Badge
                variant={SETTLEMENT[run.settlement].variant}
                label={settlementLine(run.settlement, settlementEntry, payment.currency)}
              />
            </span>
          )}
        </div>
        {isProcessorPayment(payment.payment_id) ? (
          <p className="processor-paid" data-testid="processor-paid" data-model={run.payment_model ?? undefined}>
            <Text type="supporting" weight="semibold">
              {paidLine(run).headline}
            </Text>
            <Text type="supporting" color="secondary">
              {" "}
              {paidLine(run).detail}
            </Text>
          </p>
        ) : (
          <Text type="supporting" color="secondary" as="p" data-testid="simulated-paid">
            Simulated payment: no processor involved.
          </Text>
        )}
        <Text type="supporting" color="secondary" as="p" data-testid="approval-proof">
          {proof}
        </Text>
        <dl className="outcome-facts">
          <div>
            <dt>{isProcessorPayment(payment.payment_id) ? `${PROCESSOR} PaymentIntent` : "Payment ref"}</dt>
            <dd>
              <Text type="code" data-testid="payment-id">
                {payment.payment_id}
              </Text>
            </dd>
          </div>
          {hash && (
            <div>
              <dt>Quote hash</dt>
              <dd>
                <HashChip hash={hash} label="quote hash" />
              </dd>
            </div>
          )}
        </dl>
        {isProcessorPayment(payment.payment_id) && (
          <div className="outcome-actions">
            {isReceiptUrl(payment.receipt_url) && (
              <a
                className="processor-link"
                href={payment.receipt_url}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="receipt-link"
              >
                View {PROCESSOR} receipt ↗
              </a>
            )}
            <a
              className="processor-link"
              href={dashboardUrl(payment.payment_id)}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="processor-link"
            >
              Open in {PROCESSOR} dashboard ↗
            </a>
            {onCheckProcessor && (
              <Button
                label={`Check with ${PROCESSOR}`}
                size="sm"
                variant="secondary"
                onClick={onCheckProcessor}
                data-testid="check-processor"
              />
            )}
          </div>
        )}
        {approval && onViewApproval && (
          <div className="outcome-actions">
            <Button
              label="Show approval in chain"
              size="sm"
              variant="secondary"
              icon={<ArrowDownRight size="1em" aria-hidden="true" />}
              onClick={onViewApproval}
            />
          </div>
        )}
      </div>
    </Card>
  );
}
