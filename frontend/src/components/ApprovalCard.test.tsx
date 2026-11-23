import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  ApprovalCard,
  collapsedOptions,
  countLine,
  fastestOption,
  readablePattern,
  tripLine,
  walletChoiceLabel,
  walletReason,
} from "./ApprovalCard";
import type { AuditEntry, Offer, Option, PaymentOptions, Quote } from "../types";
import { QUOTE_HASH, QUOTE_HASHES, RECOMMENDED_FARE_IDS, offer, quote, quoteFor } from "../fixtures/events";

// Astryx buttons stay focusable while locked: they report it with aria-disabled, not `disabled`.
const expectLocked = (name: string) =>
  expect(screen.getByRole("button", { name })).toHaveAttribute("aria-disabled", "true");
const expectUnlocked = (name: string) =>
  expect(screen.getByRole("button", { name })).not.toHaveAttribute("aria-disabled");

const HASH = QUOTE_HASH;
const FARE_IDS = RECOMMENDED_FARE_IDS;
const FLAGS = ["injection_flagged:travel_search.search_trains", "injection_flagged:travel_search.search_trains"];

describe("ApprovalCard", () => {
  it("explains a required second approver and keeps both decisions locked", () => {
    const onDecision = vi.fn();
    render(<ApprovalCard quote={quote} onDecision={onDecision} approverNote="You requested this trip." />);
    const note = screen.getByTestId("approver-note");
    expect(note).toHaveTextContent("Waiting for a second person");
    expect(note).toHaveTextContent("You requested this trip.");
    expectLocked("Approve");
    expectLocked("Reject");
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecision).not.toHaveBeenCalled();
  });

  it("shows each leg as a compact route and the total big", () => {
    render(<ApprovalCard quote={quote} onDecision={vi.fn()} />);
    const outbound = screen.getByRole("listitem", { name: "Outbound leg, Zurich HB → Bern" });
    expect(outbound).toHaveTextContent("Tue 1 Dec 2026ZUE09:02BER09:582nd class saver · CHF 28.00");
    expect(outbound).toHaveAttribute("title", "Zurich HB → Bern");
    expect(screen.getByRole("listitem", { name: /^Return leg/ })).toHaveTextContent("BER09:02ZUE09:58");
    expect(screen.getByTestId("trip-line")).toHaveTextContent("Demo Rail · round trip · times local (Zurich)");
    expect(screen.getByTestId("quote-total")).toHaveTextContent("CHF 56.00");
    // The rationale is a secondary detail behind an Info Tip, not a paragraph on the card; the
    // only paragraph says what Approve will do with the money.
    expect(screen.getAllByRole("paragraph")).toEqual([screen.getByTestId("charge-notice")]);
    expect(screen.getByTestId("charge-notice")).toHaveTextContent("simulated payment");
    expect(screen.getByRole("button", { name: "Why this quote" })).toBeInTheDocument();
  });

  it("exposes a focusable heading and shortens the quote hash, copying the full value", async () => {
    const headingRef = { current: null as HTMLHeadingElement | null };
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<ApprovalCard quote={quote} onDecision={vi.fn()} headingRef={headingRef} />);
    const heading = screen.getByRole("heading", { name: "Approve payment?" });
    expect(headingRef.current).toBe(heading);
    expect(heading).toHaveAttribute("tabindex", "-1");
    const chip = screen.getByRole("button", { name: `Copy quote hash ${HASH}` });
    expect(chip).toHaveTextContent(HASH.slice(0, 8));
    expect(chip).not.toHaveTextContent(HASH);
    await userEvent.click(chip);
    expect(writeText).toHaveBeenCalledWith(HASH);
    await waitFor(() => expect(chip).toHaveAttribute("data-copied", "true"));
  });

  it("adds one warning banner and asks for a review before Approve unlocks", async () => {
    const onDecision = vi.fn((_approved: boolean, _hash: string, _fareIds: string[]) => Promise.resolve());
    render(<ApprovalCard quote={quote} flags={FLAGS} onDecision={onDecision} />);
    expect(screen.getAllByTestId("run-flags")).toHaveLength(1);
    expect(screen.getByTestId("run-flags")).toHaveTextContent("Possible prompt injection · 2 tool results flagged");
    // What the guard did is on the card, not behind a hover-only tip.
    expect(screen.getByTestId("run-flags")).toHaveTextContent("Source: travel_search.search_trains, 2 results.");
    expect(screen.getByTestId("run-flags")).toHaveTextContent(/structured fields only/);
    expectLocked("Approve");
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toContainElement(screen.getByTestId("approve-lock"));
    expect(approve).toHaveAccessibleDescription("Tick the review box to unlock");
    await userEvent.click(approve);
    expect(onDecision).not.toHaveBeenCalled();
    // Reject never needs the review.
    expectUnlocked("Reject");
    await userEvent.click(
      screen.getByRole("checkbox", {
        name: "I checked the 2 flagged tool results; the quote uses only structured fields",
      }),
    );
    expectUnlocked("Approve");
    expect(screen.queryByTestId("approve-lock")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledWith(true, HASH, FARE_IDS);
  });

  it("lists the flagged results to review and links them to the chain", async () => {
    const onShowFlagged = vi.fn();
    const flagged: AuditEntry[] = [3, 6].map((seq) => ({
      seq,
      ts: "2026-11-21T16:18:25+00:00",
      run_id: "r",
      actor: "planner",
      event: "injection_flagged",
      data: { tool: "travel_search.search_trains", findings: ["[0].notes: ignore (all )?(previous|prior) instructions"] },
      prev_hash: "a",
      hash: "b",
    }));
    render(
      <ApprovalCard
        quote={quote}
        flags={FLAGS}
        flaggedEntries={flagged}
        onShowFlagged={onShowFlagged}
        request="Zurich to Bern"
        onDecision={vi.fn()}
      />,
    );
    const evidence = screen.getByTestId("flag-evidence");
    expect(evidence).toHaveTextContent("#3 · travel_search.search_trains");
    expect(evidence).toHaveTextContent("[0].notes matched “ignore previous instructions”");
    await userEvent.click(screen.getByRole("button", { name: "Show flagged results in chain (#3, #6)" }));
    expect(onShowFlagged).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("approval-request")).toHaveTextContent("for “Zurich to Bern”");
  });

  it("shows no banner or review step for a clean run", () => {
    render(<ApprovalCard quote={quote} flags={[]} onDecision={vi.fn()} />);
    expect(screen.queryByTestId("run-flags")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expectUnlocked("Approve");
  });

  it("sends one decision per click burst and locks both buttons while it is in flight", async () => {
    let resolve: () => void = () => {};
    const onDecision = vi.fn(
      () =>
        new Promise<void>((r) => {
          resolve = r;
        }),
    );
    render(<ApprovalCard quote={quote} onDecision={onDecision} />);
    await userEvent.dblClick(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledTimes(1);
    expect(onDecision).toHaveBeenCalledWith(true, HASH, FARE_IDS);
    expectLocked("Approve");
    expectLocked("Reject");
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecision).toHaveBeenCalledTimes(1);
    resolve();
    await waitFor(() => expectUnlocked("Approve"));
    expectUnlocked("Reject");
  });

  it("reject calls onDecision(false)", async () => {
    const onDecision = vi.fn((_approved: boolean, _hash: string, _fareIds: string[]) => Promise.resolve());
    render(<ApprovalCard quote={quote} onDecision={onDecision} />);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecision).toHaveBeenCalledWith(false, HASH, FARE_IDS);
  });

  it("ignores clicks while disabled", async () => {
    const onDecision = vi.fn();
    render(<ApprovalCard quote={quote} onDecision={onDecision} disabled />);
    expectLocked("Approve");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).not.toHaveBeenCalled();
  });

  it("shows one deadline chip, with the ISO instant in its title", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-21T09:00:00Z"));
    try {
      const iso = "2026-11-21T10:15:30.123456+00:00";
      const { unmount } = render(<ApprovalCard quote={quote} approveBefore={iso} onDecision={vi.fn()} />);
      const chip = screen.getByTestId("approve-before");
      expect(chip).toHaveTextContent("approve by 10:15 UTC today");
      expect(chip).toHaveAttribute("title", iso);
      expect(chip.querySelector("[data-variant]")).toHaveAttribute("data-variant", "neutral");
      unmount();
      render(<ApprovalCard quote={quote} approveBefore="2026-11-22T00:05:00+00:00" onDecision={vi.fn()} />);
      expect(screen.getByTestId("approve-before")).toHaveTextContent("approve by 2026-11-22 00:05 UTC");
    } finally {
      vi.useRealTimers();
    }
  });

  it("turns the deadline chip into a countdown in the last five minutes", () => {
    const at = (ms: number) => new Date(Date.now() + ms).toISOString();
    render(<ApprovalCard quote={quote} approveBefore={at(4 * 60_000 + 30_000)} onDecision={vi.fn()} />);
    const chip = screen.getByTestId("approve-before");
    expect(chip).toHaveTextContent("expires in 4 min");
    expect(chip.querySelector("[data-variant]")).toHaveAttribute("data-variant", "warning");
  });

  it("expires the quote once the deadline passes: Approve locks and a new quote can be requested", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date("2026-11-21T16:34:50Z"));
    const onDecision = vi.fn();
    const onRequestNew = vi.fn();
    try {
      render(
        <ApprovalCard
          quote={quote}
          approveBefore="2026-11-21T16:35:00+00:00"
          onDecision={onDecision}
          onRequestNew={onRequestNew}
        />,
      );
      expectUnlocked("Approve");
      expect(screen.queryByTestId("approval-expired")).not.toBeInTheDocument();
      await act(async () => {
        vi.advanceTimersByTime(30_000);
      });
      expectLocked("Approve");
      const chip = screen.getByTestId("approve-before");
      expect(chip).toHaveTextContent("expired at 16:35 UTC");
      expect(chip.querySelector("[data-variant]")).toHaveAttribute("data-variant", "error");
      expect(screen.getByTestId("approval-expired")).toHaveTextContent("can no longer be approved");
      fireEvent.click(screen.getByRole("button", { name: "Approve" }));
      expect(onDecision).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Request a new quote" }));
      expect(onRequestNew).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("labels the amount as the total to charge", () => {
    render(<ApprovalCard quote={quote} onDecision={vi.fn()} />);
    expect(screen.getByTestId("quote-total").parentElement).toHaveTextContent("Total to charge");
  });

  it("warns when the offer has no readable expiry", () => {
    render(<ApprovalCard quote={quote} approveBefore={null} onDecision={vi.fn()} />);
    expect(screen.getByTestId("approve-before")).toHaveTextContent(/no expiry/);
  });
});

