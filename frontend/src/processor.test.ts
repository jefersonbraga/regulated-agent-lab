import { describe, expect, it } from "vitest";
import {
  cardExpiry,
  cardLabel,
  chargeNotice,
  dashboardUrl,
  fallbackLine,
  isProcessorPayment,
  isReceiptUrl,
  paymentStep,
  walletUnavailableLine,
} from "./processor";

const VISA = { brand: "visa", last4: "4242", exp_month: 3, exp_year: 2034 };

describe("processor", () => {
  it("tells processor payments from simulated ones", () => {
    expect(isProcessorPayment("pi_3UeLuCLVvog9Cpxr1YsaC9ma")).toBe(true);
    expect(isProcessorPayment("PAY-c8ad2a4c0725")).toBe(false);
    expect(isProcessorPayment("pi_../../x")).toBe(false);
  });

  it("links to the test-mode dashboard only", () => {
    expect(dashboardUrl("pi_abc")).toBe("https://dashboard.stripe.com/test/payments/pi_abc");
  });

  it("says what approving will do before the decision", () => {
    expect(chargeNotice("stripe_test")).toContain("confirm the payment on Stripe's page");
    expect(chargeNotice("simulated")).toContain("simulated payment");
    expect(chargeNotice(undefined)).toContain("simulated payment");
  });

  it("says the agent pays from the wallet with a single-use token", () => {
    const notice = chargeNotice("stripe_test", "wallet", { card: VISA, limitCents: 20000, currency: "CHF" });
    expect(notice).toContain("the agent pays from the wallet with a single-use token");
    expect(notice).toContain("Visa ····4242, limit CHF 200");
    expect(notice).toContain("If the bank asks for authentication, you confirm on Stripe's page instead.");
    // No saved card known: the hosted page is what will happen.
    expect(chargeNotice("stripe_test", "wallet", null)).toContain("confirm the payment on Stripe's page");
  });

  it("names cards by brand and last four digits only", () => {
    expect(cardLabel(VISA)).toBe("Visa ····4242");
    expect(cardLabel({ brand: "mastercard", last4: "4444" })).toBe("Mastercard ····4444");
    expect(cardLabel({ brand: "cartes_bancaires", last4: "0001" })).toBe("Cartes bancaires ····0001");
    expect(cardExpiry(VISA)).toBe("03/2034");
  });

  it("explains every wallet fallback", () => {
    expect(fallbackLine("authentication_required")).toBe("Your bank asked for authentication: confirm on Stripe's page.");
    expect(fallbackLine("over_mandate", "CHF 200.00")).toContain("above your wallet limit of CHF 200.00");
    expect(fallbackLine("no_saved_card")).toContain("No card is saved");
    expect(fallbackLine("card_declined")).toContain("declined");
    expect(fallbackLine(null)).toBeNull();
    expect(walletUnavailableLine("over_mandate", "CHF 50.00")).toBe("Above your limit of CHF 50.00.");
    expect(walletUnavailableLine(undefined)).toBeNull();
  });

  it("says the customer pays on the processor's page in the checkout flow", () => {
    expect(chargeNotice("stripe_test", "checkout")).toBe(
      "On approval: you will confirm the payment on Stripe's page (test mode, use card 4242 4242 4242 4242, any future date, any CVC).",
    );
    // The simulated provider never uses the hosted page.
    expect(chargeNotice("simulated", "checkout")).toContain("simulated payment");
  });

  it("names who moves the money inside the payments boundary", () => {
    expect(paymentStep("checkout")).toBe("Customer pays on Stripe");
    expect(paymentStep("wallet")).toBe("Agent pays with a wallet token");
    expect(paymentStep("both")).toBe("Stripe page or wallet token");
    expect(paymentStep("both", "wallet")).toBe("Agent paid with a wallet token");
    expect(paymentStep("both", "checkout")).toBe("Customer pays on Stripe");
    expect(paymentStep(undefined)).toBe("Simulated capture");
  });

  it("links only processor-hosted https receipts", () => {
    expect(isReceiptUrl("https://pay.stripe.com/receipts/payment/abc")).toBe(true);
    expect(isReceiptUrl("http://pay.stripe.com/receipts/x")).toBe(false);
    expect(isReceiptUrl("https://evil.example/stripe.com/")).toBe(false);
    expect(isReceiptUrl("javascript:alert(1)")).toBe(false);
    expect(isReceiptUrl(null)).toBe(false);
  });
});
