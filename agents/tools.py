"""GatedToolClient: the single choke point for every agent tool call.

Order per call: approval proof check -> tool policy -> audit `tool_call`
-> scoped token injection (payments only) -> MCP call -> injection guard
-> audit `tool_result` (+ `injection_flagged`). If that last audit write fails, the call raises
``UnauditedToolResult`` carrying the result, so a side effect that happened is never lost.
If the MCP transport fails (e.g. a stdio server subprocess died, also mid-call, which the MCP
session reports as ``McpError`` with code ``CONNECTION_CLOSED``), the client for that server is
rebuilt and `transport_reset` is audited, so later calls start a fresh server. Only read-only
tools are retried within the same call; a hold or a charge is never sent twice.
Every MCP request has a deadline (``MCP_CALL_TIMEOUT_S``, default 10 s). A server that is alive
but never answers is treated like a dead one: ``tool_error`` and ``transport_reset`` are
audited and the client is rebuilt, so a stalled server cannot hang a run (or the API's per-run
lock) forever.
Agents never hold or pass the payments token; the gate issues it per call.
Each step is also emitted as a live run event (``tool_call``, ``tool_result``, ``tool_error``,
``policy_denied``) on LangGraph's custom stream when called inside a graph run: tool name,
agent and flags only, never args or results (they can carry PII).
Approval is never a caller-supplied flag: an approval-gated tool runs only with a signed
approval token (issued by the graph's approval node) that covers this run, the amount and
currency being charged, and the ``approval_ref`` sent to the server. ``wallet.issue_payment_token``
also needs the human to have chosen the wallet payment model, for the payer named in the token.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from langgraph.config import get_stream_writer
from mcp import McpError
from mcp.types import CONNECTION_CLOSED

from controls.audit_log import AuditLog
from controls.identity import issue_token, verify_approval_token
from controls.injection_guard import GuardResult, inspect_tool_output
from controls.policy import PolicyViolation, ToolPolicy

_REPO_ROOT = Path(__file__).resolve().parent.parent

# server name (as used in "server.tool") -> python module of the MCP server
SERVER_MODULES: dict[str, str] = {
    "travel_search": "mcp_servers.travel_search.server",
    "payments": "mcp_servers.payments_sandbox.server",
    "wallet": "mcp_servers.wallet.server",
}

TOKEN_SERVERS = frozenset({"payments", "wallet"})
WALLET_TOKEN_TOOL = "wallet.issue_payment_token"

# Tools with no side effects: safe to resend once after a transport failure.
READ_ONLY_TOOLS = frozenset(
    {"travel_search.search_trains", "travel_search.get_fares", "payments.checkout_status"}
)

ClientFactory = Callable[[str], Client]

DEFAULT_CALL_TIMEOUT_S = 10.0

# Codes the MCP client session raises itself (not answers from the server): the pipe closed
# with a request pending, or no response arrived before the request deadline
# (``mcp.shared.session.BaseSession.send_request`` uses HTTP 408 for that).
_TRANSPORT_ERROR_CODES = frozenset({CONNECTION_CLOSED, httpx.codes.REQUEST_TIMEOUT})


def _call_timeout_from_env() -> float:
    raw = os.environ.get("MCP_CALL_TIMEOUT_S")
    if not raw:
        return DEFAULT_CALL_TIMEOUT_S
    value = float(raw)
    if value <= 0:
        raise ValueError("MCP_CALL_TIMEOUT_S must be a positive number of seconds")
    return value


def emit_run_event(event: dict) -> None:
    """Write ``event`` to the graph's custom stream; a no-op outside a graph run.

    Callers pass only identifiers and flags (tool, agent, node), never args or tool output.
    """
    try:
        writer = get_stream_writer()
    except (RuntimeError, KeyError):  # not inside a graph node (CLI tools, direct tests)
        return
    writer(event)


class UnauditedToolResult(Exception):
    """The tool ran (its side effect may have happened) but its outcome could not be audited.

    Carries the guarded result so the caller can still surface it (e.g. a captured payment)
    instead of losing it; the audit failure is the ``__cause__``.
    """

    def __init__(self, tool: str, guard: GuardResult) -> None:
        super().__init__(f"{tool} result could not be audited")
        self.tool = tool
        self.guard = guard


def _result_payload(result: Any) -> Any:
    """Plain JSON of a tool result; list/scalar returns are wrapped as {"result": ...}."""
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    if content is not None:
        return content
    return result.data


class GatedToolClient:
    def __init__(
        self,
        clients: dict[str, Client],
        policy: ToolPolicy,
        audit: AuditLog,
        *,
        client_factory: ClientFactory | None = None,
        call_timeout_s: float | None = None,
    ) -> None:
        self._clients = clients
        # Deadline per MCP request; None: from MCP_CALL_TIMEOUT_S (default 10 s).
        self._call_timeout_s = (
            call_timeout_s if call_timeout_s is not None else _call_timeout_from_env()
        )
        self._policy = policy
        self._audit = audit
        # Rebuilds the client of a server whose transport failed; None: never rebuild.
        self._client_factory = client_factory

    @classmethod
    def stdio(cls, policy: ToolPolicy, audit: AuditLog) -> GatedToolClient:
        """Gate over stdio subprocess servers (CLI/API) that restarts a crashed server."""
        return cls(
            make_clients(in_memory=False),
            policy,
            audit,
            client_factory=lambda server: make_client(server, in_memory=False),
        )

    async def aclose(self) -> None:
        """Close every client (stops stdio subprocesses)."""
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.close()

    async def _reset_client(self, server: str, failed: Client) -> bool:
        """Replace ``failed`` with a fresh client; False when no rebuild is possible."""
        if self._client_factory is None:
            return False
        # A concurrent call may already have replaced it: keep the newer client.
        if self._clients.get(server) is failed:
            self._clients[server] = self._client_factory(server)
        with contextlib.suppress(Exception):
            await failed.close()
        return True

    async def _transport_failed(
        self, run_id: str, agent: str, tool: str, server: str, client: Client, exc: Exception
    ) -> bool:
        """Rebuild the client and audit ``transport_reset``; False when no rebuild is possible."""
        if not await self._reset_client(server, client):
            return False
        self._audit.append(
            run_id, agent, "transport_reset", {"tool": tool, "error_type": type(exc).__name__}
        )
        return True

    async def _call_mcp(self, run_id: str, agent: str, tool: str, call_args: dict) -> Any:
        server, _, tool_name = tool.partition(".")
        for attempt in range(2):
            client = self._clients[server]
            try:
                async with client:
                    return await client.call_tool(
                        tool_name, call_args, timeout=self._call_timeout_s
                    )
            except ToolError:
                raise  # the server answered: the session is fine
            except McpError as exc:
                # The MCP session raises CONNECTION_CLOSED itself when the pipe closes with a
                # request pending (e.g. the server died mid-call), and REQUEST_TIMEOUT when no
                # answer arrives in time (a stalled server): both are transport failures. A
                # timed-out hold or charge may still have reached the server: never resent.
                if exc.error.code not in _TRANSPORT_ERROR_CODES:
                    raise  # any other code is the server's own error answer
                if not await self._transport_failed(run_id, agent, tool, server, client, exc):
                    raise
                if attempt > 0 or tool not in READ_ONLY_TOOLS:
                    raise
            except Exception as exc:
                if not await self._transport_failed(run_id, agent, tool, server, client, exc):
                    raise
                if attempt > 0 or tool not in READ_ONLY_TOOLS:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def record(self, run_id: str, actor: str, event: str, data: dict) -> None:
        """Audit a non-tool step (intent, approval decision) through the same chained log."""
        self._audit.append(run_id, actor, event, data)

    def _approval_status(
        self, run_id: str, tool: str, args: dict, approval_token: str | None
    ) -> tuple[bool, str | None]:
        """(approved, why not). Approved only with a valid proof matching these args."""
        if approval_token is None:
            return False, "no approval proof"
        try:
            claims = verify_approval_token(
                approval_token,
                run_id=run_id,
                amount_cents=args.get("amount_cents"),
                currency=args.get("currency"),
            )
        except (ValueError, RuntimeError) as exc:
            return False, str(exc)
        if args.get("approval_ref") != f"{claims['approver']}:{claims['quote_hash']}":
            return False, "approval_ref does not match the approval proof"
        # payments.create_checkout also names the quote hash (it goes into the session's metadata).
        if "quote_hash" in args and args["quote_hash"] != claims["quote_hash"]:
            return False, "quote_hash does not match the approval proof"
        if tool == WALLET_TOKEN_TOOL:
            # Only the human picks the wallet, and only for the payer the decision named.
            if claims.get("payment_model") != "wallet":
                return False, "the approval did not choose the wallet"
            if args.get("run_id") != run_id:
                return False, "run_id does not match the approval proof"
            if not claims.get("payer") or args.get("wallet_user") != claims.get("payer"):
                return False, "wallet_user does not match the approval proof"
        return True, None

    async def call(
        self,
        run_id: str,
        agent: str,
        tool: str,
        args: dict,
        *,
        approval_token: str | None = None,
    ) -> GuardResult:
        approved, approval_error = self._approval_status(run_id, tool, args, approval_token)
        try:
            self._policy.check(agent, tool, approved=approved)
        except PolicyViolation as exc:
            denied = {"tool": tool, "reason": exc.reason, "approved": approved}
            if approval_error is not None and approval_token is not None:
                denied["approval_error"] = approval_error
            self._audit.append(run_id, agent, "policy_denied", denied)
            emit_run_event(
                {"type": "policy_denied", "agent": agent, "tool": tool, "reason": exc.reason}
            )
            raise

        server, _, tool_name = tool.partition(".")
        client = self._clients.get(server)
        if client is None or not tool_name:
            raise ValueError(f"no MCP client for tool {tool!r}")

        # The agent's own args are audited; any caller-supplied token is ignored.
        call_args = {k: v for k, v in args.items() if k != "agent_token"}
        self._audit.append(
            run_id, agent, "tool_call", {"tool": tool, "args": call_args, "approved": approved}
        )
        emit_run_event({"type": "tool_call", "agent": agent, "tool": tool})
        try:
            # Inside the try: a token failure (e.g. missing secret) is audited as tool_error,
            # so every audited tool_call has a recorded outcome.
            if server in TOKEN_SERVERS:
                call_args["agent_token"] = issue_token(agent, server)
            result = await self._call_mcp(run_id, agent, tool, call_args)
        except Exception as exc:
            self._audit.append(run_id, agent, "tool_error", {"tool": tool, "error": str(exc)[:500]})
            emit_run_event({"type": "tool_error", "agent": agent, "tool": tool})
            raise

        guard = inspect_tool_output(tool, _result_payload(result))
        emit_run_event(
            {"type": "tool_result", "agent": agent, "tool": tool, "flagged": guard.flagged}
        )
        try:
            self._audit.append(
                run_id,
                agent,
                "tool_result",
                {"tool": tool, "result": guard.sanitized, "flagged": guard.flagged},
            )
            if guard.flagged:
                self._audit.append(
                    run_id, agent, "injection_flagged", {"tool": tool, "findings": guard.findings}
                )
        except Exception as exc:
            # The side effect already happened (a hold, a capture): never drop the result.
            raise UnauditedToolResult(tool, guard) from exc
        return guard


def make_client(server: str, *, in_memory: bool) -> Client:
    """In-memory client (tests) or stdio subprocess client (CLI/API) for one server."""
    if in_memory:
        if server == "payments":
            from mcp_servers.payments_sandbox.server import mcp as server_mcp
        elif server == "wallet":
            from mcp_servers.wallet.server import mcp as server_mcp
        elif server == "travel_search":
            from mcp_servers.travel_search.server import mcp as server_mcp
        else:
            raise KeyError(server)
        return Client(server_mcp)

    module = SERVER_MODULES[server]
    # stdio servers get a minimal default env otherwise; they need e.g. AGENT_TOKEN_SECRET.
    # No banner and no PyPI version check: the servers make no undeclared outbound calls.
    env = {**os.environ, "FASTMCP_SHOW_CLI_BANNER": "false", "FASTMCP_CHECK_FOR_UPDATES": "off"}
    return Client(
        StdioTransport(command=sys.executable, args=["-m", module], env=env, cwd=str(_REPO_ROOT))
    )


def make_clients(in_memory: bool) -> dict[str, Client]:
    """One client per server in ``SERVER_MODULES``; see ``make_client``."""
    return {name: make_client(name, in_memory=in_memory) for name in SERVER_MODULES}


__all__ = [
    "GatedToolClient",
    "READ_ONLY_TOOLS",
    "SERVER_MODULES",
    "UnauditedToolResult",
    "emit_run_event",
    "make_client",
    "make_clients",
]
