import { useEffect, useId, useRef, useState, type FormEvent, type Ref } from "react";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { WalletView } from "../types";
import { formatCentsShort, parseCents } from "../format";
import { cardExpiry, cardLabel, PROCESSOR, TEST_CARD_NUMBER } from "../processor";

/** The processor's test card whose bank always asks for authentication off-session. */
export const SCA_TEST_CARD_NUMBER = "4000 0027 6000 3184";

export interface WalletPanelProps {
  wallet: WalletView;
  /** Opens the processor's hosted card form (the browser leaves the console). */
  onSave: () => Promise<void> | void;
  onRemove: () => Promise<void> | void;
  /** Sets the spending limit per booking, integer cents. */
  onSetLimit: (limitCents: number) => Promise<void> | void;
  /** A one-line outcome to announce, e.g. after coming back from the processor's form. */
  notice?: string | null;
  busy?: boolean;
  headingRef?: Ref<HTMLHeadingElement>;
}

/** The limit as typed in the field: francs, no currency, "200" or "200.50". */
function limitText(cents: number): string {
  return cents % 100 === 0 ? String(cents / 100) : (cents / 100).toFixed(2);
}

/** Why a typed limit is refused, or null when it is a valid number of cents in range. */
export function limitError(text: string, wallet: WalletView): string | null {
  const cents = parseCents(text);
  if (cents === null) return `Enter an amount in ${wallet.currency}, e.g. 200 or 150.50.`;
  if (cents < wallet.min_mandate_cents || cents > wallet.max_mandate_cents) {
    return `Between ${formatCentsShort(wallet.min_mandate_cents, wallet.currency)} and ${formatCentsShort(
      wallet.max_mandate_cents,
      wallet.currency,
    )}.`;
  }
  return null;
}

/**
 * The signed-in user's payment method in the wallet institution: the saved card (brand, last
 * four digits, expiry; the number stays with the processor) and the spending limit the agent may
 * pay per booking without the customer on the processor's page.
 */
export function WalletPanel({ wallet, onSave, onRemove, onSetLimit, notice, busy = false, headingRef }: WalletPanelProps) {
  const [limit, setLimit] = useState(() => limitText(wallet.mandate_cents));
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState<"card" | "remove" | "limit" | null>(null);
  const savingRef = useRef(false);
  const noteId = useId();
  // A limit saved elsewhere (or the server's answer) replaces the field unless it is being edited.
  useEffect(() => {
    if (!touched) setLimit(limitText(wallet.mandate_cents));
  }, [wallet.mandate_cents, touched]);

  const error = touched ? limitError(limit, wallet) : null;
  const typed = parseCents(limit);
  const unchanged = typed === wallet.mandate_cents;
  const locked = busy || saving !== null;

  async function run(kind: "card" | "remove" | "limit", action: () => Promise<void> | void) {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(kind);
    try {
      await action();
    } finally {
      savingRef.current = false;
      setSaving(null);
    }
  }

  function submitLimit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    const cents = parseCents(limit);
    if (cents === null || limitError(limit, wallet) || cents === wallet.mandate_cents) return;
    void run("limit", async () => {
      await onSetLimit(cents);
      setTouched(false);
    });
  }

  const card = wallet.card;
  return (
    <Card padding={4} role="region" aria-label="Payment method" className="wallet-panel" id="payment-method">
      <div className="gate-card-body">
        <div className="wallet-head">
          <Heading level={2} ref={headingRef} tabIndex={-1} className="focus-target">
            Payment method
          </Heading>
          <Badge variant="info" label="wallet · test mode" />
        </div>
        {notice && (
          <Text as="p" role="status" data-testid="wallet-notice">
            {notice}
          </Text>
        )}
        {!wallet.enabled && (
          <Text type="supporting" color="secondary" as="p" data-testid="wallet-disabled">
            Wallet payments are off on this server (PAYMENTS_FLOW): every booking is paid on {PROCESSOR}'s page.
          </Text>
        )}
        {card ? (
          <div className="wallet-card" data-testid="wallet-card">
            <Text weight="semibold" hasTabularNumbers>
              {cardLabel(card)}
            </Text>
            <Text type="supporting" color="secondary" hasTabularNumbers>
              expires {cardExpiry(card)}
            </Text>
          </div>
        ) : (
          <Text color="secondary" as="p" data-testid="wallet-empty">
            No card saved. Without one the agent cannot pay for you; you pay on {PROCESSOR}'s page instead.
          </Text>
        )}
        <div className="outcome-actions">
          <Button
            label={card ? "Replace card" : "Save a card"}
            size="sm"
            variant={card ? "secondary" : "primary"}
            isLoading={saving === "card"}
            isDisabled={locked || !wallet.enabled}
            aria-describedby={noteId}
            onClick={() => void run("card", onSave)}
          />
          {card && (
            <Button
              label="Remove card"
              size="sm"
              variant="ghost"
              isLoading={saving === "remove"}
              isDisabled={locked}
              onClick={() => void run("remove", onRemove)}
            />
          )}
        </div>
        <Text id={noteId} type="supporting" color="secondary" as="p" data-testid="wallet-note">
          You type the card on {PROCESSOR}'s own page and {PROCESSOR} keeps it. This console, the agents and the model
          only ever see the brand, the last four digits and the expiry. Test cards: {TEST_CARD_NUMBER} pays,{" "}
          {SCA_TEST_CARD_NUMBER} makes the bank ask for authentication.
        </Text>
        <form className="wallet-limit" onSubmit={submitLimit} noValidate>
          <TextInput
            label={`Limit per booking (${wallet.currency})`}
            description="The agent may pay up to this from your wallet; above it you pay on the processor's page."
            value={limit}
            onChange={(v) => {
              setLimit(v);
              setTouched(true);
            }}
            status={error ? { type: "error", message: error } : undefined}
            isDisabled={locked}
            size="sm"
            width={140}
            htmlName="limit"
          />
          <Button
            type="submit"
            label="Save limit"
            size="sm"
            variant="secondary"
            isLoading={saving === "limit"}
            isDisabled={locked || !!error || unchanged}
          />
        </form>
        <Text type="supporting" color="secondary" as="p" data-testid="wallet-limit">
          Current limit: {formatCentsShort(wallet.mandate_cents, wallet.currency)} per booking.
        </Text>
      </div>
    </Card>
  );
}
