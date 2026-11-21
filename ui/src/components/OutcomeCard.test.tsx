import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { clarificationLine, OutcomeCard } from "./OutcomeCard";
import { FLAGS, QUOTE_HASH, quote } from "../fixtures/events";
import type { AuditEntry, RunView } from "../types";

const run: RunView = {
  run_id: "r1",
  status: "completed",
  quote: null,
  interrupt: null,
  trajectory: [],
  payment: { payment_id: "PAY-c1cfe8bbff25", status: "captured", amount_cents: 5600, currency: "CHF" },
  flags: [],
  settlement: null,
};

const approval: AuditEntry = {
  seq: 21,
  ts: "2026-11-21T13:36:42.088009+00:00",
  run_id: "r1",
  actor: "human",
  event: "approval_decision",
  data: { approved: true, approver: "demo-approver", quote_hash: QUOTE_HASH },
  prev_hash: "0",
  hash: "1",
};

describe("OutcomeCard", () => {
  it("shows the payment and who approved it, when, for which quote", async () => {
    const onViewApproval = vi.fn();
    render(<OutcomeCard run={run} approval={approval} onViewApproval={onViewApproval} />);
    expect(screen.getByRole("region", { name: "Booked" })).toHaveAttribute("data-outcome", "completed");
    expect(screen.getByRole("heading", { name: "Booked" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("payment-amount")).toHaveTextContent("CHF 56.00");
    expect(screen.getByTestId("payment-id")).toHaveTextContent("PAY-c1cfe8bbff25");
    expect(screen.getByTestId("approval-proof")).toHaveTextContent("Approved by demo-approver at 13:36 UTC.");
    // The quote hash from the approval entry, short, with the full value behind Copy.
    const chip = screen.getByRole("button", { name: `Copy quote hash ${QUOTE_HASH}` });
    expect(chip).toHaveTextContent(QUOTE_HASH.slice(0, 8));
    await userEvent.click(screen.getByRole("button", { name: "Show approval in chain" }));
    expect(onViewApproval).toHaveBeenCalledTimes(1);
  });

  it("keeps the itinerary and says which flags the approver reviewed", () => {
    render(<OutcomeCard run={{ ...run, quote, flags: FLAGS }} approval={approval} />);
    const legs = screen.getByRole("list", { name: "Itinerary" });
    expect(legs).toHaveTextContent("Tue 1 Dec 2026ZUE09:02BER09:58");
    expect(legs).toHaveTextContent("Wed 2 Dec 2026BER09:02ZUE09:58");
    expect(screen.getByTestId("approval-proof")).toHaveTextContent(
      "Approved by demo-approver at 13:36 UTC after reviewing 2 flagged tool results.",
    );
  });

  it("says nothing about flags on a clean run", () => {
    render(<OutcomeCard run={run} approval={approval} />);
    expect(screen.getByTestId("approval-proof")).not.toHaveTextContent("reviewed");
  });

  it("explains a rejection caused by a selection outside the offer", () => {
    render(<OutcomeCard run={{ ...run, status: "rejected", payment: null, flags: ["quote_mismatch"] }} />);
    const card = screen.getByRole("region", { name: "Not approved" });
    expect(card).toHaveTextContent("did not match a selection from the offer");
    expect(card).toHaveTextContent("No charge was made.");
  });

  it("works before the audit entry has loaded, falling back to the run's quote hash", () => {
    render(<OutcomeCard run={{ ...run, quote }} />);
    expect(screen.getByTestId("approval-proof")).toHaveTextContent(/^Approved\.$/);
    expect(screen.getByRole("button", { name: `Copy quote hash ${QUOTE_HASH}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show approval in chain" })).not.toBeInTheDocument();
  });

  it.each([
    ["rejected", "Not approved", "No charge was made."],
    ["refused", "Refused by policy", "The policy gate stopped the run. No charge was made."],
    ["failed", "Run failed", "A technical error ended the run."],
    [
      "needs_clarification",
      "Can't book that trip",
      "Give origin, destination and date for a train between Zurich HB, Bern, Geneva and Basel.",
    ],
  ] as const)("explains a %s run without a payment", (status, title, line) => {
    render(<OutcomeCard run={{ ...run, status, payment: null }} />);
    const card = screen.getByRole("region", { name: title });
    expect(card).toHaveAttribute("data-outcome", status);
    expect(card).toHaveTextContent(line);
    expect(screen.queryByTestId("payment-amount")).not.toBeInTheDocument();
  });

  it("names the refused call and its reason, and links to the denial", async () => {
    const onViewDenial = vi.fn();
    const denial: AuditEntry = {
      ...approval,
      actor: "booking",
      event: "policy_denied",
      data: { tool: "payments.charge", reason: "tool requires human approval" },
    };
    render(<OutcomeCard run={{ ...run, status: "refused", payment: null }} denial={denial} onViewDenial={onViewDenial} />);
    expect(screen.getByRole("region", { name: "Refused by policy" })).toHaveTextContent(
      "The booking agent tried payments.charge before approval. The gate refused it: tool requires human approval. No charge was made.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Show denial in chain" }));
    expect(onViewDenial).toHaveBeenCalledTimes(1);
  });

  it("explains a failure in words, says nothing moved and offers a retry", async () => {
    const onRetry = vi.fn();
    render(
      <OutcomeCard
        run={{ ...run, status: "failed", payment: null }}
        failure="Lost connection to the travel tools (McpTransportError)."
        auditCount={4}
        onRetry={onRetry}
      />,
    );
    const card = screen.getByRole("region", { name: "Run failed" });
    expect(card).toHaveTextContent("Lost connection to the travel tools (McpTransportError). The run stopped.");
    expect(card).toHaveTextContent("Nothing was booked or charged. The 4 audit entries below record what ran.");
    await userEvent.click(screen.getByRole("button", { name: "Retry request" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

function clarification(data: Record<string, unknown>, actor = "supervisor"): AuditEntry {
  return { ...approval, actor, event: "needs_clarification", data };
}

describe("clarificationLine", () => {
  it.each([
    [{ reason: "unsupported intent" }, "only books train trips between Zurich HB, Bern, Geneva and Basel"],
    [{ reason: "unknown or same station" }, "Pick two different stations"],
    [{ reason: "invalid extraction", errors: [{ loc: ["trip", "passengers"], type: "less_than_equal" }] }, "1 to 9 passengers"],
    [{ reason: "invalid extraction", errors: [{ loc: ["trip"], type: "value_error" }] }, "return must be on or after"],
    [{ reason: "no direct train", leg: "outbound" }, "No direct train on that route and date"],
    [{ reason: "timetable unavailable", leg: "outbound" }, "timetable could not be reached"],
  ])("words %o", (data, expected) => {
    expect(clarificationLine(clarification(data))).toContain(expected);
  });

  it("falls back to the generic line for unknown reasons", () => {
    expect(clarificationLine(clarification({ reason: "something new" }))).toBeNull();
    render(
      <OutcomeCard
        run={{ ...run, status: "needs_clarification", payment: null }}
        clarification={clarification({ reason: "unsupported intent" })}
      />,
    );
    expect(screen.getByRole("region", { name: "Can't book that trip" })).toHaveTextContent(
      "This console only books train trips",
    );
  });
});

describe("answered runs", () => {
  it("lists every station and direct link instead of asking for detail", () => {
    render(<OutcomeCard run={{ ...run, status: "answered", payment: null }} />);
    const card = screen.getByRole("region", { name: "Routes you can book" });
    expect(card).toHaveTextContent("Bern: the capital, in the centre.");
    expect(card).toHaveTextContent("Zurich HB ⇄ Geneva · typical journey about 2 h 45");
    expect(screen.getAllByTestId("outcome-line")).toHaveLength(11);
  });
});

describe("OutcomeCard settlement", () => {
  const entry = (event: string, data: Record<string, unknown> = {}): AuditEntry => ({
    seq: 30,
    ts: "2026-11-21T21:34:05.000000+00:00",
    run_id: "r1",
    actor: "processor",
    event,
    data: { payment_id: "pi_test_abc", amount_cents: 5600, currency: "CHF", ...data },
    prev_hash: "2",
    hash: "3",
  });

  it("shows nothing for a simulated payment (no processor)", () => {
    render(<OutcomeCard run={run} approval={approval} />);
    expect(screen.queryByTestId("settlement")).toBeNull();
  });

  it.each([
    ["confirmed", "payment_confirmed", "Confirmed by processor 21:34 UTC"],
    ["refunded", "payment_refunded", "Refunded CHF 56.00 · 21:34 UTC"],
    ["disputed", "dispute_opened", "Dispute opened · 21:34 UTC"],
    ["failed", "payment_failed", "Payment failed at processor · 21:34 UTC"],
  ] as const)("says %s next to the charge, with the processor's time", (settlement, event, text) => {
    render(<OutcomeCard run={{ ...run, settlement }} approval={approval} settlementEntry={entry(event)} />);
    const mark = screen.getByTestId("settlement");
    expect(mark).toHaveAttribute("data-settlement", settlement);
    expect(mark).toHaveTextContent(text);
    // Still the booking card: the charge stays on screen next to what happened to it.
    expect(screen.getByTestId("payment-amount")).toHaveTextContent("CHF 56.00");
  });

  it("waits for the processor without inventing a time", () => {
    render(<OutcomeCard run={{ ...run, settlement: "pending" }} approval={approval} />);
    expect(screen.getByTestId("settlement")).toHaveTextContent("Awaiting processor confirmation");
  });

  it("names a refund without its amount when the entry came from reconciliation", () => {
    render(
      <OutcomeCard
        run={{ ...run, settlement: "refunded" }}
        approval={approval}
        settlementEntry={entry("settlement_reconciled", { amount_cents: undefined })}
      />,
    );
    expect(screen.getByTestId("settlement")).toHaveTextContent(/^Refunded · 21:34 UTC$/);
  });
});

describe("OutcomeCard processor payments", () => {
  it("says the payment went through the processor and links to its test dashboard", () => {
    const paid = { ...run, payment: { ...run.payment!, payment_id: "pi_3UeLuCLVvog9Cpxr1YsaC9ma" } };
    render(<OutcomeCard run={paid} />);
    expect(screen.getByTestId("processor-paid")).toHaveTextContent("Paid through Stripe in test mode");
    expect(screen.getByText("Stripe PaymentIntent")).toBeInTheDocument();
    const link = screen.getByTestId("processor-link");
    expect(link).toHaveAttribute("href", "https://dashboard.stripe.com/test/payments/pi_3UeLuCLVvog9Cpxr1YsaC9ma");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("labels simulated payments as such, without a processor link", () => {
    render(<OutcomeCard run={run} />);
    expect(screen.getByTestId("simulated-paid")).toHaveTextContent("Simulated payment");
    expect(screen.queryByTestId("processor-link")).not.toBeInTheDocument();
  });
});

describe("OutcomeCard processor check", () => {
  it("offers a check with the processor while the capture is pending", async () => {
    const onCheck = vi.fn();
    const paid = {
      ...run,
      settlement: "pending" as const,
      payment: { ...run.payment!, payment_id: "pi_3UeLuCLVvog9Cpxr1YsaC9ma" },
    };
    render(<OutcomeCard run={paid} onCheckProcessor={onCheck} />);
    await userEvent.click(screen.getByRole("button", { name: "Check with Stripe" }));
    expect(onCheck).toHaveBeenCalledOnce();
  });
});

describe("OutcomeCard receipts and hosted Checkout endings", () => {
  const paid: RunView = {
    ...run,
    payment: {
      payment_id: "pi_3TestReceipt",
      status: "captured",
      amount_cents: 5600,
      currency: "CHF",
      receipt_url: "https://pay.stripe.com/receipts/payment/abc",
    },
    settlement: "confirmed",
  };

  it("links the processor's public receipt next to the dashboard", () => {
    render(<OutcomeCard run={paid} />);
    const receipt = screen.getByTestId("receipt-link");
    expect(receipt).toHaveTextContent("View Stripe receipt ↗");
    expect(receipt).toHaveAttribute("href", "https://pay.stripe.com/receipts/payment/abc");
    expect(receipt).toHaveAttribute("target", "_blank");
    expect(receipt).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByTestId("processor-link")).toBeInTheDocument();
  });

  it("shows no receipt link without a processor receipt", () => {
    render(<OutcomeCard run={{ ...paid, payment: { ...paid.payment!, receipt_url: "https://evil.example/r" } }} />);
    expect(screen.queryByTestId("receipt-link")).not.toBeInTheDocument();
  });

  it("says an expired payment page moved no money", () => {
    const ended: AuditEntry = { ...approval, seq: 30, actor: "booking", event: "checkout_ended", data: { reason: "checkout expired" } };
    render(<OutcomeCard run={{ ...run, status: "failed", payment: null }} checkoutEnd={ended} />);
    expect(screen.getByRole("heading", { name: "Payment not completed" })).toBeInTheDocument();
    expect(screen.getAllByTestId("outcome-line")[0]).toHaveTextContent("payment page expired before anyone paid");
  });

  it("explains a refused amount mismatch", () => {
    render(<OutcomeCard run={{ ...run, status: "refused", payment: null, flags: ["amount_mismatch"] }} />);
    expect(screen.getByRole("heading", { name: "Payment refused" })).toBeInTheDocument();
    expect(screen.getAllByTestId("outcome-line")[0]).toHaveTextContent("amount_mismatch");
  });
});

describe("OutcomeCard payment models", () => {
  const processorPaid = {
    ...run,
    payment: { ...run.payment!, payment_id: "pi_3WalletPaid", receipt_url: "https://pay.stripe.com/receipts/payment/w1" },
  };

  it("says the agent paid from the wallet, which card and under which limit", () => {
    render(
      <OutcomeCard
        run={{
          ...processorPaid,
          payment_model: "wallet",
          wallet_payment: { brand: "visa", last4: "4242", mandate_cents: 20000 },
        }}
      />,
    );
    const paid = screen.getByTestId("processor-paid");
    expect(paid).toHaveAttribute("data-model", "wallet");
    expect(paid).toHaveTextContent("Paid by the agent from your wallet (Visa ····4242) under your CHF 200 limit.");
    expect(paid).toHaveTextContent("single-use token");
    expect(screen.getByTestId("receipt-link")).toHaveTextContent("View Stripe receipt ↗");
    expect(screen.getByTestId("processor-link")).toHaveTextContent("Open in Stripe dashboard ↗");
  });

  it("says the customer paid on the processor's page", () => {
    render(<OutcomeCard run={{ ...processorPaid, payment_model: "checkout" }} />);
    expect(screen.getByTestId("processor-paid")).toHaveTextContent("Paid by you on Stripe's page.");
    expect(screen.getByTestId("processor-paid")).not.toHaveTextContent("wallet");
  });

  it("says why the wallet was not used when the run fell back", () => {
    render(
      <OutcomeCard
        run={{ ...processorPaid, payment_model: "checkout", payment_fallback: "authentication_required" }}
      />,
    );
    expect(screen.getByTestId("processor-paid")).toHaveTextContent(
      "Paid by you on Stripe's page. The agent tried your wallet first; your bank asked for authentication.",
    );
  });
});
