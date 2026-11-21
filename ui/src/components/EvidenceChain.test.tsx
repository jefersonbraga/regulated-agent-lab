import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EvidenceChain, chainColumns, entriesForNode, showVerifyBadge } from "./EvidenceChain";
import { lamps, primaryLamp } from "./Signals";
import { audit, awaiting, completed, refused } from "../fixtures/events";

const entries = audit.poisoned.completed;

describe("entriesForNode", () => {
  it("maps stage nodes to audit actors, with payments as the charge rows", () => {
    expect(entriesForNode(entries, null)).toHaveLength(50);
    expect(entriesForNode(entries, "planner").every((e) => e.actor === "planner")).toBe(true);
    expect(entriesForNode(entries, "approval").map((e) => e.event)).toEqual(["approval_decision"]);
    expect(entriesForNode(entries, "payments")).toHaveLength(2);
    expect(entriesForNode(entries, "booking")).toHaveLength(4);
  });
});

describe("chainColumns", () => {
  it("balances rows so the last one is never a widow", () => {
    // 20 fit in a row: 26 blocks become 13 + 13, 21 become 11 + 10.
    const w = 38 + 19 * 47;
    expect(chainColumns(26, w)).toBe(13);
    expect(chainColumns(21, w)).toBe(11);
    expect(chainColumns(20, w)).toBe(20);
    expect(chainColumns(5, w)).toBe(5);
    expect(chainColumns(26, 38)).toBe(1);
    expect(chainColumns(0, w)).toBe(1);
  });
});

describe("showVerifyBadge", () => {
  it("needs entries, except for a broken chain", () => {
    expect(showVerifyBadge([], null)).toBe(false);
    expect(showVerifyBadge([], { ok: true, entries: 0, first_bad_seq: null, reason: null })).toBe(false);
    expect(showVerifyBadge([], { ok: false, entries: 3, first_bad_seq: 1, reason: "x" })).toBe(true);
    expect(showVerifyBadge(entries, null)).toBe(true);
  });
});

describe("EvidenceChain", () => {
  it("draws one block per entry, marks flagged and human blocks, and opens a hash table", async () => {
    const onVerify = vi.fn();
    render(
      <EvidenceChain entries={entries} verify={null} filter={null} onClearFilter={() => {}} onVerify={onVerify} />,
    );
    const blocks = within(screen.getByRole("list", { name: "Audit blocks" })).getAllByRole("button");
    expect(blocks).toHaveLength(50);
    expect(blocks.filter((b) => b.dataset.tone === "flagged")).toHaveLength(2);
    expect(document.getElementById("audit-block-43")).toHaveAttribute("data-tone", "approved");
    expect(screen.getByTestId("verify-badge")).toHaveTextContent("not verified");
    await userEvent.click(screen.getByRole("button", { name: "Verify chain" }));
    expect(onVerify).toHaveBeenCalledTimes(1);
    // State is in words, not colour only.
    expect(document.getElementById("audit-block-3")).toHaveAccessibleName(/flagged by injection guard$/);
    expect(document.getElementById("audit-block-43")).toHaveAccessibleName(/human approval$/);
    expect(screen.getByTestId("chain-legend")).toHaveTextContent("tool call");
    // Each row starts on a bare block: with no width measured, 50 blocks wrap 17 + 17 + 16.
    const links = document.querySelectorAll(".chain-link");
    expect(links).toHaveLength(47);
    // A block opens its detail: the hash and the link to the block before it.
    await userEvent.click(document.getElementById("audit-block-3")!);
    const detail = screen.getByTestId("block-detail");
    expect(detail).toHaveTextContent("#3 · planner · injection_flagged");
    expect(detail).toHaveTextContent("[0].notes: ignore (all )?(previous|prior) instructions");
    expect(document.getElementById("audit-block-3")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "Close block detail" }));
    expect(screen.queryByTestId("block-detail")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getAllByTestId("hash-chip")).toHaveLength(50);
  });

  it("says the chain is recording while a run is in progress", () => {
    render(<EvidenceChain entries={[]} verify={null} filter={null} onClearFilter={() => {}} onVerify={() => {}} recording />);
    expect(screen.getByTestId("audit-recording")).toHaveTextContent("recording…");
    expect(screen.getByTestId("audit-empty")).toHaveTextContent("loads when the run pauses for approval or ends");
    expect(screen.getByRole("button", { name: "Verify chain" })).toHaveAttribute("aria-disabled", "true");
  });

  it("marks the first bad block and dims the ones after it", () => {
    render(
      <EvidenceChain
        entries={entries}
        verify={{ ok: false, entries: 26, first_bad_seq: 10, reason: "hash mismatch" }}
        filter={null}
        onClearFilter={() => {}}
        onVerify={() => {}}
      />,
    );
    expect(document.getElementById("audit-block-10")).toHaveAttribute("data-broken", "true");
    expect(document.getElementById("audit-block-11")).toHaveAttribute("data-after-break", "true");
    expect(screen.getByTestId("verify-badge")).toHaveTextContent("Chain broken at seq 10: hash mismatch");
    expect(screen.getByTestId("chain-break-note")).toHaveTextContent("Block #10 no longer matches its stored hash");
    expect(document.getElementById("audit-block-12")).toHaveAccessibleName(/after the break$/);
  });

  it("marks a policy denial", () => {
    render(
      <EvidenceChain entries={audit.denied} verify={null} filter={null} onClearFilter={() => {}} onVerify={() => {}} />,
    );
    expect(document.querySelectorAll('.chain-block[data-tone="denied"]')).toHaveLength(1);
  });
});

