import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App, { approverNote, awaitingFor, describeFailure, errorText, modeBadges, requestNote } from "./App";
import type { ApiClient } from "./api";
import { demoClient, type DemoOptions } from "./demo";
import { audit as fixtureAudit, cleanCompleted, QUOTE_HASH, QUOTE_HASHES, RECOMMENDED_FARE_IDS, RUN_ID } from "./fixtures/events";
import type { RunSummary } from "./types";

/** The recorded demo scenarios double as a deterministic backend; spies record each call. */
function setup(opts: DemoOptions, override: Partial<ApiClient> = {}) {
  const base = demoClient(opts);
  const client: ApiClient = {
    streamRun: vi.fn(base.streamRun),
    streamApproval: vi.fn(base.streamApproval),
    getQuote: vi.fn(base.getQuote),
    getAudit: vi.fn(base.getAudit),
    verifyAudit: vi.fn(base.verifyAudit),
    ...override,
  };
  render(<App client={client} paceMs={0} />);
  return client;
}

/** Plans Zurich HB -> Bern on the map (a return trip by default) and sends it. */
async function roundTrip() {
  const map = screen.getByRole("group", { name: /^Rail network map/ });
  await userEvent.click(within(map).getByRole("button", { name: "Zurich HB" }));
  await userEvent.click(within(map).getByRole("button", { name: "Bern" }));
  await userEvent.click(screen.getByRole("button", { name: "Find trains" }));
}
const composer = () => screen.getByLabelText("Message input");
/** Alerts with something to say: Astryx form fields keep empty live regions ready for entry errors. */
const pageAlerts = () => screen.queryAllByRole("alert").filter((a) => a.textContent?.trim());
const node = (name: RegExp) => within(screen.getByTestId("agent-stage")).getByRole("button", { name });

describe("approverNote", () => {
  const me = (name: string, role: "requester" | "approver" | "both", separate = true) => ({
    name,
    role,
    auth_enabled: true,
    require_separate_approver: separate,
  });

  it("asks for a second person when the viewer requested the run", () => {
    const note = approverNote(me("jeff", "both"), "jeff");
    expect(note).toMatch(/You requested this trip/);
    // The rule says why, not only what: the four-eyes control and what it protects against.
    expect(note).toMatch(/four-eyes rule/);
    expect(note).toMatch(/cannot also authorize the spend/);
  });

  it("asks for an approver when the viewer may only request", () => {
    expect(approverNote(me("recruiter", "requester"), "someone")).toMatch(/cannot approve/);
  });

  it("stays silent for an approver of someone else's run, or without the rule", () => {
    expect(approverNote(me("jeff-approver", "approver"), "jeff")).toBeNull();
    expect(approverNote(me("jeff", "both", false), "jeff")).toBeNull();
    expect(approverNote(null, "jeff")).toBeNull();
    expect(approverNote(me("jeff", "both"), null)).toBeNull();
  });
});

describe("awaitingFor", () => {
  const me = (name: string, role: "requester" | "approver" | "both") => ({
    name,
    role,
    auth_enabled: true,
    require_separate_approver: true,
  });
  const row = (run_id: string, status: RunSummary["status"], requester: string): RunSummary => ({
    run_id,
    created_at: "2026-11-23T15:00:00Z",
    updated_at: "2026-11-23T15:00:00Z",
    request_redacted: `trip ${run_id}`,
    status,
    total_cents: 8400,
    currency: "CHF",
    payment_id: null,
    flags: [],
    settlement: null,
    requester,
  });
  const runs = [
    row("a", "awaiting_approval", "jeff"),
    row("b", "completed", "jeff"),
    row("c", "awaiting_approval", "jeff-approver"),
    row("d", "awaiting_approval", "recruiter"),
  ];

  it("lists the quotes an approver may decide: waiting, and requested by someone else", () => {
    expect(awaitingFor(me("jeff-approver", "approver"), runs).map((r) => r.run_id)).toEqual(["a", "d"]);
  });

  it("lists nothing for a requester-only account or without a signed-in user", () => {
    expect(awaitingFor(me("recruiter", "requester"), runs)).toEqual([]);
    expect(awaitingFor(null, runs)).toEqual([]);
  });
});

describe("requestNote", () => {
  const me = (role: "requester" | "approver" | "both") => ({
    name: "x",
    role,
    auth_enabled: true,
    require_separate_approver: true,
  });

  it("locks planning for an approver-only account, and says why", () => {
    expect(requestNote(me("approver"))).toMatch(/approves trips and cannot request them/);
  });

  it("leaves planning open for accounts that may request, and without sign-in", () => {
    expect(requestNote(me("requester"))).toBeNull();
    expect(requestNote(me("both"))).toBeNull();
    expect(requestNote(null)).toBeNull();
  });
});

