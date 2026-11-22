---
status: accepted
date: 2026-11-21
---

# The customer confirms the payment on the processor's page

ADR-6 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

An agent that confirms a card payment with a stored card on the customer's behalf
is a merchant-initiated payment: under strong customer authentication rules it needs a prior
mandate, and the customer never authenticates the amount the human approved.

## Decision Outcome

With the test-mode processor, the default flow opens a hosted Checkout Session
for exactly the approved total and pauses the run (`await_payment`). Completion comes only
from the processor's record (signed webhook, or the session read back on demand), applied by
a conditional ledger update under the run's lock, then read by the resumed graph through the
gate. The merchant-initiated alternative is the wallet (ADR-7), not a stored test card.

### Consequences

A second pause and two more tools (`payments.create_checkout`,
approval-gated; `payments.checkout_status`, read-only). The Checkout page lives at least 30
minutes, longer than the 15-minute seat holds: a late payment is flagged
`paid_after_hold_expiry` instead of being refused (the money already moved).
