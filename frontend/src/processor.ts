/**
 * The card processor behind test-mode payments, named in the UI so a viewer can see where the
 * money step happens. Only test mode exists in this lab: no real money moves.
 */
import { formatCentsShort } from "./format";
import type { PaymentModel, PaymentsFlow, WalletCard } from "./types";

export const PROCESSOR = "Stripe";

/** A processor PaymentIntent id (test-mode captures); simulated payments use "PAY-…". */
export function isProcessorPayment(paymentId: string): boolean {
  return /^pi_[A-Za-z0-9]+$/.test(paymentId);
}

/** The payment's page in the processor's test-mode dashboard (the viewer must be signed in). */
export function dashboardUrl(paymentId: string): string {
  return `https://dashboard.stripe.com/test/payments/${encodeURIComponent(paymentId)}`;
}

/** The processor's public test card number, typed by the customer on its hosted page. */
export const TEST_CARD_NUMBER = "4242 4242 4242 4242";

/** What approving will do, said before the human decides, for the chosen payment model. */
export function chargeNotice(
  payments: "simulated" | "stripe_test" | undefined,
  model: PaymentModel | null | undefined = "checkout",
  wallet?: { card: WalletCard; limitCents: number; currency: string } | null,
): string {
  if (payments !== "stripe_test" || model === "agent") return "On approval: simulated payment, no processor involved.";
  if (model === "wallet" && wallet) {
    return `On approval: the agent pays from the wallet with a single-use token for exactly this total (${cardLabel(wallet.card)}, limit ${formatCentsShort(wallet.limitCents, wallet.currency)}, ${PROCESSOR} test mode). If the bank asks for authentication, you confirm on ${PROCESSOR}'s page instead.`;
  }
  return `On approval: you will confirm the payment on ${PROCESSOR}'s page (test mode, use card ${TEST_CARD_NUMBER}, any future date, any CVC).`;
}

/** Who moves the money, as the stage's payments boundary says it. */
export function paymentStep(flow: PaymentsFlow | undefined, ran?: PaymentModel | null): string {
  if (ran === "wallet") return "Agent paid with a wallet token";
  if (ran === "checkout") return `Customer pays on ${PROCESSOR}`;
  if (flow === "checkout") return `Customer pays on ${PROCESSOR}`;
  if (flow === "wallet") return "Agent pays with a wallet token";
  if (flow === "both") return `${PROCESSOR} page or wallet token`;
  return "Simulated capture";
}

const BRANDS: Record<string, string> = {
  visa: "Visa",
  mastercard: "Mastercard",
  amex: "American Express",
  discover: "Discover",
  diners: "Diners Club",
  jcb: "JCB",
  unionpay: "UnionPay",
};

/** "Visa ····4242": brand and last four digits, the only card details the lab ever shows. */
export function cardLabel(card: { brand: string; last4: string }): string {
  const brand = BRANDS[card.brand] ?? card.brand.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  return `${brand} ····${card.last4}`;
}

/** "12/2034" */
export function cardExpiry(card: { exp_month: number; exp_year: number }): string {
  return `${String(card.exp_month).padStart(2, "0")}/${card.exp_year}`;
}

/** Why the run is on the processor's page instead of the wallet, in words. */
export function fallbackLine(reason: string | null | undefined, limit?: string | null): string | null {
  switch (reason) {
    case "authentication_required":
      return `Your bank asked for authentication: confirm on ${PROCESSOR}'s page.`;
    case "over_mandate":
      return `This total is above your wallet limit${limit ? ` of ${limit}` : ""}: pay on ${PROCESSOR}'s page.`;
    case "no_saved_card":
      return `No card is saved in your wallet: pay on ${PROCESSOR}'s page.`;
    case "card_expired":
      return `The card in your wallet has expired: pay on ${PROCESSOR}'s page.`;
    case "card_declined":
      return `Your saved card was declined: pay on ${PROCESSOR}'s page with another card.`;
    case null:
    case undefined:
      return null;
    default:
      return `The wallet could not pay: pay on ${PROCESSOR}'s page.`;
  }
}

/** Why the wallet option is unavailable on the approval card. */
export function walletUnavailableLine(reason: string | null | undefined, limit?: string | null): string | null {
  switch (reason) {
    case "no_saved_card":
      return "No card saved: add one under Payment method.";
    case "over_mandate":
      return `Above your limit${limit ? ` of ${limit}` : ""}.`;
    case "card_expired":
      return "The saved card has expired.";
    case null:
    case undefined:
      return null;
    default:
      return "The wallet cannot pay this total.";
  }
}

/** Only a processor-hosted https receipt is linked (the server checks the domain too). */
export function isReceiptUrl(url: string | null | undefined): url is string {
  return typeof url === "string" && /^https:\/\/[A-Za-z0-9.-]+\.stripe\.com\//.test(url);
}