describe("lamps", () => {
  const byId = (l: ReturnType<typeof lamps>) => Object.fromEntries(l.map((x) => [x.id, x.value]));

  it("stays dark before any run", () => {
    expect(byId(lamps(null, null))).toEqual({ gate: "idle", guard: "quiet", chain: "unchecked" });
  });

  it("lights from the run and the verify result", () => {
    expect(byId(lamps(awaiting, null))).toMatchObject({ gate: "waiting", guard: "2 caught" });
    expect(byId(lamps(completed, { ok: true, entries: 26, first_bad_seq: null, reason: null }))).toEqual({
      gate: "approved",
      guard: "2 caught",
      chain: "intact",
    });
    expect(byId(lamps(refused, { ok: false, entries: 2, first_bad_seq: 1, reason: null }))).toMatchObject({
      gate: "refused",
      guard: "quiet",
      chain: "broken at 1",
    });
  });

  it("follows the live status while the run has no view yet, and a phone shows the most severe", () => {
    const running = lamps(null, null, "running");
    expect(byId(running).gate).toBe("working");
    expect(primaryLamp(running)).toBe("gate");
    const failed = lamps(null, { ok: true, entries: 4, first_bad_seq: null, reason: null }, "failed");
    expect(byId(failed)).toMatchObject({ gate: "stopped", chain: "intact" });
    expect(primaryLamp(failed)).toBe("gate");
  });
});

describe("processor events", () => {
  const last = entries[entries.length - 1];
  const processor = (seq: number, event: string, data: Record<string, unknown> = {}) => ({
    seq,
    ts: "2026-11-21T21:34:05.000000+00:00",
    run_id: last.run_id,
    actor: "processor",
    event,
    data: { payment_id: "pi_test_abc", amount_cents: 5600, currency: "CHF", ...data },
    prev_hash: "a".repeat(64),
    hash: "b".repeat(64),
  });
  const withProcessor = [
    ...entries,
    processor(last.seq + 1, "payment_confirmed", { settlement: "confirmed" }),
    processor(last.seq + 2, "payment_refunded", { settlement: "refunded" }),
  ];

  it("renders processor events as blocks with their own actor, glyph and words", async () => {
    render(<EvidenceChain entries={withProcessor} verify={null} filter={null} onClearFilter={() => {}} onVerify={() => {}} />);
    const confirmed = document.getElementById(`audit-block-${last.seq + 1}`)!;
    const refunded = document.getElementById(`audit-block-${last.seq + 2}`)!;
    expect(confirmed).toHaveAttribute("data-actor", "processor");
    expect(confirmed).toHaveAttribute("data-tone", "processor");
    expect(confirmed).toHaveAccessibleName(/processor, payment_confirmed, processor: payment confirmed$/);
    // Money that went back is marked, in words and not by colour alone.
    expect(refunded).toHaveAttribute("data-tone", "processor-alert");
    expect(refunded).toHaveAccessibleName(/processor: payment refunded$/);
    const legend = screen.getByTestId("chain-legend");
    expect(legend).toHaveTextContent("processor event");
    expect(legend.querySelector('[data-actor="processor"]')).not.toBeNull();
    await userEvent.click(refunded);
    const detail = screen.getByTestId("block-detail");
    expect(detail).toHaveTextContent("processor · payment_refunded");
    expect(detail).toHaveTextContent("pi_test_abc");
  });

  it("files processor events under the payments node", () => {
    expect(entriesForNode(withProcessor, "payments")).toHaveLength(4);
    expect(entriesForNode(withProcessor, "booking")).toHaveLength(4);
  });
});
