"""stdio MCP servers start quietly: no FastMCP banner and no PyPI update check."""

import importlib.util
import runpy

import pytest
from fastmcp import FastMCP

from regulated_agent_lab.agents.tools import SERVER_MODULES, make_clients


@pytest.mark.parametrize("module", sorted(SERVER_MODULES.values()))
def test_server_main_disables_banner(module, monkeypatch):
    calls = []
    monkeypatch.setattr(FastMCP, "run", lambda self, *a, **kw: calls.append(kw))
    # run_path executes a fresh copy as __main__ without re-importing the cached module.
    runpy.run_path(importlib.util.find_spec(module).origin, run_name="__main__")
    assert calls == [{"show_banner": False}]


def test_stdio_clients_disable_update_check():
    for client in make_clients(in_memory=False).values():
        env = client.transport.env
        assert env["FASTMCP_CHECK_FOR_UPDATES"] == "off"
        assert env["FASTMCP_SHOW_CLI_BANNER"] == "false"
