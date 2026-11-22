# regulated-agent-lab — Spec

> **Historical document.** This is the original specification the lab was built from. The flow
> has since changed (seats are held after approval, the human picks the trains and the payment
> model, payments go through a processor in test mode). The current design is in the
> [README](../README.md) and [architecture.md](architecture.md).

Portfolio lab showing a supervised multi-agent booking pattern for regulated sectors, in small:
a LangGraph supervisor, domain agents, tools over MCP, human approval before payment, and
regulated-sector controls.

## Demo flow

1. User asks: "Train from Zurich to Bern tomorrow morning, back the next day, 1 adult."
2. Supervisor routes: **planner** builds the itinerary → **pricing** compares options →
   **booking** holds seats and prepares the purchase.
3. Before payment the graph stops (LangGraph `interrupt`). The UI shows an approval card.
   No approval → no payment.
4. Every step is written to an audit log the UI can show and anyone can verify later.
5. All data and payments are simulated. Fictional operator "Demo Rail". No real customer data or money.

## Controls (each one has a test)

| Control | Proof |
|---|---|
| Append-only audit log, SHA-256 hash chain | Tampering with any line makes `verify` fail |
| Per-agent tool permissions in `backend/config/tool_policy.yaml` | Pricing agent calling `payments.charge` raises `PolicyViolation` |
| Prompt-injection defence: tool output is data, never instructions | Poisoned search result does not change the trajectory and is flagged in the audit log |
| PII redaction in logs (name, email, document number) | No raw PII in the audit file after a run |
| Cross-institution boundary: payments is a separate institution and accepts only a scoped agent token | Charge without a valid booking-agent token is refused by the server |
| Charge amount bound to the approved quote | Charge with an amount different from the approved quote is refused |
| Evals for non-deterministic systems | Assert tool trajectory and structured outputs, never free text |

## Models

- Default: on-prem vLLM on DGX Spark (OpenAI-compatible endpoint), `Qwen3.6-35B-A3B-NVFP4`.
- Switch to Anthropic or OpenAI with one line in `backend/config/llm.yaml`.
- CI uses a mock model: tests depend on neither the DGX nor API keys.
- The DGX is never exposed to the internet. The public demo is a video.

## Design rule

**LLM proposes, code disposes.** The LLM only produces typed structured output (intent, trip request,
rationale). Tool calls, routing, selection and payment are deterministic code behind the policy gate.
That makes trajectories testable and auditable.

## Stack

Python 3.12 (uv, LangGraph, FastMCP, FastAPI, Pydantic v2, pytest, ruff) · TypeScript (Vite, React, Vitest).

## Constraints

- No third-party client or employer code, data, names or branding; no real operator names.
- No keys in the repo (`.env.example` only).
- English everywhere in the repo.
- 3-day budget: Day 1 graph + MCP + interrupt end-to-end on the terminal. Day 2 controls + evals + CI. Day 3 UI + docs + video.
