import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { parseWalletReturn, walletNote, walletReturnNotice } from "./App";
import type { ApiClient } from "./api";
import { demoClient } from "./demo";
import { cleanCompleted } from "./fixtures/events";
import { eventsFromView } from "./runGraph";
import type { ApprovalRequest, PaymentOptions, RunEvent, RunView, WalletView } from "./types";

const VISA = { brand: "visa", last4: "4242", exp_month: 12, exp_year: 2034 };
const WALLET: WalletView = {
  user: "local-dev",
  enabled: true,
  card: VISA,
  mandate_cents: 20000,
  currency: "CHF",
  min_mandate_cents: 100,
  max_mandate_cents: 1_000_000,
};
const OPTIONS: PaymentOptions = {
  models: ["checkout", "wallet"],
  default: "wallet",
  payer: "local-dev",
  card: VISA,
  mandate_cents: 20000,
  wallet_unavailable: null,
};
const CHECKOUT = {
  session_id: "cs_test_abc",
  checkout_url: "https://checkout.stripe.com/c/pay/cs_test_abc",
  expires_at: "2026-11-21T16:35:00+00:00",
  amount_cents: 5600,
  currency: "CHF",
};

const both = async () => ({
  timetable: "simulated" as const,
  payments: "stripe_test" as const,
  payments_flow: "both" as const,
  payment_models: ["checkout" as const, "wallet" as const],
});

function play(view: RunView, onEvent: (e: RunEvent) => void, previous: RunView | null = null): RunView {
  for (const e of eventsFromView(view, previous)) onEvent(e);
  return view;
}

/** The demo client, its paused run carrying payment options, and the wallet API. */
function walletClient(over: Partial<ApiClient> = {}, options: PaymentOptions = OPTIONS): ApiClient {
  const base = demoClient({ scenario: "clean" });
  return {
    ...base,
    getMode: both,
    streamRun: async (message, onEvent) => {
      const add = (v: RunView) => (v.status === "awaiting_approval" ? { ...v, payment_options: options } : v);
      const view = await base.streamRun(message, (e) => onEvent(e.type === "run_view" ? { ...e, ...add(e) } : e));
      return add(view);
    },
    getWallet: vi.fn(async () => WALLET),
    startWalletSetup: vi.fn(async () => ({ setup_url: "https://checkout.stripe.com/c/setup/cs_test_s1" })),
    completeWalletSetup: vi.fn(async () => ({ ...WALLET, setup_state: "saved" })),
    removeWalletCard: vi.fn(async () => ({ ...WALLET, card: null })),
    setWalletMandate: vi.fn(async (cents: number) => ({ ...WALLET, mandate_cents: cents })),
    ...over,
  };
}

const walletPaid: RunView = {
  ...cleanCompleted,
  payment: { payment_id: "pi_3WalletPaid", status: "captured", amount_cents: 5600, currency: "CHF", receipt_url: null },
  settlement: "pending",
  payment_model: "wallet",
  wallet_payment: { brand: "visa", last4: "4242", mandate_cents: 20000 },
  trajectory: [...cleanCompleted.trajectory.filter((s) => s !== "payments.charge"), "wallet.issue_payment_token", "payments.charge"],
};

async function roundTrip() {
  const map = screen.getByRole("group", { name: /^Rail network map/ });
  await userEvent.click(within(map).getByRole("button", { name: "Zurich HB" }));
  await userEvent.click(within(map).getByRole("button", { name: "Bern" }));
  await userEvent.click(screen.getByRole("button", { name: "Find trains" }));
}

afterEach(() => window.history.replaceState(null, "", "/"));

