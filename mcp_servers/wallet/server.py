"""wallet: the agent-platform wallet as its own institution, exposed over MCP.

Two tools, two callers, each with its own scoped identity (``controls.identity`` tokens for
server ``wallet``):

- ``issue_payment_token``: only the ``booking`` agent, and the gate lets it through only with
  the human's signed approval token for the wallet payment model (``tool_policy.yaml``:
  booking only, approval required). It returns an opaque single-use token bound to the exact
  approved amount, the currency, the merchant, the run and a 15-minute expiry, or a reason the
  run falls back to the hosted Checkout (no saved card, expired card, above the mandate).
- ``redeem_payment_token``: only the ``payments`` institution (no agent is allowed it by the
  policy, and the server checks the caller's identity). It spends the token once and answers
  with the processor ids to charge, never card data.

The wallet's records are in its own SQLite file (``WALLET_DB_PATH``, see ``vault``).
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from controls.identity import verify_token
from mcp_servers.wallet import vault

SERVER_NAME = "wallet"
ISSUER_AGENT = "booking"
REDEEMER = "payments"

mcp = FastMCP(SERVER_NAME)


def _authorize(agent_token: str, allowed: str) -> None:
    try:
        caller = verify_token(agent_token, SERVER_NAME)
    except (ValueError, RuntimeError) as exc:
        raise ToolError("unauthorized") from exc
    if caller != allowed:
        raise ToolError("unauthorized")


@mcp.tool
def issue_payment_token(
    agent_token: str,
    wallet_user: str,
    amount_cents: int,
    currency: str,
    run_id: str,
    merchant_id: str,
    approval_ref: str,
) -> dict:
    """A single-use payment token for exactly the approved amount (booking agent only)."""
    _authorize(agent_token, ISSUER_AGENT)
    try:
        return vault.issue_payment_token(
            user=wallet_user,
            amount_cents=amount_cents,
            currency=currency,
            run_id=run_id,
            merchant_id=merchant_id,
            approval_ref=approval_ref,
        )
    except vault.WalletRefused as exc:
        raise ToolError(f"wallet refused: {exc}") from exc


@mcp.tool
def redeem_payment_token(
    agent_token: str,
    payment_token: str,
    amount_cents: int,
    currency: str,
    merchant_id: str,
    run_id: str,
) -> dict:
    """Spend a payment token once (payments institution only): the processor ids to charge."""
    _authorize(agent_token, REDEEMER)
    try:
        return vault.redeem_payment_token(
            payment_token=payment_token,
            amount_cents=amount_cents,
            currency=currency,
            merchant_id=merchant_id,
            run_id=run_id,
        )
    except vault.WalletRefused as exc:
        raise ToolError(f"payment token refused: {exc}") from exc


if __name__ == "__main__":
    mcp.run(show_banner=False)
