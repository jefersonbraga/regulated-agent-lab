"""Scenario eval harness: run one ``scenarios.jsonl`` row through the full graph.

Default runs use ``MockLLM`` with the row's fixtures and in-memory MCP servers, so they need
no GPU, network or API keys. ``pytest -m live`` reruns the rows against the configured real
model and prints a pass-rate. The live provider is ``LIVE_LLM_PROVIDER`` if set, else the
caller's own ``LLM_PROVIDER`` (unless it is ``mock``), else ``provider`` in ``config/llm.yaml``
(``vllm`` when that is ``mock``). A live row is skipped unless that provider is reachable:
``VLLM_BASE_URL`` for vllm, ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY`` for the cloud ones.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

# The caller's own provider choice, captured before the mock default below hides it.
_USER_LLM_PROVIDER = os.environ.get("LLM_PROVIDER")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("AGENT_TOKEN_SECRET", "test")
# Scenarios assert exact trajectories over the simulated schedule and ledger: never the real
# timetable or the test-mode card processor, even if the caller's shell enables them.
for _real in (
    "TRAVEL_SEARCH_SOURCE",
    "PAYMENTS_PROVIDER",
    "STRIPE_TEST_SECRET_KEY",
    "TIMETABLE_API_URL",
):
    os.environ.pop(_real, None)
# Persistence never touches the real var/: one throwaway directory per test session (stdio
# server subprocesses inherit these). Tests that need their own file pass a tmp_path.
_DB_DIR = tempfile.mkdtemp(prefix="lab-test-db-")
atexit.register(shutil.rmtree, _DB_DIR, ignore_errors=True)
os.environ["LAB_DB_PATH"] = os.path.join(_DB_DIR, "lab.db")
os.environ["PAYMENTS_DB_PATH"] = os.path.join(_DB_DIR, "payments.db")
os.environ["WALLET_DB_PATH"] = os.path.join(_DB_DIR, "wallet.db")

from agents.config import LLMConfig, load_llm_config  # noqa: E402
from agents.graph import build_graph, initial_state  # noqa: E402
from agents.llm import LLM, MockLLM, build_llm  # noqa: E402
from agents.state import (  # noqa: E402
    SELECTED_RATIONALE,
    InvalidSelection,
    quote_for_selection,
)
from agents.tools import GatedToolClient, make_clients  # noqa: E402
from controls.audit_log import AuditLog  # noqa: E402
from controls.policy import ToolPolicy  # noqa: E402
from mcp_servers.payments_sandbox.server import reset_ledger  # noqa: E402

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
SCENARIO_ENV_KEYS = ("TRAVEL_SEARCH_POISONED",)
APPROVER = "eval-approver"


def load_scenarios(path: Path = EVALS_DIR / "scenarios.jsonl") -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


SCENARIOS: list[dict] = load_scenarios()


def scenario_by_id(scenario_id: str) -> dict:
    for row in SCENARIOS:
        if row["id"] == scenario_id:
            return row
    raise KeyError(scenario_id)


def tool_steps(state: dict) -> list[str]:
    """The ``"server.tool"`` entries of a trajectory (node names have no dot)."""
    return [step for step in state["trajectory"] if "." in step]


class SpyLLM(MockLLM):
    """MockLLM that records every prompt it receives: (schema name, system, user)."""

    def __init__(self, fixtures: dict[str, dict]) -> None:
        super().__init__(fixtures)
        self.calls: list[tuple[str, str, str]] = []

    async def extract(self, schema, system, user):
        self.calls.append((schema.__name__, system, user))
        return await super().extract(schema, system, user)


@dataclass
class ScenarioEnv:
    """Shared per-test environment: one audit log across every run in the test."""

    audit: AuditLog
    monkeypatch: pytest.MonkeyPatch
    runs: int = 0


@dataclass
class Outcome:
    state: dict
    audit: AuditLog
    paused: dict | None = None
    interrupted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def scenario_env(tmp_path, monkeypatch) -> ScenarioEnv:
    reset_ledger()
    yield ScenarioEnv(audit=AuditLog(tmp_path / "audit.jsonl"), monkeypatch=monkeypatch)
    reset_ledger()


def _apply_env(row: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    env = row.get("env") or {}
    for key in SCENARIO_ENV_KEYS:
        if key in env:
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)


def approval_decision(row: dict, state: dict) -> dict:
    """The human decision for a paused run, from the row's ``approve``.

    ``approve`` is a bool (approve or reject the recommended quote) or an object
    ``{"approved": bool, "fare_ids": [...]}``: one offered fare id per leg. Like the UI, the
    harness binds the decision to the hash code computes for that selection (the quote
    endpoint); a selection outside the offer has no such hash, so it keeps the displayed one.
    """
    approve = row["approve"]
    spec = approve if isinstance(approve, dict) else {"approved": bool(approve)}
    payload = state["__interrupt__"][0].value
    decision = {
        "approved": bool(spec.get("approved", False)),
        "approver": APPROVER,
        "quote_hash": payload["quote_hash"],
    }
    if "fare_ids" in spec:
        decision["fare_ids"] = spec["fare_ids"]
        try:
            selected = quote_for_selection(state["offer"], spec["fare_ids"], SELECTED_RATIONALE)
        except InvalidSelection:
            pass
        else:
            decision["quote_hash"] = selected.quote_hash
    return decision


async def run_scenario(row: dict, env: ScenarioEnv, llm: LLM | None = None) -> Outcome:
    """Run a row end to end; resume at the approval interrupt when ``approve`` is set."""
    reset_ledger()
    _apply_env(row, env.monkeypatch)
    env.runs += 1
    run_id = f"eval-{row['id']}-{env.runs}"
    config = {"configurable": {"thread_id": run_id}}

    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), env.audit)
    graph = build_graph(llm or MockLLM(row["fixtures"]), tools)

    state = await graph.ainvoke(initial_state(run_id, row["user_message"]), config)
    interrupted = "__interrupt__" in state
    paused = None
    if interrupted and row.get("approve") is not None:
        paused = state
        state = await graph.ainvoke(Command(resume=approval_decision(row, state)), config)
    return Outcome(state=state, audit=env.audit, paused=paused, interrupted=interrupted)


# --- live evals: real model, measured pass-rate -------------------------------------------


_LIVE_CONFIG_PATH = REPO_ROOT / "config" / "llm.yaml"
_LIVE_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _live_config(
    path: Path = _LIVE_CONFIG_PATH, user_provider: str | None = _USER_LLM_PROVIDER
) -> LLMConfig:
    """The real model to rerun the rows against; never the mock forced for plain runs."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("LLM_PROVIDER", raising=False)  # read the YAML provider itself
        cfg = load_llm_config(path)
    provider = os.environ.get("LIVE_LLM_PROVIDER") or (
        user_provider if user_provider and user_provider != "mock" else cfg.provider
    )
    if provider == "mock":
        provider = "vllm"
    return LLMConfig.model_validate({**cfg.model_dump(), "provider": provider})