describe("the wallet in the console", () => {
  it("draws the wallet as its own institution and offers its panel", async () => {
    render(<App client={walletClient()} paceMs={0} />);
    const boundary = await screen.findByTestId("stage-wallet-boundary");
    expect(boundary).toHaveTextContent("Walletseparate institution");
    // The note follows GET /wallet, which may answer after GET /mode.
    expect(await screen.findByTestId("stage-wallet-note")).toHaveTextContent("Visa ····4242 · limit CHF 200");
    expect(screen.getByTestId("stage-payment-step")).toHaveTextContent("Stripe page or wallet token");
    expect(within(screen.getByTestId("agent-stage")).getByRole("button", { name: /^Wallet:/ })).toBeInTheDocument();
    const panel = await screen.findByRole("region", { name: "Payment method" });
    expect(within(panel).getByTestId("wallet-card")).toHaveTextContent("Visa ····4242");
    expect(screen.getByRole("button", { name: "Payment method" })).toBeInTheDocument();
  });

  it("approves with the wallet and says the agent paid from it", async () => {
    const streamApproval = vi.fn(
      async (_id: string, _body: ApprovalRequest, onEvent: (e: RunEvent) => void, previous?: RunView | null) =>
        play(walletPaid, onEvent, previous ?? null),
    );
    render(<App client={walletClient({ streamApproval })} paceMs={0} />);
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    expect(within(card).getByRole("radio", { name: /Agent pays from my wallet \(Visa ····4242, limit CHF 200\)/ })).toBeChecked();
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));

    expect(streamApproval.mock.calls[0][1].payment_model).toBe("wallet");
    const booked = await screen.findByRole("region", { name: "Booked" });
    expect(within(booked).getByTestId("processor-paid")).toHaveTextContent(
      "Paid by the agent from your wallet (Visa ····4242) under your CHF 200 limit.",
    );
    const stage = screen.getByTestId("agent-stage");
    expect(stage).toHaveAttribute("data-model", "wallet");
    expect(stage.querySelector('[data-edge="booking-wallet"]')).toHaveAttribute("data-state", "done");
    expect(stage.querySelector('[data-edge="wallet-payments"]')).toHaveAttribute("data-state", "done");
    expect(screen.getByTestId("stage-payment-step")).toHaveTextContent("Agent paid with a wallet token");
  });

  it("sends checkout when the approver picks Stripe's page", async () => {
    const streamApproval = vi.fn(async () => ({ ...cleanCompleted, status: "awaiting_payment" as const, payment: null, checkout: CHECKOUT }));
    render(<App client={walletClient({ streamApproval })} paceMs={0} />);
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    await userEvent.click(within(card).getByRole("radio", { name: /Pay on Stripe now/ }));
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));
    expect((streamApproval.mock.calls[0] as unknown as [string, ApprovalRequest])[1].payment_model).toBe("checkout");
  });

  it("explains a fallback to the processor's page", async () => {
    const fallback: RunView = {
      ...cleanCompleted,
      status: "awaiting_payment",
      payment: null,
      settlement: null,
      checkout: CHECKOUT,
      payment_model: "checkout",
      payment_fallback: "authentication_required",
      trajectory: cleanCompleted.trajectory.filter((s) => s !== "payments.charge"),
    };
    const streamApproval = vi.fn(
      async (_id: string, _body: ApprovalRequest, onEvent: (e: RunEvent) => void, previous?: RunView | null) =>
        play(fallback, onEvent, previous ?? null),
    );
    render(<App client={walletClient({ streamApproval })} paceMs={0} />);
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));
    const payment = await screen.findByRole("region", { name: "Payment" });
    expect(within(payment).getByTestId("wallet-fallback")).toHaveTextContent(
      "Your bank asked for authentication: confirm on Stripe's page.",
    );
  });

  it("sends the browser to the processor's card form and reads it back on return", async () => {
    const navigate = vi.fn();
    const client = walletClient({ getWallet: vi.fn(async () => ({ ...WALLET, card: null })) });
    const { unmount } = render(<App client={client} paceMs={0} navigate={navigate} />);
    const panel = await screen.findByRole("region", { name: "Payment method" });
    await userEvent.click(within(panel).getByRole("button", { name: "Save a card" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("https://checkout.stripe.com/c/setup/cs_test_s1"));
    unmount();

    window.history.replaceState(null, "", "/?wallet=saved");
    render(<App client={client} paceMs={0} navigate={navigate} />);
    await waitFor(() => expect(client.completeWalletSetup).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("wallet-notice")).toHaveTextContent("Card saved: Visa ····4242.");
    expect(window.location.search).toBe("");
  });

  it("removes the card and sets the limit", async () => {
    const client = walletClient();
    render(<App client={client} paceMs={0} />);
    const panel = await screen.findByRole("region", { name: "Payment method" });
    const input = within(panel).getByRole("textbox", { name: /Limit per booking/ });
    await userEvent.clear(input);
    await userEvent.type(input, "120");
    await userEvent.click(within(panel).getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(client.setWalletMandate).toHaveBeenCalledWith(12000));
    expect(await within(panel).findByTestId("wallet-notice")).toHaveTextContent("Limit set to CHF 120 per booking.");
    await userEvent.click(within(panel).getByRole("button", { name: "Remove card" }));
    await waitFor(() => expect(within(panel).getByTestId("wallet-empty")).toBeInTheDocument());
  });

  it("parses the return and words its outcome", () => {
    expect(parseWalletReturn("?wallet=saved")).toBe("saved");
    expect(parseWalletReturn("?wallet=canceled")).toBe("canceled");
    expect(parseWalletReturn("?wallet=<script>")).toBeNull();
    expect(walletReturnNotice("canceled", null)).toContain("without saving a card");
    expect(walletReturnNotice("saved", { ...WALLET, setup_state: "pending" })).toContain("not confirmed");
    expect(walletNote({ ...WALLET, card: null })).toBe("no card saved · limit CHF 200");
  });
});
