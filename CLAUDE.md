# CLAUDE.md

## Run

- Layout: `backend/` (uv project, package `backend/src/regulated_agent_lab`, config in
  `backend/config`), `frontend/` (Vite UI), `infra/` (prod deploy), `scripts/`, `docs/` (ADRs in
  `docs/adr/`). The root `Makefile` wraps the commands below.
- Install: `make install` (= `cd backend && uv sync` + `cd frontend && npm ci`; Python 3.12,
  pinned in `backend/.python-version`).
- Tests: `cd backend && LLM_PROVIDER=mock AGENT_TOKEN_SECRET=test uv run pytest -v` (or `make test`,
  which also runs the frontend tests). `backend/tests/conftest.py` sets these defaults too; tests
  never need a GPU, network or API keys. Unit tests in `backend/tests/unit`, everything that
  goes through the API, the graph, an MCP client or a subprocess in `backend/tests/integration`.
- Lint: `cd backend && uv run ruff check . && uv run ruff format --check .` (or `make lint`).
- Dev: `make dev-api` (uvicorn `regulated_agent_lab.api.server:app` on :8000), `make dev-ui` (:5173).

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
  real operator or station-operator names. `backend/tests/integration/test_repo_hygiene.py` enforces the names list.
- No secrets committed: only `.env.example`; `.env` and `backend/var/` are gitignored.
- LLM provider is set in `backend/config/llm.yaml` (default `vllm`); env `LLM_PROVIDER` overrides it.