describe("App", () => {
  it("starts idle, with the design rule on screen", () => {
    setup({ scenario: "clean" });
    expect(screen.getByRole("heading", { name: "Agent console" })).toBeInTheDocument();
    expect(screen.getByTestId("agent-stage")).toHaveAttribute("data-empty", "true");
    expect(screen.getByTestId("run-status")).toHaveTextContent("No run yet");
    expect(screen.getByText("Nothing to approve.")).toBeInTheDocument();
    const rules = screen.getByRole("list", { name: "LLM proposes, code disposes" });
    expect(rules).toHaveTextContent("LLM proposes");
    expect(rules).toHaveTextContent("Code disposes");
    expect(rules).toHaveTextContent("Human approves");
    expect(screen.queryByTestId("verify-badge")).not.toBeInTheDocument();
    expect(screen.getByTestId("audit-empty")).toHaveTextContent("Blocks appear here once a run starts.");
  });

  it("runs a flagged trip through review, approval and payment", async () => {
    const client = setup({ scenario: "flagged" });
    await roundTrip();
    expect(client.streamRun).toHaveBeenCalledWith(expect.stringContaining("from Zurich HB to Bern"), expect.any(Function));

    const card = await screen.findByRole("region", { name: "Approval required" });
    // The pending decision holds focus; the composer stays locked until a decision.
    await waitFor(() => expect(within(card).getByRole("heading", { name: "Approve payment?" })).toHaveFocus());
    expect(composer()).toHaveAttribute("aria-disabled", "true");
    expect(document.querySelector(".console")).toHaveAttribute("data-gate", "open");
    expect(screen.getByTestId("run-status")).toHaveTextContent("awaiting approval");
    expect(node(/^Approval:/)).toHaveAccessibleName("Approval: waiting for you");
    expect(node(/^Planner:/)).toHaveAccessibleName("Planner: flagged");
    expect(within(card).getByTestId("quote-total")).toHaveTextContent("CHF 56.00");
    // One injection banner: on the card while it waits, not repeated on the page.
    expect(screen.getAllByTestId("run-flags")).toHaveLength(1);
    expect(within(card).getByTestId("run-flags")).toHaveTextContent("Possible prompt injection · 2 tool results flagged");
    expect(screen.getByTestId("signal-gate")).toHaveTextContent("waiting");
    expect(screen.getByTestId("signal-guard")).toHaveTextContent("2 caught");
    await waitFor(() => expect(screen.getByTestId("verify-badge")).toHaveTextContent("Whole chain verified · 43 entries"));
    // The approver sees what was asked, and the flagged results with a way to their blocks.
    expect(within(card).getByTestId("approval-request")).toHaveTextContent("from Zurich HB to Bern");
    expect(screen.getByTestId("run-request")).toHaveTextContent("from Zurich HB to Bern");
    await waitFor(() => expect(within(card).getByTestId("flag-evidence")).toHaveTextContent("#3"));
    expect(within(card).getByTestId("flag-evidence")).toHaveTextContent("#6");
    expect(screen.getByTestId("composer-locked")).toHaveTextContent("Approve or reject the quote first");
    expect(client.getAudit).toHaveBeenCalledWith(RUN_ID);
    // Narrow screens get a pinned reminder (hidden by CSS from 1100px).
    expect(screen.getByRole("region", { name: "Pending approval" })).toHaveTextContent("Approval needed · CHF 56.00");

    await userEvent.click(within(card).getByRole("button", { name: "Show flagged results in chain (#3, #6)" }));
    expect(screen.getByTestId("block-detail")).toHaveTextContent("#3 · planner · injection_flagged");
    await userEvent.click(within(card).getByRole("checkbox", { name: /I checked the 2 flagged tool results/ }));
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));
    const booked = await screen.findByRole("region", { name: "Booked" });
    expect(client.streamApproval).toHaveBeenCalledWith(
      RUN_ID,
      { approved: true, approver: "demo-approver", quote_hash: QUOTE_HASH, fare_ids: RECOMMENDED_FARE_IDS },
      expect.any(Function),
      expect.objectContaining({ status: "awaiting_approval" }),
    );
    await waitFor(() => expect(within(booked).getByRole("heading", { name: "Booked" })).toHaveFocus());
    expect(within(booked).getByTestId("payment-id")).toHaveTextContent("PAY-4e97dfb3afd6");
    expect(screen.getByTestId("run-status")).toHaveTextContent("completed");
    expect(node(/^Approval:/)).toHaveAccessibleName("Approval: approved");
    expect(node(/^Payments:/)).toHaveAccessibleName("Payments: done");
    // Once the card is gone the page keeps the flags in view, as a neutral record (not a warning).
    expect(screen.getByTestId("run-flags")).toHaveAttribute("data-tone", "note");
    expect(within(booked).getByTestId("payment-amount").parentElement).toHaveTextContent("Charged");
    expect(node(/^Supervisor:/)).toHaveAccessibleName("Supervisor: done");
    expect(screen.getByTestId("run-flags")).toHaveTextContent(
      "2 tool results flagged and neutralised · reviewed by a human before payment",
    );
    await waitFor(() => expect(screen.getByTestId("verify-badge")).toHaveTextContent("Whole chain verified · 50 entries"));
    expect(within(screen.getByRole("list", { name: "Audit blocks" })).getAllByRole("button")).toHaveLength(50);
    expect(screen.getByTestId("signal-gate")).toHaveTextContent("approved");
    expect(screen.queryByRole("region", { name: "Pending approval" })).not.toBeInTheDocument();
    expect(composer()).not.toHaveAttribute("aria-disabled");
  });

  it("books the train the approver picked, charging that selection's server quote", async () => {
    const client = setup({ scenario: "clean" });
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    // "in the morning" on the way out; no time asked for the way back.
    expect(within(card).getByText("Morning 06:00–12:00")).toBeInTheDocument();
    expect(within(card).getAllByText("No time given: showing trains from 07:00")).toHaveLength(1);
    const back = within(card).getByRole("radiogroup", { name: /^Return/ });
    await userEvent.click(within(back).getByRole("radio", { name: /^07:02 to 07:58/ }));
    const ids = [RECOMMENDED_FARE_IDS[0], "DR-BER-ZUE-20261202-0702-2nd"];
    expect(client.getQuote).toHaveBeenCalledWith(RUN_ID, ids);
    await waitFor(() => expect(within(card).getByTestId("quote-total")).toHaveTextContent("CHF 84.00"));
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));
    const booked = await screen.findByRole("region", { name: "Booked" });
    expect(client.streamApproval).toHaveBeenCalledWith(
      RUN_ID,
      { approved: true, approver: "demo-approver", quote_hash: QUOTE_HASHES[ids.join("|")], fare_ids: ids },
      expect.any(Function),
      expect.anything(),
    );
    expect(within(booked).getByTestId("payment-amount")).toHaveTextContent("CHF 84.00");
    expect(node(/^Booking:/)).toHaveAccessibleName("Booking: done");
  });

  it("warns about flagged tool output while the run is still streaming", async () => {
    setup({ scenario: "flagged", hold: 9 });
    await roundTrip();
    await waitFor(() => expect(screen.getByTestId("run-flags")).toHaveTextContent("2 tool results flagged"));
    expect(screen.getByTestId("run-status")).toHaveTextContent("running");
    expect(screen.getByText("Agents at work…")).toBeInTheDocument();
    expect(screen.getByTestId("audit-recording")).toBeInTheDocument();
    expect(composer()).toHaveAttribute("aria-disabled", "true");
  });

  it("says the run is still working when no event arrives for a while", async () => {
    // A slow model call keeps the stream quiet (the API only sends keep-alive comments).
    render(<App client={demoClient({ scenario: "clean", hold: 3 })} paceMs={0} quietNoticeMs={50} />);
    await roundTrip();
    const notice = await screen.findByTestId("run-quiet");
    expect(notice).toHaveTextContent("Still working");
    expect(notice).toHaveTextContent("nothing is booked or charged");
    expect(screen.getByTestId("run-status")).toHaveTextContent("running");
  });

  it("shows no still-working notice while events keep arriving", async () => {
    setup({ scenario: "clean" });
    await roundTrip();
    await screen.findByRole("region", { name: "Approval required" });
    expect(screen.queryByTestId("run-quiet")).not.toBeInTheDocument();
  });

  it("shows the live step while agents work", async () => {
    setup({ scenario: "clean", hold: 17 });
    await roundTrip();
    await waitFor(() => expect(node(/^Pricing:/)).toHaveAccessibleName("Pricing: running"));
    expect(screen.getByTestId("stage-live")).toHaveTextContent("Pricing · get_fares");
    expect(node(/^Planner:/)).toHaveAccessibleName("Planner: done");
    expect(node(/^Booking:/)).toHaveAccessibleName("Booking: idle");
    expect(screen.queryByTestId("run-flags")).not.toBeInTheDocument();
  });

  it("ends without a charge when the human rejects", async () => {
    const client = setup({ scenario: "clean" });
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Approval required" });
    // A clean run needs no review step.
    expect(within(card).queryByRole("checkbox")).not.toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Reject" }));
    const end = await screen.findByRole("region", { name: "Not approved" });
    expect(end).toHaveTextContent("No charge was made.");
    expect(client.streamApproval).toHaveBeenCalledWith(
      RUN_ID,
      expect.objectContaining({ approved: false }),
      expect.any(Function),
      expect.anything(),
    );
    expect(node(/^Approval:/)).toHaveAccessibleName("Approval: rejected");
    expect(node(/^Payments:/)).toHaveAccessibleName("Payments: idle");
    expect(screen.getByTestId("signal-gate")).toHaveTextContent("rejected");
  });

  it("shows a policy denial on the stage, in the outcome and in the chain", async () => {
    setup({ scenario: "denied" });
    await roundTrip();
    await screen.findByRole("region", { name: "Refused by policy" });
    expect(node(/^Booking:/)).toHaveAccessibleName("Booking: blocked by policy");
    // The outcome card explains the refusal; the page does not repeat it in a banner.
    expect(screen.queryByTestId("run-flags")).not.toBeInTheDocument();
    expect(screen.getByTestId("signal-gate")).toHaveTextContent("refused");
    await waitFor(() => expect(document.querySelector('.chain-block[data-tone="denied"]')).not.toBeNull());
    expect(screen.getByRole("region", { name: "Refused by policy" })).toHaveTextContent(
      "The booking agent tried payments.charge before approval. The gate refused it: tool requires human approval.",
    );
    expect(screen.getByRole("region", { name: "Refused by policy" })).toHaveTextContent("This is the gate working");
    expect(screen.getByRole("button", { name: "Show denial in chain" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send request again" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Approval required" })).not.toBeInTheDocument();
  });

  it("ends a run that fails mid-stream in its outcome card, in words, and keeps its audit trail", async () => {
    const client = setup({ scenario: "error" });
    await roundTrip();
    const card = await screen.findByRole("region", { name: "Run failed" });
    // The failure is explained where the run ends, not as a page-top alert.
    expect(pageAlerts()).toEqual([]);
    expect(card).toHaveTextContent(
      "Lost connection to the travel tools during travel_search.get_fares (McpTransportError). The run stopped.",
    );
    // The call that was in flight is marked on its node, and the supervisor reads stopped.
    expect(node(/^Pricing:/)).toHaveAccessibleName("Pricing: failed");
    expect(node(/^Supervisor:/)).toHaveAccessibleName("Supervisor: stopped");
    expect(screen.getByRole("list", { name: "Pricing tool calls" })).toHaveTextContent("get_fares · failed");
    expect(screen.getByTestId("signal-gate")).toHaveTextContent("stopped");
    await waitFor(() => expect(card).toHaveTextContent("Nothing was booked or charged. The 4 audit entries below"));
    expect(screen.getByTestId("run-status")).toHaveTextContent("failed");
    await waitFor(() => expect(client.getAudit).toHaveBeenCalledWith(RUN_ID));
    await waitFor(() => expect(screen.getByTestId("audit-scope")).toHaveTextContent("4 entries"));
    // The composer unlocks again once the failed request settles, and the request can be retried.
    expect(composer()).not.toHaveAttribute("aria-disabled");
    await userEvent.click(within(card).getByRole("button", { name: "Retry request" }));
    await waitFor(() => expect(client.streamRun).toHaveBeenCalledTimes(2));
  });

  it("shows an API error that stops a run before it starts as an alert", async () => {
    setup({ scenario: "clean" }, { streamRun: vi.fn().mockRejectedValue(new Error("HTTP 503")) });
    await roundTrip();
    await waitFor(() => expect(pageAlerts()).toHaveLength(1));
    const [alert] = pageAlerts();
    expect(alert).toHaveTextContent("API error");
    expect(alert).toHaveTextContent("HTTP 503");
    expect(alert).not.toHaveTextContent("Error: ");
  });

  it("shows a broken chain in red with the first bad seq", async () => {
    setup(
      { scenario: "clean" },
      { verifyAudit: vi.fn().mockResolvedValue({ ok: false, entries: 27, first_bad_seq: 12, reason: "hash mismatch" }) },
    );
    await roundTrip();
    await waitFor(() => expect(screen.getByTestId("verify-badge")).toHaveTextContent("Chain broken at seq 12"));
    expect(screen.getByTestId("verify-badge")).toHaveAttribute("data-variant", "error");
    expect(screen.getByTestId("signal-chain")).toHaveTextContent("broken at 12");
    expect(document.getElementById("audit-block-12")).toHaveAttribute("data-broken", "true");
  });

  it("lists recent runs and reopens one with its view and audit trail", async () => {
    const summary: RunSummary = {
      run_id: RUN_ID,
      created_at: "2026-11-21T13:47:12.000000+00:00",
      updated_at: "2026-11-21T13:48:02.000000+00:00",
      request_redacted: "Zurich HB to Bern, contact [REDACTED:email]",
      status: "completed",
      total_cents: cleanCompleted.payment!.amount_cents,
      currency: "CHF",
      payment_id: cleanCompleted.payment!.payment_id,
      flags: [],
      settlement: null,
    };
    const client = setup(
      { scenario: "clean" },
      {
        listRuns: vi.fn().mockResolvedValue([summary]),
        getRun: vi.fn().mockResolvedValue(cleanCompleted),
        getAudit: vi.fn().mockResolvedValue(fixtureAudit.clean.completed),
      },
    );
    const list = await screen.findByTestId("recent-runs");
    const row = await within(list).findByTestId("recent-run");
    expect(row).toHaveTextContent("13:47 UTC");
    expect(row).toHaveTextContent("[REDACTED:email]");
    expect(row).toHaveTextContent(`CHF ${Math.floor(summary.total_cents! / 100)}.`);
    expect(within(row).getByRole("img", { name: "completed" })).toBeInTheDocument();

    await userEvent.click(row);
    expect(client.getRun).toHaveBeenCalledWith(RUN_ID);
    await waitFor(() => expect(screen.getByTestId("run-status")).toHaveTextContent("completed"));
    expect(client.getAudit).toHaveBeenCalledWith(RUN_ID);
    expect(screen.getByTestId("run-request")).toHaveTextContent("[REDACTED:email]");
    expect(await within(list).findByTestId("recent-run")).toHaveAttribute("aria-current", "true");
  });

  it("shows no history for a client without GET /runs", () => {
    setup({ scenario: "clean" });
    expect(screen.queryByTestId("recent-runs")).not.toBeInTheDocument();
  });

  it("filters the audit chain to the selected agent", async () => {
    setup({ scenario: "clean" });
    await roundTrip();
    await waitFor(() => expect(screen.getByTestId("audit-scope")).toHaveTextContent("41 entries"));
    await userEvent.click(node(/^Planner:/));
    expect(node(/^Planner:/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("audit-scope")).toHaveTextContent("4 of 41 entries");
    expect(screen.getByTestId("stage-inspect")).toHaveTextContent("Planner · 1 visit · search_trains ×2");
    await userEvent.click(screen.getByRole("button", { name: "Clear filter Planner" }));
    expect(screen.getByTestId("audit-scope")).toHaveTextContent("41 entries");
  });
});

