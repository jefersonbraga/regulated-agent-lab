---
status: accepted
date: 2026-11-21
---

# Scoped tokens across institutions

ADR-4 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

Payments belong to a different institution, and that institution should not
trust the agent platform's claims.

## Decision Outcome

For each `payments.*` and `wallet.*` call the gate issues a short-lived HMAC
token bound to `(agent, server, exp)` (`backend/src/regulated_agent_lab/controls/identity.py`, TTL 300 s). The payments server
accepts only `agent == "booking"` for `server == "payments"`; the wallet accepts `booking` for
`issue_payment_token` and only the payments institution's own identity (`payments`, for
server `wallet`) for `redeem_payment_token`. Payments also requires a non-empty
`approval_ref` and enforces `idempotency_key`.

### Consequences

A pricing-agent token or an expired one is refused at the server, even if
the policy gate were bypassed. Limits: the key is a shared symmetric secret, and the token is
not single-use (see the threat model).
