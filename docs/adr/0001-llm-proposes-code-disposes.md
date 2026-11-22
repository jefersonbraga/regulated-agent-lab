---
status: accepted
date: 2026-11-21
---

# LLM proposes, code disposes

ADR-1 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

LLM output is non-deterministic and can be steered by anything it reads.

## Decision Outcome

The LLM only returns Pydantic-validated `Intent` (trip extraction) and
`Rationale` (display text) through `LLM.extract`. The intent may carry a departure-time window
per leg (`outbound_window`, `return_window`); its bounds are validated in code. Code does the
routing, the pricing (every train in the window per leg, earliest first and capped at 12, default
earliest 07:00, each train's cheapest 2nd-class fare, saver or standard), the recommendation
(cheapest per leg, ties to the shortest journey, then the earliest), totals and payment.

### Consequences

Trajectories are deterministic for a given intent, so tests can assert
exact tool sequences. A bad extraction (return before departure, unknown station, empty
message, non-trip request) ends in `needs_clarification` with zero tool calls. What we give up:
the agent cannot improvise beyond the coded flow.
