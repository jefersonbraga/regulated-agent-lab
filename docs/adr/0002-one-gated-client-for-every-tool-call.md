---
status: accepted
date: 2026-11-21
---

# One gated client for every tool call

ADR-2 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

Controls spread across agents get skipped.

## Decision Outcome

All MCP calls go through `GatedToolClient.call` in a fixed order: policy check,
audit `tool_call`, token injection, MCP call, injection guard, audit `tool_result`. Agents
never see or pass the payments token. The gate drops any `agent_token` the caller supplies.

### Consequences

One place to review and test. A denied call is audited as `policy_denied`
and never reaches the server. The cost: a node that imported an MCP client directly would
bypass the gate. Code review and the tests are the only things that stop that.
