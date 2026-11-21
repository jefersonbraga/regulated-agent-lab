"""A crashed stdio MCP server is restarted instead of failing every later run."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from agents.tools import SERVER_MODULES, GatedToolClient
from controls.audit_log import AuditLog
from controls.policy import ToolPolicy

SEARCH = "travel_search.search_trains"
HOLD = "travel_search.hold_seat"
REPO_ROOT = Path(__file__).resolve().parent.parent
DYING_SERVER = Path(__file__).resolve().parent / "servers" / "dying_travel_search.py"
SEARCH_ARGS = {"origin": "Zurich HB", "destination": "Bern", "date": "2026-12-01"}


def _server_pids(server: str) -> set[int]:
    out = subprocess.run(
        ["pgrep", "-P", str(os.getpid()), "-f", SERVER_MODULES[server]],
        capture_output=True,
        text=True,
    ).stdout
    return {int(pid) for pid in out.split()}


async def _kill_server(server: str) -> None:
    pids = _server_pids(server)
    assert pids, "stdio server is not running"
    for pid in pids:
        os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        if not _server_pids(server):
            # Let the client's reader task notice the closed pipe.
            await asyncio.sleep(0.2)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("stdio server did not exit")


@pytest.fixture
async def gated(tmp_path):
    client = GatedToolClient.stdio(ToolPolicy.from_yaml(), AuditLog(tmp_path / "audit.jsonl"))
    yield client
    await client.aclose()


async def test_read_tool_survives_a_crashed_stdio_server(gated):
    first = await gated.call("run-1", "planner", SEARCH, SEARCH_ARGS)
    before = _server_pids("travel_search")

    await _kill_server("travel_search")
    second = await gated.call("run-2", "planner", SEARCH, SEARCH_ARGS)

    assert second.sanitized == first.sanitized
    after = _server_pids("travel_search")
    assert after and not (after & before)  # a fresh subprocess
    events = [e.event for e in gated._audit.read("run-2")]
    assert events == ["tool_call", "transport_reset", "tool_result"]
    assert gated._audit.verify().ok


async def test_non_idempotent_tool_is_not_retried_but_next_call_reconnects(gated):
    found = await gated.call("run-3", "planner", SEARCH, SEARCH_ARGS)
    train_id = found.sanitized[0]["train_id"]
    fares = await gated.call("run-3", "pricing", "travel_search.get_fares", {"train_id": train_id})
    hold_args = {"fare_id": fares.sanitized[0]["fare_id"], "passenger_ref": "pax-run-3"}

    await _kill_server("travel_search")
    with pytest.raises(Exception) as exc_info:
        await gated.call("run-3", "booking", HOLD, hold_args)
    assert not isinstance(exc_info.value, ToolError)
    events = [(e.event, e.data.get("tool")) for e in gated._audit.read("run-3")]
    assert events[-3:] == [("tool_call", HOLD), ("transport_reset", HOLD), ("tool_error", HOLD)]
    assert events.count(("tool_call", HOLD)) == 1  # not retried: a hold may have reached the server

    # The client was rebuilt: the next run's call works without restarting the process.
    again = await gated.call("run-4", "booking", HOLD, {**hold_args, "passenger_ref": "pax-run-4"})
    assert again.sanitized["hold_id"]


def _dying_gate(
    tmp_path, die_on: str = "", *, stall_on: str = "", call_timeout_s: float | None = None
) -> GatedToolClient:
    """Gate whose travel_search server exits (or stalls) while the first such call is pending."""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "DIE_ON_TOOL": die_on,
        "STALL_ON_TOOL": stall_on,
        "DIE_MARKER": str(tmp_path / "died"),
        "FASTMCP_SHOW_CLI_BANNER": "false",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }

    def factory(server: str) -> Client:
        assert server == "travel_search"
        return Client(
            StdioTransport(
                command=sys.executable, args=[str(DYING_SERVER)], env=env, cwd=str(REPO_ROOT)
            )
        )

    return GatedToolClient(
        {"travel_search": factory("travel_search")},
        ToolPolicy.from_yaml(),
        AuditLog(tmp_path / "audit.jsonl"),
        client_factory=factory,
        call_timeout_s=call_timeout_s,
    )


async def test_read_tool_retried_when_server_dies_mid_call(tmp_path):
    # The pipe closes while the request is pending: the MCP session raises McpError
    # (CONNECTION_CLOSED). That is a transport failure, not an answer from the server.
    gated = _dying_gate(tmp_path, "search_trains")
    try:
        result = await asyncio.wait_for(gated.call("run-5", "planner", SEARCH, SEARCH_ARGS), 30)
    finally:
        await gated.aclose()

    assert (tmp_path / "died").exists()
    assert result.sanitized and result.sanitized[0]["train_id"]
    events = [e.event for e in gated._audit.read("run-5")]
    assert events == ["tool_call", "transport_reset", "tool_result"]
    (reset,) = [e for e in gated._audit.read("run-5") if e.event == "transport_reset"]
    assert reset.data == {"tool": SEARCH, "error_type": "McpError"}


async def test_hold_not_retried_when_server_dies_mid_call(tmp_path):
    gated = _dying_gate(tmp_path, "hold_seat")
    from datetime import date

    from mcp_servers.travel_search import data

    train_id = data.search_trains("Zurich HB", "Bern", date(2026, 12, 1))[0]["train_id"]
    hold_args = {"fare_id": data.get_fares(train_id)[0]["fare_id"], "passenger_ref": "pax-run-6"}
    try:
        with pytest.raises(Exception) as exc_info:
            await asyncio.wait_for(gated.call("run-6", "booking", HOLD, hold_args), 30)
        assert not isinstance(exc_info.value, ToolError)
        events = [(e.event, e.data.get("tool")) for e in gated._audit.read("run-6")]
        assert events == [("tool_call", HOLD), ("transport_reset", HOLD), ("tool_error", HOLD)]

        # The client was rebuilt: the next call reaches a fresh server.
        found = await asyncio.wait_for(gated.call("run-7", "planner", SEARCH, SEARCH_ARGS), 30)
        assert found.sanitized
        assert [e.event for e in gated._audit.read("run-7")] == ["tool_call", "tool_result"]
    finally:
        await gated.aclose()


async def test_read_tool_retried_when_server_stalls_past_the_timeout(tmp_path):
    # The server is alive but never answers: the call must not hang the run forever.
    gated = _dying_gate(tmp_path, stall_on="search_trains", call_timeout_s=2)
    try:
        result = await asyncio.wait_for(gated.call("run-8", "planner", SEARCH, SEARCH_ARGS), 30)
    finally:
        await gated.aclose()

    assert (tmp_path / "died").read_text() == "stalled"
    assert result.sanitized and result.sanitized[0]["train_id"]
    events = [e.event for e in gated._audit.read("run-8")]
    assert events == ["tool_call", "transport_reset", "tool_result"]
    assert gated._audit.verify().ok


async def test_hold_not_resent_when_server_stalls_past_the_timeout(tmp_path):
    gated = _dying_gate(tmp_path, stall_on="hold_seat", call_timeout_s=2)
    from datetime import date

    from mcp_servers.travel_search import data

    train_id = data.search_trains("Zurich HB", "Bern", date(2026, 12, 1))[0]["train_id"]
    hold_args = {"fare_id": data.get_fares(train_id)[0]["fare_id"], "passenger_ref": "pax-run-9"}
    try:
        with pytest.raises(Exception) as exc_info:
            await asyncio.wait_for(gated.call("run-9", "booking", HOLD, hold_args), 30)
        assert not isinstance(exc_info.value, (ToolError, asyncio.TimeoutError))
        events = [(e.event, e.data.get("tool")) for e in gated._audit.read("run-9")]
        assert events == [("tool_call", HOLD), ("transport_reset", HOLD), ("tool_error", HOLD)]

        found = await asyncio.wait_for(gated.call("run-10", "planner", SEARCH, SEARCH_ARGS), 30)
        assert found.sanitized
    finally:
        await gated.aclose()


def test_call_timeout_defaults_from_env(monkeypatch):
    monkeypatch.setenv("MCP_CALL_TIMEOUT_S", "7.5")
    gated = GatedToolClient({}, ToolPolicy.from_yaml(), AuditLog(Path("/nonexistent/a.jsonl")))
    assert gated._call_timeout_s == 7.5
    monkeypatch.delenv("MCP_CALL_TIMEOUT_S")
    gated = GatedToolClient({}, ToolPolicy.from_yaml(), AuditLog(Path("/nonexistent/a.jsonl")))
    assert gated._call_timeout_s == 10.0
