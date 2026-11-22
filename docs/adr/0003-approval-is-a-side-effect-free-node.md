---
status: accepted
date: 2026-11-21
---

# Approval is a side-effect-free node

ADR-3 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

On resume, LangGraph re-executes the interrupted node from its first line.

## Decision Outcome

`approval` does nothing before `interrupt()` except build the payload (the
recommended quote and its hash, the offer with every per-leg alternative and the window used,
and the offer's `approve_before`). Seat holds happen later in `booking_hold`, for the chosen
fares only, and the charge after them in `booking_pay`. The decision names one offered fare
per leg (`fare_ids`, omitted means the recommended one) and carries the hash of the quote the
human saw (`POST /runs/{id}/quote` returns it). The node recomputes that quote from the offer
alone: a fare outside the offer, not exactly one per leg, a different hash, or a recommended
quote edited in state after display, rejects the run with the flag `quote_mismatch`
(`backend/tests/integration/test_graph.py::test_selection_outside_the_offer_is_rejected_and_never_charged`,
`::test_approval_node_recomputes_quote_hash`). `booking_hold` and `booking_pay` recompute the
hash again before holding and charging
(`backend/tests/integration/test_graph.py::test_booking_hold_refuses_a_quote_the_human_did_not_approve`,
`::test_booking_pay_recomputes_quote_hash`).

### Consequences

A rejected run never holds a seat. Double approval is absorbed three times
over: the API's per-run lock (409), the graph routing (holds and payment already set), and the
payments `idempotency_key = run_id`.
