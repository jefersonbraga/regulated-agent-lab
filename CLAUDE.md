# CLAUDE.md

## Run

- Install: `uv sync` (Python 3.12, pinned in `.python-version`).
- Tests: `LLM_PROVIDER=mock AGENT_TOKEN_SECRET=test uv run pytest -v`
  (`tests/conftest.py` sets these defaults too; tests never need a GPU, network or API keys).
- Lint: `uv run ruff check . && uv run ruff format --check .`

## Design rule: LLM proposes, code disposes

The LLM only produces typed structured output (intent, trip request, rationale).
Tool calls, routing, selection and payment are deterministic code behind the policy gate.
Every tool call goes through the single gated client (policy, injection guard, redaction, audit log).

## Constraints

- English everywhere. Fictional operator `Demo Rail`; stations `Zurich HB`, `Bern`, `Geneva`, `Basel`.
- Opt-in real parts, for runs only: `TRAVEL_SEARCH_SOURCE=live` (public timetable API, queried by
  numeric station id) and `PAYMENTS_PROVIDER=stripe_test` (card processor in test mode, `sk_test_`/`rk_test_`
  keys only). Real names may appear in runtime data, never in the repo. Fares and seat holds stay
  simulated. Tests and evals strip these variables and never touch the network.
- Currency `CHF`, amounts as integer cents, never float.
- Forbidden in the repo: third-party client, employer or partner code, data, names or branding;
  real operator or station-operator names. `tests/test_repo_hygiene.py` enforces the names list.
- No secrets committed: only `.env.example`; `.env` and `var/` are gitignored.
- LLM provider is set in `config/llm.yaml` (default `vllm`); env `LLM_PROVIDER` overrides it.
