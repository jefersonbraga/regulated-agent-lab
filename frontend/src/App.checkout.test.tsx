import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { parseCheckoutReturn } from "./App";
import { ApiError, type ApiClient } from "./api";
import { demoClient } from "./demo";
import { cleanAwaiting, cleanCompleted } from "./fixtures/events";
import { SOURCE_URL } from "./links";
import { eventsFromView } from "./runGraph";
import type { Me, RunEvent, RunSummary, RunView } from "./types";

const HEX_RUN = "0123456789abcdef0123456789abcdef";
const RECEIPT = "https://pay.stripe.com/receipts/payment/test_1";
const CHECKOUT = {
  session_id: "cs_test_abc",
  checkout_url: "https://checkout.stripe.com/c/pay/cs_test_abc",
  expires_at: "2026-11-21T16:35:00+00:00",
  amount_cents: 5600,
  currency: "CHF",
};

function awaitingPayment(base: RunView = cleanCompleted): RunView {
  return {
    ...base,
    status: "awaiting_payment",
    payment: null,
    settlement: null,
    checkout: CHECKOUT,
    trajectory: base.trajectory.filter((s) => s !== "payments.charge"),
  };
}

function paid(base: RunView = cleanCompleted): RunView {
  return {
    ...base,
    status: "completed",
    checkout: null,
    settlement: "confirmed",
    payment: { payment_id: "pi_3TestPaid", status: "captured", amount_cents: 5600, currency: "CHF", receipt_url: RECEIPT },
  };
}

/** What the server streams for a resumed run: the steps after `previous`, then its view. */
function play(view: RunView, onEvent: (e: RunEvent) => void, previous: RunView | null = null): RunView {
  for (const e of eventsFromView(view, previous)) onEvent(e);
  return view;
}

async function roundTrip() {
  const map = screen.getByRole("group", { name: /^Rail network map/ });
  await userEvent.click(within(map).getByRole("button", { name: "Zurich HB" }));
  await userEvent.click(within(map).getByRole("button", { name: "Bern" }));
  await userEvent.click(screen.getByRole("button", { name: "Find trains" }));
}

afterEach(() => window.history.replaceState(null, "", "/"));