describe("ApprovalCard with an offer", () => {

  it("lists the trains per leg, with the cheapest preselected and labelled", () => {
    render(<ApprovalCard quote={quote} offer={offer} onQuote={vi.fn()} onDecision={vi.fn()} />);
    const outbound = screen.getByRole("radiogroup", { name: "Outbound · Zurich HB → Bern · Tue 1 Dec 2026" });
    const options = within(outbound).getAllByRole("radio");
    // Six morning trains, four on screen until "Show all".
    expect(options).toHaveLength(4);
    expect(options[0]).toHaveAccessibleName("06:02 to 06:58, 56 min, IC, direct, 2nd class, standard, CHF 56.00");
    expect(options[3]).toHaveAccessibleName("09:02 to 09:58, 56 min, IC, direct, 2nd class, saver, CHF 28.00, cheapest");
    expect(options[3]).toBeChecked();
    expect(within(outbound).getAllByTestId("cheapest")).toHaveLength(1);
    // All trains take as long: no "fastest" badge to single one out.
    expect(within(outbound).queryByTestId("fastest")).not.toBeInTheDocument();
    expect(within(outbound).getAllByTestId("train-line")[0]).toHaveTextContent("56 min · IC · direct");
    expect(within(outbound).getAllByTestId("fare-type").map((b) => b.textContent)).toEqual([
      "standard",
      "standard",
      "standard",
      "saver",
    ]);
    expect(screen.getByTestId("leg-count-outbound")).toHaveTextContent("6 trains in your window · showing 4");
    const back = screen.getByRole("radiogroup", { name: "Return · Bern → Zurich HB · Wed 2 Dec 2026" });
    expect(within(back).getAllByRole("radio")[2]).toBeChecked();
    expect(screen.getByTestId("leg-count-return")).toHaveTextContent("12 trains in your window · showing 4");
    expect(screen.getByTestId("quote-total")).toHaveTextContent("CHF 56.00");
  });

  it("shows every train of a leg on request and keeps a picked one visible when collapsed again", async () => {
    const onQuote = vi.fn((ids: string[]) => Promise.resolve(quoteFor(ids) as Quote));
    render(<ApprovalCard quote={quote} offer={offer} onQuote={onQuote} onDecision={vi.fn()} />);
    const back = () => screen.getByRole("radiogroup", { name: /^Return/ });
    const showAll = within(screen.getByTestId("leg-choice-return")).getByRole("button", { name: "Show all" });
    expect(showAll).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(showAll);
    expect(within(back()).getAllByRole("radio")).toHaveLength(12);
    expect(screen.getByTestId("leg-count-return")).toHaveTextContent("12 trains in your window · showing all");
    const fewer = within(screen.getByTestId("leg-choice-return")).getByRole("button", { name: "Show fewer" });
    expect(fewer).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(within(back()).getByRole("radio", { name: /^18:02 to 18:58/ }));
    await userEvent.click(fewer);
    const rows = within(back()).getAllByRole("radio");
    expect(rows.map((r) => r.getAttribute("aria-label")?.slice(0, 5))).toEqual(["07:02", "08:02", "09:02", "18:02"]);
    expect(within(back()).getByRole("radio", { name: /^18:02/ })).toBeChecked();
    // The other leg is unaffected.
    expect(within(screen.getByRole("radiogroup", { name: /^Outbound/ })).getAllByRole("radio")).toHaveLength(4);
  });

  it("names the requested window per leg, and says when no time was given", () => {
    render(<ApprovalCard quote={quote} offer={offer} onDecision={vi.fn()} />);
    const outbound = screen.getByTestId("leg-choice-outbound");
    const back = screen.getByTestId("leg-choice-return");
    expect(within(outbound).getByText("Morning 06:00–12:00")).toBeInTheDocument();
    expect(within(back).getByText("No time given: showing trains from 07:00")).toBeInTheDocument();
    expect(screen.getAllByText("No time given: showing trains from 07:00")).toHaveLength(1);
  });

  it("asks the server for the quote of a new selection and approves exactly that hash", async () => {
    const onQuote = vi.fn((ids: string[]) => Promise.resolve(quoteFor(ids) as Quote));
    const onDecision = vi.fn((_a: boolean, _h: string, _f: string[]) => Promise.resolve());
    render(<ApprovalCard quote={quote} offer={offer} onQuote={onQuote} onDecision={onDecision} />);
    const outbound = screen.getByRole("radiogroup", { name: /^Outbound/ });
    await userEvent.click(within(outbound).getAllByRole("radio")[0]);
    const ids = ["DR-ZUE-BER-20261201-0602-2nd", FARE_IDS[1]];
    expect(onQuote).toHaveBeenCalledWith(ids);
    await waitFor(() => expect(screen.getByTestId("quote-total")).toHaveTextContent("CHF 84.00"));
    const hash = QUOTE_HASHES[ids.join("|")];
    expect(screen.getByRole("button", { name: `Copy quote hash ${hash}` })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledWith(true, hash, ids);
    // Going back to the recommended trains needs no new request: that quote came with the offer.
    await userEvent.click(within(outbound).getAllByRole("radio")[3]);
    expect(onQuote).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("quote-total")).toHaveTextContent("CHF 56.00");
  });

  it("keeps Approve locked until the server has priced the selection, and after a failure", async () => {
    let answer: (q: Quote) => void = () => {};
    const onQuote = vi
      .fn<(ids: string[]) => Promise<Quote>>()
      .mockImplementationOnce(() => new Promise<Quote>((r) => (answer = r)))
      .mockRejectedValueOnce(new Error("422"));
    const onDecision = vi.fn();
    render(<ApprovalCard quote={quote} offer={offer} onQuote={onQuote} onDecision={onDecision} />);
    const outbound = screen.getByRole("radiogroup", { name: /^Outbound/ });
    await userEvent.click(within(outbound).getAllByRole("radio")[1]);
    expectLocked("Approve");
    expect(screen.getByTestId("quote-total")).toHaveTextContent("pricing…");
    expect(screen.queryByTestId("quote-hash")).not.toBeInTheDocument();
    await act(async () => answer(quoteFor(["DR-ZUE-BER-20261201-0702-2nd", FARE_IDS[1]]) as Quote));
    expectUnlocked("Approve");
    await userEvent.click(within(outbound).getAllByRole("radio")[0]);
    await waitFor(() => expect(screen.getByTestId("quote-error")).toHaveTextContent("Could not price this selection"));
    expectLocked("Approve");
    expect(screen.getByTestId("quote-total")).toHaveTextContent("unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).not.toHaveBeenCalled();
    // Reject still works, bound to the recommended quote.
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecision).toHaveBeenCalledWith(false, HASH, FARE_IDS);
  });

  it("refuses a server answer for a different selection", async () => {
    const onQuote = vi.fn(() => Promise.resolve(quote));
    render(<ApprovalCard quote={quote} offer={offer} onQuote={onQuote} onDecision={vi.fn()} />);
    await userEvent.click(within(screen.getByRole("radiogroup", { name: /^Outbound/ })).getAllByRole("radio")[0]);
    await waitFor(() => expect(screen.getByTestId("quote-error")).toBeInTheDocument());
    expectLocked("Approve");
  });
});