def _live_skip_reason(cfg: LLMConfig) -> str | None:
    if cfg.provider == "vllm" and not cfg.base_url:
        return "live evals on vllm need VLLM_BASE_URL (on-prem OpenAI-compatible endpoint)"
    key = _LIVE_KEYS.get(cfg.provider)
    if key is not None and not os.environ.get(key):
        return f"live evals on {cfg.provider} need {key}"
    return None


@pytest.fixture
def live_llm() -> LLM:
    cfg = _live_config()
    reason = _live_skip_reason(cfg)
    if reason is not None:
        pytest.skip(reason)
    return build_llm(cfg)


_LIVE_RESULTS: dict[str, str] = {}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: rerun scenario evals against the configured real model"
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "test_scenario_live" not in report.nodeid:
        return
    if report.skipped:
        _LIVE_RESULTS.setdefault(report.nodeid, "skipped")
    elif report.when == "call":
        _LIVE_RESULTS[report.nodeid] = "passed" if report.passed else "failed"
    elif report.failed:
        _LIVE_RESULTS[report.nodeid] = "failed"


def pytest_terminal_summary(terminalreporter) -> None:
    ran = [v for v in _LIVE_RESULTS.values() if v != "skipped"]
    if not ran:
        return
    passed = ran.count("passed")
    terminalreporter.write_sep("=", "live eval pass-rate")
    terminalreporter.write_line(
        f"{passed}/{len(ran)} scenarios passed against the live model "
        f"({100 * passed / len(ran):.0f}%)"
    )