describe("hosted Checkout in the console", () => {
  it("says on the approval card that the customer pays on the processor's page", async () => {
    const client: ApiClient = {
      ...demoClient({ scenario: "clean" }),
      getMode: async () => ({ timetable: "simulated", payments: "stripe_test", payments_flow: "checkout" }),
    };
    render(<App client={client} paceMs={0} />);
    await waitFor(() => expect(screen.getByTestId("stage-payment-step")).toHaveTextContent("Customer pays on Stripe"));
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    expect(within(card).getByTestId("charge-notice")).toHaveTextContent(
      "On approval: you will confirm the payment on Stripe's page (test mode, use card 4242 4242 4242 4242, any future date, any CVC)",
    );
  });

  it("draws no wallet when only the hosted page is allowed", async () => {
    const client: ApiClient = {
      ...demoClient({ scenario: "clean" }),
      getMode: async () => ({
        timetable: "simulated",
        payments: "stripe_test",
        payments_flow: "checkout",
        payment_models: ["checkout"],
      }),
    };
    render(<App client={client} paceMs={0} />);
    await waitFor(() => expect(screen.getByTestId("stage-payment-step")).toHaveTextContent("Customer pays on Stripe"));
    expect(screen.queryByTestId("stage-wallet-boundary")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Payment method" })).not.toBeInTheDocument();
  });

  it("approves, waits on the payment page, then completes with the receipt", async () => {
    const base = demoClient({ scenario: "clean" });
    const refreshPayment = vi.fn(async () => paid());
    const client: ApiClient = {
      ...base,
      streamApproval: vi.fn(async (_id, _body, onEvent, previous) => play(awaitingPayment(), onEvent, previous)),
      refreshPayment,
      cancelPayment: vi.fn(),
    };
    render(<App client={client} paceMs={0} />);
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));

    const payment = await screen.findByRole("region", { name: "Payment" });
    expect(within(payment).getByRole("link", { name: "Pay with Stripe" })).toHaveAttribute("href", CHECKOUT.checkout_url);
    expect(screen.getByTestId("run-status")).toHaveTextContent("awaiting payment");
    expect(within(screen.getByTestId("agent-stage")).getByRole("button", { name: /^Payments:/ })).toHaveAccessibleName(
      "Payments: waiting for you",
    );
    expect(screen.queryByRole("region", { name: "Booked" })).not.toBeInTheDocument();

    await userEvent.click(within(payment).getByRole("button", { name: "Check with Stripe" }));
    expect(refreshPayment).toHaveBeenCalledWith(cleanCompleted.run_id);
    const booked = await screen.findByRole("region", { name: "Booked" });
    expect(within(booked).getByTestId("receipt-link")).toHaveAttribute("href", RECEIPT);
    expect(within(booked).getByTestId("receipt-link")).toHaveTextContent("View Stripe receipt ↗");
  });

  it("reopens the run when the processor sends the customer back, and asks the processor", async () => {
    window.history.pushState(null, "", `/?run=${HEX_RUN}&checkout=success`);
    const waiting = awaitingPayment({ ...cleanCompleted, run_id: HEX_RUN });
    const getRun = vi.fn(async () => waiting);
    const refreshPayment = vi.fn(async () => paid({ ...cleanCompleted, run_id: HEX_RUN }));
    const client: ApiClient = { ...demoClient({ scenario: "clean" }), getRun, refreshPayment };
    render(<App client={client} paceMs={0} />);

    const booked = await screen.findByRole("region", { name: "Booked" });
    expect(getRun).toHaveBeenCalledWith(HEX_RUN);
    expect(refreshPayment).toHaveBeenCalledWith(HEX_RUN);
    expect(within(booked).getByTestId("receipt-link")).toBeInTheDocument();
    // The return query is consumed, so a reload does not repeat it.
    expect(window.location.search).toBe("");
  });

  it("after a cancelled checkout offers to pay again or cancel", async () => {
    window.history.pushState(null, "", `/?run=${HEX_RUN}&checkout=cancel`);
    const waiting = awaitingPayment({ ...cleanCompleted, run_id: HEX_RUN });
    const refreshPayment = vi.fn();
    const cancelPayment = vi.fn(async () => ({ ...waiting, status: "failed" as const, checkout: null }));
    const client: ApiClient = {
      ...demoClient({ scenario: "clean" }),
      getRun: vi.fn(async () => waiting),
      refreshPayment,
      cancelPayment,
    };
    render(<App client={client} paceMs={0} />);
    const payment = await screen.findByRole("region", { name: "Payment" });
    expect(within(payment).getByTestId("checkout-returned")).toHaveTextContent("left the payment page");
    expect(refreshPayment).not.toHaveBeenCalled();
    await userEvent.click(within(payment).getByRole("button", { name: "Cancel payment" }));
    expect(cancelPayment).toHaveBeenCalledWith(HEX_RUN);
    await screen.findByRole("region", { name: "Run failed" });
  });

  it("parses only a well-formed return query", () => {
    expect(parseCheckoutReturn(`?run=${HEX_RUN}&checkout=success`)).toEqual({ runId: HEX_RUN, checkout: "success" });
    expect(parseCheckoutReturn(`?run=${HEX_RUN}&checkout=maybe`)).toBeNull();
    expect(parseCheckoutReturn("?run=../x&checkout=success")).toBeNull();
    expect(parseCheckoutReturn("")).toBeNull();
  });
});