describe("readablePattern", () => {
  it("turns a guard regex into words", () => {
    expect(readablePattern("ignore (all )?(previous|prior) instructions")).toBe("ignore previous instructions");
    expect(readablePattern("payments?\\.charge")).toBe("payments.charge");
    expect(readablePattern("call (the )?(tool|function)")).toBe("call tool");
  });
});

describe("tripLine", () => {
  it("names live-timetable trains and keeps their fares labelled simulated", () => {
    const option = {
      leg: "outbound" as const,
      train_id: "LV-ZUE-BER-20261201-0732-IC1",
      depart: "2026-12-01T07:32:00",
      arrive: "2026-12-01T08:28:00",
      fare_id: "LV-ZUE-BER-20261201-0732-IC1-2nd",
      price_cents: 5600,
      fare_type: "standard" as const,
    };
    const quote: Quote = { options: [option], total_cents: 5600, currency: "CHF", rationale: "", quote_hash: "h" };
    expect(tripLine(quote)).toBe("Live timetable · simulated fares · one way · times local (Zurich)");
  });
});

/** A leg with a slow direct regional train among the IC ones (live timetables have both). */
function mixedOptions(): Option[] {
  const at = (h: number, m: number, minutes: number, price: number, product: string): Option => {
    const hh = String(h).padStart(2, "0");
    const mm = String(m).padStart(2, "0");
    const end = h * 60 + m + minutes;
    const ah = String(Math.floor(end / 60)).padStart(2, "0");
    const am = String(end % 60).padStart(2, "0");
    const train_id = `LV-ZUE-BER-20261201-${hh}${mm}-${product.replace(" ", "")}`;
    return {
      leg: "outbound",
      train_id,
      depart: `2026-12-01T${hh}:${mm}:00`,
      arrive: `2026-12-01T${ah}:${am}:00`,
      fare_id: `${train_id}-2nd`,
      price_cents: price,
      fare_type: price < 5600 ? "saver" : "standard",
      product,
    };
  };
  return [
    at(6, 2, 56, 5600, "IC 8"),
    at(6, 32, 78, 5600, "IR 15"),
    at(7, 2, 56, 5600, "IC 8"),
    at(7, 32, 78, 5600, "IR 15"),
    at(8, 2, 56, 5600, "IC 8"),
    at(9, 32, 78, 2800, "IR 15"),
    at(10, 2, 52, 3000, "IC 1"),
    at(10, 32, 78, 2800, "IR 15"),
  ];
}

