// Mirrors the Python models served by api/server.py. Field names stay snake_case on purpose.

export type Leg = "outbound" | "return";

/** agents/state.py: Option */
export interface Option {
  leg: Leg;
  train_id: string;
  /** ISO datetime */
  depart: string;
  /** ISO datetime */
  arrive: string;
  fare_id: string;
  /** Integer cents, never a float amount. */
  price_cents: number;
  /** "saver": off-peak 2nd class, non-refundable; "standard" otherwise. */
  fare_type: FareType;
  /** Train product as the timetable names it ("IC", "IC 8"); null when unknown. Every train here is direct. */
  product?: string | null;
}

export type FareType = "standard" | "saver";

/** agents/state.py: TimeWindow. Local wall-clock "HH:MM:SS"; null is an open bound. */
export interface TimeWindow {
  earliest: string | null;
  latest: string | null;
}

/** agents/state.py: LegOffer. One option per train in the window, earliest first. */
export interface LegOffer {
  leg: Leg;
  window: TimeWindow;
  /** True when the request named no time and code applied the default earliest departure. */
  window_defaulted: boolean;
  options: Option[];
  recommended_fare_id: string;
}

/** agents/state.py: Offer. Everything the human may choose from; nothing outside it is charged. */
export interface Offer {
  legs: LegOffer[];
  passengers: number;
  currency: string;
  /** ISO datetime; approvals at or after it are rejected. */
  expires_at: string;
}

/** agents/state.py: Quote */
export interface Quote {
  options: Option[];
  total_cents: number;
  currency: string;
  rationale: string;
  quote_hash: string;
}