describe("sign-in", () => {
  const me: Me = { name: "alice", role: "both", auth_enabled: true, require_separate_approver: false };

  function sessionClient(overrides: Partial<ApiClient> = {}): ApiClient {
    return {
      ...demoClient({ scenario: "clean" }),
      getMe: vi.fn().mockRejectedValue(new ApiError(401, "401 not signed in")),
      login: vi.fn().mockResolvedValue(me),
      logout: vi.fn().mockResolvedValue(undefined),
      ...overrides,
    };
  }

  it("shows the sign-in screen until the API has a session, then the console", async () => {
    const client = sessionClient();
    render(<App client={client} paceMs={0} />);
    const form = await screen.findByRole("region", { name: "Sign in" });
    expect(screen.queryByTestId("agent-stage")).not.toBeInTheDocument();
    await userEvent.type(within(form).getByLabelText(/^Name/), "alice");
    await userEvent.type(within(form).getByLabelText(/^Password/), "correct horse");
    await userEvent.click(within(form).getByRole("button", { name: "Sign in" }));
    expect(client.login).toHaveBeenCalledWith("alice", "correct horse");
    expect(await screen.findByTestId("nav-user")).toHaveTextContent("Signed in as alice");
    expect(screen.getByTestId("agent-stage")).toBeInTheDocument();
  });

  it("says why a sign-in failed and never keeps the password", async () => {
    const client = sessionClient({ login: vi.fn().mockRejectedValue(new ApiError(401, "401 invalid")) });
    render(<App client={client} paceMs={0} />);
    const form = await screen.findByRole("region", { name: "Sign in" });
    await userEvent.type(within(form).getByLabelText(/^Name/), "alice");
    await userEvent.type(within(form).getByLabelText(/^Password/), "wrong");
    await userEvent.click(within(form).getByRole("button", { name: "Sign in" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Wrong name or password.");
    expect(within(form).getByLabelText(/^Password/)).toHaveValue("");
  });

  it("opens straight into the console for a valid session and signs out", async () => {
    const client = sessionClient({ getMe: vi.fn().mockResolvedValue(me) });
    render(<App client={client} paceMs={0} />);
    expect(await screen.findByTestId("nav-user")).toHaveTextContent("alice");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(client.logout).toHaveBeenCalled();
    expect(await screen.findByRole("region", { name: "Sign in" })).toBeInTheDocument();
  });

  it("goes back to sign-in when a call finds the session ended", async () => {
    const client = sessionClient({
      getMe: vi.fn().mockResolvedValue(me),
      streamRun: vi.fn().mockRejectedValue(new ApiError(401, "401 not signed in")),
    });
    render(<App client={client} paceMs={0} />);
    await screen.findByTestId("nav-user");
    await roundTrip();
    expect(await screen.findByRole("region", { name: "Sign in" })).toBeInTheDocument();
  });

  it("follows a quote another person must decide, and shows their decision when it lands", async () => {
    const requester: Me = { ...me, require_separate_approver: true };
    let decided = false;
    const getRun = vi.fn(async () => (decided ? cleanCompleted : cleanAwaiting));
    const client = sessionClient({ getMe: vi.fn().mockResolvedValue(requester), getRun });
    render(<App client={client} paceMs={0} decisionPollMs={20} />);
    await screen.findByTestId("nav-user");
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    expect(within(card).getByTestId("approver-note")).toHaveTextContent("You requested this trip");
    // Still waiting: polling keeps the card. Then the approver decides elsewhere.
    await waitFor(() => expect(getRun).toHaveBeenCalled());
    expect(screen.getByRole("region", { name: "Approval required" })).toBeInTheDocument();
    decided = true;
    await waitFor(() => expect(screen.getByTestId("run-status")).toHaveTextContent("completed"));
    expect(getRun).toHaveBeenCalledWith(cleanCompleted.run_id);
    expect(screen.queryByRole("region", { name: "Approval required" })).not.toBeInTheDocument();
  });

  it("tells an approver that a quote waits for them, in the tab title and a corner notice", async () => {
    const approver: Me = { name: "bob", role: "approver", auth_enabled: true, require_separate_approver: true };
    const pending: RunSummary = {
      run_id: cleanAwaiting.run_id,
      created_at: "2026-11-23T15:00:00Z",
      updated_at: "2026-11-23T15:00:00Z",
      request_redacted: "Train from Zurich HB to Bern",
      status: "awaiting_approval",
      total_cents: 8400,
      currency: "CHF",
      payment_id: null,
      flags: [],
      settlement: null,
      requester: "alice",
    };
    const getRun = vi.fn(async () => cleanAwaiting);
    const client = sessionClient({
      getMe: vi.fn().mockResolvedValue(approver),
      listRuns: vi.fn().mockResolvedValue([pending]),
      getRun,
    });
    render(<App client={client} paceMs={0} />);
    const notice = await screen.findByTestId("approval-notice");
    expect(notice).toHaveTextContent("alice requested a trip");
    expect(notice).toHaveTextContent("CHF 84.00");
    await waitFor(() => expect(document.title).toMatch(/^\(1\) Approval needed/));
    // The notice opens nothing by itself: the approver chooses to review.
    expect(screen.queryByRole("region", { name: "Approval required" })).not.toBeInTheDocument();
    await userEvent.click(within(notice).getByRole("button", { name: "Review" }));
    expect(getRun).toHaveBeenCalledWith(cleanAwaiting.run_id);
    expect(await screen.findByRole("region", { name: "Approval required" })).toBeInTheDocument();
    expect(screen.queryByTestId("approval-notice")).not.toBeInTheDocument();
  });

  it("lets an approver put the notice away, and does not show it again for the same trip", async () => {
    const approver: Me = { name: "bob", role: "approver", auth_enabled: true, require_separate_approver: true };
    const pending: RunSummary = {
      run_id: cleanAwaiting.run_id,
      created_at: "2026-11-23T15:00:00Z",
      updated_at: "2026-11-23T15:00:00Z",
      request_redacted: "Train from Zurich HB to Bern",
      status: "awaiting_approval",
      total_cents: 8400,
      currency: "CHF",
      payment_id: null,
      flags: [],
      settlement: null,
      requester: "alice",
    };
    const listRuns = vi.fn().mockResolvedValue([pending]);
    const client = sessionClient({
      getMe: vi.fn().mockResolvedValue(approver),
      listRuns,
      getRun: vi.fn(async () => cleanAwaiting),
    });
    render(<App client={client} paceMs={0} />);
    const notice = await screen.findByTestId("approval-notice");
    // An approver-only account cannot request trips: the composer says so instead of a 403.
    expect(screen.getByText(/approves trips and cannot request them/)).toBeInTheDocument();
    await userEvent.click(within(notice).getByRole("button", { name: "Later" }));
    expect(screen.queryByTestId("approval-notice")).not.toBeInTheDocument();
    // The trip still waits in the Human gate.
    expect(screen.getByTestId("gate-waiting")).toHaveTextContent("alice");
  });

  it("links the source code before sign-in and in the console header", async () => {
    const client = sessionClient();
    render(<App client={client} paceMs={0} />);
    await screen.findByRole("region", { name: "Sign in" });
    const before = screen.getByRole("link", { name: /source code/i });
    expect(before).toHaveAttribute("href", SOURCE_URL);
    expect(before).toHaveAttribute("rel", expect.stringContaining("noopener"));

    const form = screen.getByRole("region", { name: "Sign in" });
    await userEvent.type(within(form).getByLabelText(/^Name/), "alice");
    await userEvent.type(within(form).getByLabelText(/^Password/), "secret");
    await userEvent.click(within(form).getByRole("button", { name: "Sign in" }));
    await screen.findByTestId("nav-user");
    expect(screen.getByRole("link", { name: /source code/i })).toHaveAttribute("href", SOURCE_URL);
  });

  it("needs no sign-in for a recorded demo", async () => {
    render(<App client={demoClient({ scenario: "clean" })} paceMs={0} />);
    expect(screen.queryByRole("region", { name: "Sign in" })).not.toBeInTheDocument();
    expect(await screen.findByTestId("agent-stage")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("recent-runs")).toHaveTextContent("No runs yet."));
    expect(screen.queryByTestId("nav-user")).not.toBeInTheDocument();
  });
});
