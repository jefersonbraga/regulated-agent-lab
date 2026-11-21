import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RecentRuns } from "./RecentRuns";
import type { RunSummary } from "../types";

const base: RunSummary = {
  run_id: "r1",
  created_at: "2026-11-21T13:47:12.000000+00:00",
  updated_at: "2026-11-21T13:48:02.000000+00:00",
  request_redacted: "Zurich HB to Bern",
  status: "completed",
  total_cents: 5600,
  currency: "CHF",
  payment_id: "pi_test_abc",
  flags: [],
  settlement: "confirmed",
};

describe("RecentRuns settlement", () => {
  it("marks refunded and disputed runs, in words, and leaves confirmed ones plain", () => {
    render(
      <RecentRuns
        runs={[
          { ...base, run_id: "a", settlement: "refunded" },
          { ...base, run_id: "b", settlement: "disputed" },
          { ...base, run_id: "c", settlement: "confirmed" },
          { ...base, run_id: "d", settlement: null, payment_id: "PAY-1" },
        ]}
        onOpen={() => {}}
      />,
    );
    const rows = screen.getAllByTestId("recent-run");
    expect(rows[0]).toHaveTextContent("Refunded");
    expect(rows[0]).toHaveAttribute("title", expect.stringContaining("refunded"));
    expect(rows[1]).toHaveTextContent("Disputed");
    const marks = screen.getAllByTestId("recent-run-settlement");
    expect(marks.map((m) => m.dataset.settlement)).toEqual(["refunded", "disputed"]);
    expect(rows[2]).not.toHaveTextContent("Refunded");
    // The total stays: the mark says what happened to it afterwards.
    expect(rows[0]).toHaveTextContent("CHF 56.00");
  });
});