describe("collapsedOptions", () => {
  it("always keeps the recommended and the fastest train, then the earliest up to four", () => {
    const options = mixedOptions();
    const cheapest = options[5].fare_id; // 09:32 saver
    expect(fastestOption(options)?.depart).toBe("2026-12-01T10:02:00");
    const shown = collapsedOptions(options, cheapest).map((o) => o.depart.slice(11, 16));
    expect(shown).toEqual(["06:02", "06:32", "09:32", "10:02"]);
  });

  it("keeps the selected train too, and returns rows in departure order", () => {
    const options = mixedOptions();
    const shown = collapsedOptions([...options].reverse(), options[5].fare_id, options[4].fare_id);
    expect(shown.map((o) => o.depart.slice(11, 16))).toEqual(["06:02", "08:02", "09:32", "10:02"]);
  });

  it("shows short legs whole", () => {
    const options = mixedOptions().slice(0, 3);
    expect(collapsedOptions(options, options[0].fare_id)).toHaveLength(3);
    expect(countLine(3, 3)).toBe("3 trains in your window");
    expect(countLine(1, 1)).toBe("1 train in your window");
    expect(countLine(12, 4)).toBe("12 trains in your window · showing 4");
  });

  it("marks the fastest train and names each train's product, all direct", () => {
    const options = mixedOptions();
    const mixed: Offer = {
      legs: [
        {
          leg: "outbound",
          window: { earliest: "06:00:00", latest: "12:00:00" },
          window_defaulted: false,
          options,
          recommended_fare_id: options[5].fare_id,
        },
      ],
      passengers: 1,
      currency: "CHF",
      expires_at: "2099-12-01T06:00:00+00:00",
    };
    const recommendedQuote: Quote = {
      options: [options[5]],
      total_cents: 2800,
      currency: "CHF",
      rationale: "",
      quote_hash: "h",
    };
    render(<ApprovalCard quote={recommendedQuote} offer={mixed} onDecision={vi.fn()} />);
    const leg = screen.getByRole("radiogroup", { name: /^Outbound/ });
    expect(within(leg).getByRole("radio", { name: /^10:02 to 10:54/ })).toHaveAccessibleName(
      "10:02 to 10:54, 52 min, IC 1, direct, 2nd class, saver, CHF 30.00, fastest",
    );
    expect(within(leg).getAllByTestId("fastest")).toHaveLength(1);
    expect(within(leg).getAllByTestId("train-line").map((t) => t.textContent)).toEqual([
      "56 min · IC 8 · direct",
      "1 h 18 min · IR 15 · direct",
      "1 h 18 min · IR 15 · direct",
      "52 min · IC 1 · direct",
    ]);
    expect(screen.getByTestId("leg-count-outbound")).toHaveTextContent("8 trains in your window · showing 4");
  });
});

