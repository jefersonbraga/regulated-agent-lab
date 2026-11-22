---
status: accepted
date: 2026-11-21
---

# The agent pays from a wallet with a scoped, single-use token

ADR-7 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

Some purchases should finish without the customer on a payment page (a repeat
booking the customer delegated). Agent platforms do this by vaulting the card themselves and
handing the merchant a scoped credential, not the card; the old fixed test card showed the
charge but none of those controls.

## Decision Outcome

A third institution, the wallet (`backend/src/regulated_agent_lab/services/wallet/`, its own `wallet.db`).
The card is saved on the processor's hosted form (`mode=setup`); the wallet keeps references
only and a mandate per user. The human picks the wallet per run; the choice and the payer are
signed into the approval token, and the gate issues `wallet.issue_payment_token` only with
that token. The token is single use, bound to the exact amount, currency, merchant, run and 15
minutes; the payments institution redeems it at the wallet with its own identity and charges
off-session with the run id as idempotency key. SCA, over-limit, no card or a decline fall
back to the hosted Checkout.

### Consequences

A third MCP server and a service-to-service call (payments to wallet). The
lab's wallet and payments share one processor account, so the wallet can hand over processor
ids; a real scheme would issue a network or shared payment token instead (next step).
