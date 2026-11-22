import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PaymentCard } from "./PaymentCard";
import type { Checkout } from "../types";

const checkout: Checkout = {
  session_id: "cs_test_abc",
  checkout_url: "https://checkout.stripe.com/c/pay/cs_test_abc",
  expires_at: "2026-11-21T16:35:00+00:00",
  amount_cents: 5600,
  currency: "CHF",
};

describe("PaymentCard", () => {
  it("sends the customer to the processor's own page with the approved total", () => {
    render(<PaymentCard checkout={checkout} />);
    expect(screen.getByRole("heading", { name: "Pay on Stripe" })).toBeInTheDocument();
    const pay = screen.getByRole("link", { name: "Pay with Stripe" });
    expect(pay).toHaveAttribute("href", checkout.checkout_url);
    expect(screen.getByTestId("checkout-amount")).toHaveTextContent("CHF 56.00");
    expect(screen.getByTestId("test-card-hint")).toHaveTextContent("4242 4242 4242 4242, any future date, any CVC");
    expect(screen.getByTestId("checkout-expiry")).toHaveTextContent("16:35 UTC");
    expect(screen.queryByTestId("checkout-returned")).not.toBeInTheDocument();
  });

  it("checks with the processor or cancels on request", async () => {
    const onCheck = vi.fn();
    const onCancel = vi.fn();
    render(<PaymentCard checkout={checkout} onCheck={onCheck} onCancel={onCancel} returned="cancel" />);
    expect(screen.getByTestId("checkout-returned")).toHaveTextContent("left the payment page");
    await userEvent.click(screen.getByRole("button", { name: "Check with Stripe" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel payment" }));
    expect(onCheck).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("says the booking waits for the processor after the customer comes back", () => {
    render(<PaymentCard checkout={checkout} returned="success" busy />);
    expect(screen.getByTestId("checkout-returned")).toHaveTextContent("completes once Stripe confirms");
    expect(screen.getByTestId("pay-link")).toHaveAttribute("aria-disabled", "true");
  });
});

describe("PaymentCard after a wallet fallback", () => {
  it("says the bank asked for authentication", () => {
    render(<PaymentCard checkout={checkout} fallback="authentication_required" />);
    expect(screen.getByTestId("wallet-fallback")).toHaveTextContent(
      "Your bank asked for authentication: confirm on Stripe's page.",
    );
    expect(screen.getByTestId("wallet-fallback")).toHaveAttribute("role", "status");
  });

  it("says the total is above the wallet limit", () => {
    render(<PaymentCard checkout={checkout} fallback="over_mandate" limit="CHF 50" />);
    expect(screen.getByTestId("wallet-fallback")).toHaveTextContent("above your wallet limit of CHF 50");
  });

  it("says nothing when the customer chose the page", () => {
    render(<PaymentCard checkout={checkout} />);
    expect(screen.queryByTestId("wallet-fallback")).not.toBeInTheDocument();
  });
});