describe("ApprovalCard payments notice", () => {
  it("names the processor before the decision in test mode", () => {
    render(<ApprovalCard quote={quote} onDecision={vi.fn()} payments="stripe_test" />);
    expect(screen.getByTestId("charge-notice")).toHaveTextContent(
      "On approval: you will confirm the payment on Stripe's page (test mode, use card 4242 4242 4242 4242, any future date, any CVC).",
    );
    // Without payment options from the server there is nothing to choose.
    expect(screen.queryByTestId("payment-choice")).not.toBeInTheDocument();
  });
});

describe("ApprovalCard payment model choice", () => {
  const VISA = { brand: "visa", last4: "4242", exp_month: 12, exp_year: 2034 };
  const options = (over: Partial<PaymentOptions> = {}): PaymentOptions => ({
    models: ["checkout", "wallet"],
    default: "wallet",
    payer: "local-dev",
    card: VISA,
    mandate_cents: 20000,
    wallet_unavailable: null,
    ...over,
  });
  const card = (opts: PaymentOptions, onDecision = vi.fn(), viewer = "local-dev") =>
    render(
      <ApprovalCard
        quote={quote}
        payments="stripe_test"
        paymentsFlow="both"
        paymentOptions={opts}
        viewer={viewer}
        onDecision={onDecision}
      />,
    );

  it("offers Stripe's page or the wallet, proposes the wallet and sends the choice", async () => {
    const onDecision = vi.fn(() => Promise.resolve());
    card(options(), onDecision);
    const choice = screen.getByTestId("payment-choice");
    const wallet = within(choice).getByRole("radio", { name: /Agent pays from my wallet \(Visa ····4242, limit CHF 200\)/ });
    const checkout = within(choice).getByRole("radio", { name: /Pay on Stripe now/ });
    expect(wallet).toBeChecked();
    expect(checkout).not.toBeChecked();
    expect(screen.getByTestId("charge-notice")).toHaveTextContent("the agent pays from the wallet with a single-use token");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledWith(true, HASH, FARE_IDS, "wallet");
  });

  it("sends checkout when the approver picks Stripe's page", async () => {
    const onDecision = vi.fn(() => Promise.resolve());
    card(options(), onDecision);
    await userEvent.click(screen.getByRole("radio", { name: /Pay on Stripe now/ }));
    expect(screen.getByTestId("charge-notice")).toHaveTextContent("you will confirm the payment on Stripe's page");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledWith(true, HASH, FARE_IDS, "checkout");
  });

  it("disables the wallet without a saved card and says why", async () => {
    const onDecision = vi.fn(() => Promise.resolve());
    card(options({ card: null, default: "checkout", wallet_unavailable: "no_saved_card" }), onDecision);
    const wallet = screen.getByRole("radio", { name: /Agent pays from my wallet/ });
    expect(wallet).toBeDisabled();
    expect(screen.getByTestId("payment-choice")).toHaveTextContent("No card saved: add one under Payment method.");
    expect(screen.getByRole("radio", { name: /Pay on Stripe now/ })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledWith(true, HASH, FARE_IDS, "checkout");
  });

  it("disables the wallet above the limit, for the total on screen", async () => {
    const onDecision = vi.fn(() => Promise.resolve());
    // The server proposed the wallet, but CHF 56.00 is above a CHF 50 limit.
    card(options({ mandate_cents: 5000 }), onDecision);
    expect(screen.getByRole("radio", { name: /Agent pays from my wallet/ })).toBeDisabled();
    expect(screen.getByTestId("payment-choice")).toHaveTextContent("Above your limit of CHF 50.");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecision).toHaveBeenCalledWith(true, HASH, FARE_IDS, "checkout");
  });

  it("names whose wallet pays when the approver is not the requester, and a rejection carries no model", async () => {
    const onDecision = vi.fn(() => Promise.resolve());
    card(options({ payer: "alice" }), onDecision, "bob");
    expect(screen.getByRole("radio", { name: /Agent pays from alice's wallet/ })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecision).toHaveBeenCalledWith(false, HASH, FARE_IDS);
  });

  it("explains the wallet reason helper", () => {
    expect(walletReason(options(), 5600)).toBeNull();
    expect(walletReason(options(), 20001)).toBe("over_mandate");
    expect(walletReason(options({ card: null }), 100)).toBe("no_saved_card");
    expect(walletReason(options({ wallet_unavailable: "card_expired" }), 100)).toBe("card_expired");
    expect(walletChoiceLabel(options({ card: null, mandate_cents: 15050 }))).toBe(
      "Agent pays from my wallet (limit CHF 150.50)",
    );
  });
});
