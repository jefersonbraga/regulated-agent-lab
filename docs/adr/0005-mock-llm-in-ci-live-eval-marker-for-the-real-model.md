---
status: accepted
date: 2026-11-21
---

# Mock LLM in CI, live eval marker for the real model

ADR-5 of [regulated-agent-lab](../architecture.md#decisions). Format: [MADR](https://adr.github.io/madr/).

## Context and Problem Statement

CI must not depend on a GPU, a LAN endpoint or API keys. Real models still drift.

## Decision Outcome

`MockLLM` returns fixtures per schema. `backend/evals/scenarios.jsonl` drives the full
graph with in-memory MCP servers and asserts status, the exact tool trajectory, forbidden
tools and amounts, never free text. `pytest -m live` reruns the same rows against the
configured model (`provider` in `backend/config/llm.yaml`, or `LLM_PROVIDER` / `LIVE_LLM_PROVIDER` when
set; a `mock` YAML falls back to `vllm`) and prints a pass-rate. A row is skipped unless that
provider is reachable: `VLLM_BASE_URL` for vllm, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
for the cloud providers (`backend/tests/integration/test_live_opt_in.py`).

### Consequences

CI tests the control logic deterministically. Model quality is a separate,
measured number, not a hidden flake.