/** controls/audit_log.py: AuditEntry */
export interface AuditEntry {
  seq: number;
  ts: string;
  run_id: string;
  actor: string;
  event: string;
  data: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

/** controls/audit_log.py: VerifyResult */
export interface VerifyResult {
  ok: boolean;
  entries: number;
  first_bad_seq: number | null;
  reason: string | null;
}

export type RunStatus =
  | "running"
  | "needs_clarification"
  | "answered"
  | "awaiting_approval"
  /** Hosted Checkout: the customer pays on the processor's page (RunView.checkout). */
  | "awaiting_payment"
  | "rejected"
  | "completed"
  | "refused"
  | "failed";

/** Payload of the approval node's interrupt(). */
export interface ApprovalInterrupt {
  /** The recommended selection (cheapest per leg; ties: shortest journey, then earliest). */
  quote: Quote;
  quote_hash: string;
  offer: Offer;
  /** ISO datetime when the offer expires; approvals at or after it are rejected. */
  approve_before: string | null;
}

/** api/server.py: Payment (the captured payment) */
export interface Payment {
  payment_id: string;
  status: string;
  /** Integer cents, never a float amount. */
  amount_cents: number;
  currency: string;
  /** The processor's public receipt page (no login needed); null for simulated payments. */
  receipt_url?: string | null;
}

/** api/server.py: Checkout. A hosted Checkout Session waiting for the customer. */
export interface Checkout {
  session_id: string;
  /** The processor's own payment page (https). */
  checkout_url: string;
  /** ISO datetime; the processor closes the page then. */
  expires_at: string;
  /** Integer cents, exactly the approved total. */
  amount_cents: number;
  currency: string;
}

/**
 * agents/persistence.py: Settlement. What the card processor reported after the capture
 * (webhook or reconciliation): "pending" until it reports. Null without a processor payment
 * (no payment, or a simulated one).
 */
export type Settlement = "confirmed" | "refunded" | "disputed" | "failed" | "pending";

/** api/server.py: response of POST /runs, GET /runs/{run_id}, POST /runs/{run_id}/approval */
export interface RunView {
  run_id: string;
  status: RunStatus;
  quote: Quote | null;
  interrupt: ApprovalInterrupt | null;
  trajectory: string[];
  payment: Payment | null;
  /** Run-level flags, e.g. "injection_flagged:travel_search.search_trains". */
  flags: string[];
  settlement: Settlement | null;
  /** While status is "awaiting_payment": the processor's payment page; null otherwise. */
  checkout?: Checkout | null;
  /** The payment model that ran (or is running); null before the payment step. */
  payment_model?: PaymentModel | null;
  /** Why a wallet payment fell back to the hosted Checkout; null otherwise. */
  payment_fallback?: WalletFallback | null;
  /** While awaiting approval: the payment models the approver may choose; null otherwise. */
  payment_options?: PaymentOptions | null;
  /** A completed wallet payment: the card and the limit it ran under. */
  wallet_payment?: WalletPayment | null;
}

/**
 * Who executes the payment: "checkout" (the customer pays on the processor's hosted page),
 * "wallet" (the agent pays with a scoped single-use token from the user's wallet) or "agent"
 * (the simulated provider, no processor).
 */
export type PaymentModel = "checkout" | "wallet" | "agent";

/** Why the wallet could not pay (agents/booking.py, mcp_servers/wallet/vault.py). */
export type WalletFallback =
  | "authentication_required"
  | "over_mandate"
  | "no_saved_card"
  | "card_expired"
  | "card_declined"
  | "currency_not_supported"
  | "wallet_unavailable";

/** api/server.py: WalletCard. Brand, last four digits and expiry only; never the number. */
export interface WalletCard {
  brand: string;
  last4: string;
  exp_month: number;
  exp_year: number;
  created_at?: string | null;
}

/** api/server.py: PaymentOptions, while a run awaits approval. */
export interface PaymentOptions {
  models: PaymentModel[];
  /** The proposed model: the wallet when it can pay this total, else the hosted Checkout. */
  default: PaymentModel | null;
  /** Whose wallet would pay (the requester; "local-dev" without authentication). */
  payer: string;
  card?: WalletCard | null;
  /** Spending limit per booking, integer cents. */
  mandate_cents?: number | null;
  /** Why the wallet cannot pay the recommended total now. */
  wallet_unavailable?: WalletFallback | null;
}

/** api/server.py: WalletPayment */
export interface WalletPayment {
  brand: string;
  last4: string;
  /** The limit the payment ran under, integer cents. */
  mandate_cents: number;
}

/** api/server.py: WalletView (GET /wallet and the wallet POSTs). */
export interface WalletView {
  user: string;
  /** The wallet payment model is allowed (test-mode processor and PAYMENTS_FLOW). */
  enabled: boolean;
  card: WalletCard | null;
  /** Spending limit per booking, integer cents. */
  mandate_cents: number;
  currency: string;
  min_mandate_cents: number;
  max_mandate_cents: number;
  /** After completing a setup: "saved", "pending", "expired", "refused" or "not_found". */
  setup_state?: string | null;
}

/** agents/persistence.py: RunSummary, one row of GET /runs (newest first). */
export interface RunSummary {
  run_id: string;
  /** ISO datetime (UTC) */
  created_at: string;
  /** ISO datetime (UTC) of the last start, pause or finish */
  updated_at: string;
  /** The user's request after PII redaction; the raw text is never stored. */
  request_redacted: string;
  status: RunStatus;
  /** Integer cents of the run's quote; null before pricing. */
  total_cents: number | null;
  currency: string | null;
  payment_id: string | null;
  flags: string[];
  settlement: Settlement | null;
  /** Signed-in user who created the run; null with API authentication disabled. */
  requester?: string | null;
}

/** api/server.py: Me (GET /auth/me, POST /auth/login) */
export interface Me {
  name: string;
  role: "requester" | "approver" | "both";
  auth_enabled: boolean;
  /** REQUIRE_SEPARATE_APPROVER: the requester of a run cannot approve it. */
  require_separate_approver: boolean;
}

/** api/server.py: body of POST /runs/{run_id}/approval */
export interface ApprovalRequest {
  approved: boolean;
  /** Kept for compatibility: with authentication on, the server records the signed-in user. */
  approver: string;
  /** Hash of the quote for `fare_ids` (as returned by POST /runs/{run_id}/quote). */
  quote_hash: string;
  /** One offered fare id per leg, in leg order; omitted means the recommended selection. */
  fare_ids?: string[];
  /** How the approved total is paid; omitted means the server's proposal. */
  payment_model?: PaymentModel;
}

/** api/server.py: body of POST /runs/{run_id}/quote (a pure recompute, nothing is booked). */
export interface QuoteRequest {
  fare_ids: string[];
}

/** Graph nodes named in node_started / node_finished events (api/server.py). */
export type ServerNode =
  | "supervisor"
  | "planner"
  | "pricing"
  | "approval"
  | "booking_hold"
  | "booking_pay"
  | "await_payment";

/**
 * One server-sent event of POST /runs/stream and POST /runs/{run_id}/approval/stream.
 * Events carry node, agent and tool names, flags and statuses only: never args or results.
 */
export type RunEvent =
  | { type: "run_started"; run_id: string }
  | { type: "node_started"; node: ServerNode | string }
  | { type: "node_finished"; node: ServerNode | string; interrupted: boolean }
  | { type: "tool_call"; agent: string; tool: string }
  | { type: "tool_result"; agent: string; tool: string; flagged: boolean }
  | { type: "tool_error"; agent: string; tool: string }
  | { type: "policy_denied"; agent: string; tool: string; reason: string }
  | { type: "status"; status: RunStatus }
  | ({ type: "run_view" } & RunView)
  | { type: "error"; error_type?: string; status_code?: number; detail?: string };

/** Which parts of a run are real (GET /mode). Fares and seat holds are always simulated. */
export interface Mode {
  timetable: "simulated" | "live";
  payments: "simulated" | "stripe_test";
  /** "agent" (simulated), or with the processor "checkout", "wallet" or "both". */
  payments_flow?: PaymentsFlow;
  /** The payment models an approver may choose. */
  payment_models?: PaymentModel[];
}

export type PaymentsFlow = "checkout" | "wallet" | "both" | "agent";