describe("describeFailure", () => {
  it("words known stream errors and keeps others readable", () => {
    expect(describeFailure("McpTransportError")).toBe("Lost connection to the travel tools (McpTransportError).");
    expect(describeFailure("McpTransportError", "travel_search.get_fares")).toBe(
      "Lost connection to the travel tools during travel_search.get_fares (McpTransportError).",
    );
    expect(describeFailure("the fares tool timed out")).toBe("the fares tool timed out.");
  });
});

describe("errorText", () => {
  it("drops the Error prefix", () => {
    expect(errorText(new Error("boom"))).toBe("boom");
    expect(errorText("plain")).toBe("plain");
  });
});

describe("modeBadges", () => {
  it("says simulated unless the backend reports real parts", () => {
    expect(modeBadges(null)).toEqual([{ label: "simulated", variant: "neutral" }]);
    expect(modeBadges({ timetable: "simulated", payments: "simulated" }).map((b) => b.label)).toEqual(["simulated"]);
    expect(modeBadges({ timetable: "live", payments: "stripe_test" }).map((b) => b.label)).toEqual([
      "live timetable",
      "test payments",
    ]);
  });

  it("shows the backend mode in the header", async () => {
    const client: ApiClient = { ...demoClient({ scenario: "clean" }), getMode: async () => ({ timetable: "live", payments: "simulated" }) };
    render(<App client={client} paceMs={0} />);
    await waitFor(() => expect(screen.getAllByTestId("mode-badge").map((b) => b.textContent)).toEqual(["live timetable"]));
  });
});
