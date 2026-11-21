"""Terminal demo: request -> quote -> human approval -> payment -> audit verify.

    python -m agents.cli "Train from Zurich to Bern on 1 December, back the next day"

With ``LLM_PROVIDER=mock`` the mock answers from ``agents/fixtures/mock_roundtrip.json``
unless ``--mock-fixtures`` (or env ``MOCK_FIXTURES``) names another file.
MCP servers run as stdio subprocesses. Simulated data only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langgraph.types import Command

from agents.config import load_llm_config
from agents.graph import build_graph, initial_state
from agents.llm import LLMConfigError, build_llm, load_mock_fixtures
from agents.tools import GatedToolClient
from controls.audit_log import AuditLog
from controls.policy import ToolPolicy

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _chf(cents: int) -> str:
    return f"CHF {cents // 100}.{cents % 100:02d}"


def _print_quote(quote: dict) -> None:
    print("\nQuote (Demo Rail, simulated):")
    for o in quote["options"]:
        print(
            f"  {o['leg']:<8} {o['train_id']}  {o['depart'][11:16]} -> {o['arrive'][11:16]}"
            f"  {_chf(o['price_cents'])}"
        )
    print(f"  total    {_chf(quote['total_cents'])}")
    print(f"  why      {quote['rationale']}")
    print(f"  hash     {quote['quote_hash'][:16]}...")


async def _pay_on_processor_page(graph, config: dict, run_id: str, state: dict) -> dict:
    """Hosted Checkout: the customer pays in a browser; then the processor's record is read."""
    from mcp_servers.payments_sandbox import checkout
    from mcp_servers.payments_sandbox import server as payments

    page = state["__interrupt__"][0].value
    print("\nPay on the processor's page (test card 4242 4242 4242 4242):")
    print(f"  {page['checkout_url']}")
    try:
        input("Press Enter once paid (Ctrl-C to leave the run waiting) ")
    except (EOFError, KeyboardInterrupt):
        print()
        return state
    outcome = await asyncio.to_thread(
        checkout.refresh, run_id, transport=payments.PROCESSOR_TRANSPORT
    )
    if outcome.state not in checkout.TERMINAL:
        print(f"checkout {outcome.state}: not paid yet")
        return state
    return await graph.ainvoke(Command(resume={"source": "refresh"}), config)


async def run(message: str, fixtures: dict | None, approver: str, audit_path: Path) -> int:
    cfg = load_llm_config(_REPO_ROOT / "config" / "llm.yaml")
    llm = build_llm(cfg, fixtures)
    audit = AuditLog(audit_path)
    tools = GatedToolClient.stdio(ToolPolicy.from_yaml(), audit)
    graph = build_graph(llm, tools)
    run_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": run_id}}
    print(f"run_id {run_id}  provider {cfg.provider}  model {cfg.model}")

    state = await graph.ainvoke(initial_state(run_id, message), config)
    if "__interrupt__" in state:
        payload = state["__interrupt__"][0].value
        _print_quote(payload["quote"])
        try:
            answer = input("\nApprove? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # No answer (stdin closed, not interactive, or Ctrl-C): the default N applies.
            print()
            answer = ""
        decision = {
            "approved": answer in {"y", "yes"},
            "approver": approver,
            "quote_hash": payload["quote_hash"],
        }
        state = await graph.ainvoke(Command(resume=decision), config)
    if state.get("status") == "awaiting_payment" and "__interrupt__" in state:
        state = await _pay_on_processor_page(graph, config, run_id, state)

    print(f"\nstatus   {state['status']}")
    if state.get("payment"):
        p = state["payment"]
        print(f"payment  {p['payment_id']} {p['status']} {_chf(p['amount_cents'])}")
    if state.get("flags"):
        print(f"flags    {', '.join(state['flags'])}")
    print("tools    " + " -> ".join(s for s in state["trajectory"] if "." in s))
    result = audit.verify()
    print(f"audit    {audit_path} verify={result.model_dump_json()}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m agents.cli", description=__doc__)
    parser.add_argument("message")
    parser.add_argument(
        "--mock-fixtures",
        default=os.environ.get("MOCK_FIXTURES"),
        help="JSON file of MockLLM fixtures keyed by schema name (LLM_PROVIDER=mock)",
    )
    parser.add_argument("--approver", default=os.environ.get("APPROVER", "cli-user"))
    args = parser.parse_args(argv)

    if not os.environ.get("AGENT_TOKEN_SECRET"):
        # Fail closed: no default secret. Without it every gated call would be refused.
        print(
            "error: AGENT_TOKEN_SECRET is not set; export it (e.g. AGENT_TOKEN_SECRET=demo-only) "
            "or run `cp .env.example .env` and set it there",
            file=sys.stderr,
        )
        return 2

    cfg = load_llm_config(_REPO_ROOT / "config" / "llm.yaml")
    fixtures = load_mock_fixtures(cfg.provider, args.mock_fixtures)
    audit_path = Path(os.environ.get("AUDIT_LOG_PATH", "var/audit.jsonl"))
    try:
        return asyncio.run(run(args.message, fixtures, args.approver, audit_path))
    except LLMConfigError as exc:  # raised by build_llm before any request is sent
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
