import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { limitError, WalletPanel } from "./WalletPanel";
import type { WalletView } from "../types";

const empty: WalletView = {
  user: "alice",
  enabled: true,
  card: null,
  mandate_cents: 20000,
  currency: "CHF",
  min_mandate_cents: 100,
  max_mandate_cents: 1_000_000,
};
const saved: WalletView = { ...empty, card: { brand: "visa", last4: "4242", exp_month: 12, exp_year: 2034 } };

describe("WalletPanel", () => {
  it("offers to save a card and says the card stays with the processor", async () => {
    const onSave = vi.fn();
    render(<WalletPanel wallet={empty} onSave={onSave} onRemove={vi.fn()} onSetLimit={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Payment method" })).toBeInTheDocument();
    expect(screen.getByTestId("wallet-empty")).toHaveTextContent("No card saved");
    expect(screen.getByTestId("wallet-note")).toHaveTextContent(
      "only ever see the brand, the last four digits and the expiry",
    );
    expect(screen.getByTestId("wallet-note")).toHaveTextContent("4000 0027 6000 3184");
    expect(screen.queryByRole("button", { name: "Remove card" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save a card" }));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("shows the saved card by brand, last four and expiry, with Replace and Remove", async () => {
    const onSave = vi.fn();
    const onRemove = vi.fn();
    render(<WalletPanel wallet={saved} onSave={onSave} onRemove={onRemove} onSetLimit={vi.fn()} />);
    expect(screen.getByTestId("wallet-card")).toHaveTextContent("Visa ····4242expires 12/2034");
    await userEvent.click(screen.getByRole("button", { name: "Replace card" }));
    expect(onSave).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Remove card" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("sets the limit in integer cents and refuses what is not an amount", async () => {
    const onSetLimit = vi.fn(() => Promise.resolve());
    render(<WalletPanel wallet={saved} onSave={vi.fn()} onRemove={vi.fn()} onSetLimit={onSetLimit} />);
    const input = screen.getByRole("textbox", { name: /Limit per booking \(CHF\)/ });
    expect(input).toHaveValue("200");
    expect(screen.getByRole("button", { name: "Save limit" })).toHaveAttribute("aria-disabled", "true");
    await userEvent.clear(input);
    await userEvent.type(input, "abc");
    expect(screen.getAllByText("Enter an amount in CHF, e.g. 200 or 150.50.").length).toBeGreaterThan(0);
    await userEvent.clear(input);
    await userEvent.type(input, "150.5");
    await userEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(onSetLimit).toHaveBeenCalledWith(15050));
    expect(screen.getByTestId("wallet-limit")).toHaveTextContent("Current limit: CHF 200 per booking.");
  });

  it("announces what happened and says when the wallet is off", () => {
    render(
      <WalletPanel
        wallet={{ ...empty, enabled: false }}
        notice="Card removed from your wallet."
        onSave={vi.fn()}
        onRemove={vi.fn()}
        onSetLimit={vi.fn()}
      />,
    );
    expect(screen.getByTestId("wallet-notice")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("wallet-disabled")).toHaveTextContent("PAYMENTS_FLOW");
    expect(screen.getByRole("button", { name: "Save a card" })).toHaveAttribute("aria-disabled", "true");
  });

  it("checks the limit range", () => {
    expect(limitError("0.5", empty)).toBe("Between CHF 1 and CHF 10000.");
    expect(limitError("200", empty)).toBeNull();
    expect(limitError("200,25", empty)).toBeNull();
    expect(limitError("1e3", empty)).not.toBeNull();
  });
});
