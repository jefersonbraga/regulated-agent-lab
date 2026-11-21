import type { Ref } from "react";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";
import type { Checkout } from "../types";
import { formatCents, formatClock } from "../format";
import { fallbackLine, PROCESSOR, TEST_CARD_NUMBER } from "../processor";

export interface PaymentCardProps {
  checkout: Checkout;
  /** Asks the processor now (POST /runs/{id}/payment/refresh) instead of waiting for its webhook. */
  onCheck?: () => void;
  /** Closes the payment page at the processor; the run ends unless it was already paid. */
  onCancel?: () => void;
  /** The customer came back from the processor's page ("success" or "cancel"). */
  returned?: "success" | "cancel" | null;
  busy?: boolean;
  /** Why the agent could not pay from the wallet (RunView.payment_fallback), if it tried. */
  fallback?: string | null;
  /** The wallet's limit as shown to the user ("CHF 200"), for the over-limit reason. */
  limit?: string | null;
  headingRef?: Ref<HTMLHeadingElement>;
}

/**
 * The run is approved and waits for the customer: the payment happens on the processor's own
 * page, never here, and the run completes only when the processor reports it.
 */
export function PaymentCard({
  checkout,
  onCheck,
  onCancel,
  returned,
  busy = false,
  fallback,
  limit,
  headingRef,
}: PaymentCardProps) {
  const why = fallbackLine(fallback, limit);
  return (
    <Card padding={4} role="region" aria-label="Payment" className="gate-card" data-outcome="awaiting_payment">
      <div className="gate-card-body">
        <div className="gate-head">
          <StatusDot variant="accent" label="Waiting for payment" isPulsing />
          <Heading level={2} ref={headingRef} tabIndex={-1} className="focus-target">
            Pay on {PROCESSOR}
          </Heading>
        </div>
        {why && (
          <Text as="p" role="status" weight="semibold" data-testid="wallet-fallback">
            {why}
          </Text>
        )}
        <div className="amount">
          <Text type="supporting" color="secondary">
            Approved total
          </Text>
          <Text type="display-2" weight="semibold" hasTabularNumbers data-testid="checkout-amount">
            {formatCents(checkout.amount_cents, checkout.currency)}
          </Text>
          <Badge variant="info" label="test mode" />
        </div>
        <Text color="secondary" as="p">
          The customer confirms the payment on {PROCESSOR}'s own page, and authenticates there if the card asks for it.
          The agent never confirms a payment for them.
        </Text>
        <Text type="supporting" color="secondary" as="p" data-testid="test-card-hint">
          Test card {TEST_CARD_NUMBER}, any future date, any CVC. No real money moves.
        </Text>
        {returned === "success" && (
          <Text type="supporting" as="p" role="status" data-testid="checkout-returned">
            Back from {PROCESSOR}. The booking completes once {PROCESSOR} confirms the payment.
          </Text>
        )}
        {returned === "cancel" && (
          <Text type="supporting" as="p" role="status" data-testid="checkout-returned">
            You left the payment page without paying. Pay now, or cancel the payment to end the run.
          </Text>
        )}
        <div className="outcome-actions">
          <a
            className="pay-link"
            href={checkout.checkout_url}
            rel="noopener noreferrer"
            data-testid="pay-link"
            aria-disabled={busy || undefined}
          >
            Pay with {PROCESSOR}
          </a>
          {onCheck && (
            <Button
              label={`Check with ${PROCESSOR}`}
              size="sm"
              variant="secondary"
              onClick={onCheck}
              isDisabled={busy}
              data-testid="check-payment"
            />
          )}
          {onCancel && (
            <Button
              label="Cancel payment"
              size="sm"
              variant="ghost"
              onClick={onCancel}
              isDisabled={busy}
              data-testid="cancel-payment"
            />
          )}
        </div>
        <Text type="supporting" color="secondary" as="p" data-testid="checkout-expiry">
          The payment page closes at {formatClock(checkout.expires_at)}.
        </Text>
      </div>
    </Card>
  );
}
