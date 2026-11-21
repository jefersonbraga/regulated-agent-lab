"""Scoped agent identity: short-lived HMAC tokens bound to (agent, target server)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time


def _secret() -> bytes:
    secret = os.environ.get("AGENT_TOKEN_SECRET")
    if not secret:
        raise RuntimeError("AGENT_TOKEN_SECRET is not set")
    return secret.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> bytes:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest()


def issue_token(agent: str, server: str, ttl_s: int = 300) -> str:
    if "|" in agent or "|" in server:
        raise ValueError("agent and server must not contain '|'")
    exp = int(time.time()) + int(ttl_s)
    payload = f"{agent}|{server}|{exp}"
    return f"{_b64e(payload.encode('utf-8'))}.{_b64e(_sign(payload))}"


def verify_token(token: str, server: str) -> str:
    """Return the agent name if the token is valid for `server`; raise ValueError otherwise."""
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = _b64d(payload_b64).decode("utf-8")
        sig = _b64d(sig_b64)
        agent, token_server, exp_text = payload.split("|")
        exp = int(exp_text)
    except (AttributeError, ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("malformed agent token") from exc
    if not hmac.compare_digest(sig, _sign(payload)):
        raise ValueError("invalid agent token signature")
    if token_server != server:
        raise ValueError("agent token not scoped to this server")
    if time.time() >= exp:
        raise ValueError("agent token expired")
    return agent


# --- human approval proof -------------------------------------------------------------------
# Issued by the graph's approval node only after a valid human decision, and checked by the
# gate before any approval-gated tool runs. It binds the approval to one run, one quote hash
# and the exact amount and currency, so an approval flag cannot be claimed by the caller. It
# also carries the payment model the human chose ("checkout", "wallet" or "agent") and whose
# wallet may pay (``payer``), so the agent cannot switch to the wallet on its own.

_APPROVAL_DOMAIN = "approval-v1:"


def issue_approval_token(
    *,
    run_id: str,
    approver: str,
    quote_hash: str,
    amount_cents: int,
    currency: str,
    ttl_s: int = 900,
    payment_model: str | None = None,
    payer: str | None = None,
) -> str:
    claims: dict = {
        "run_id": run_id,
        "approver": approver,
        "quote_hash": quote_hash,
        "amount_cents": int(amount_cents),
        "currency": currency,
        "exp": int(time.time()) + int(ttl_s),
    }
    if payment_model is not None:
        claims["payment_model"] = payment_model
    if payer is not None:
        claims["payer"] = payer
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{_b64e(payload.encode('utf-8'))}.{_b64e(_sign(_APPROVAL_DOMAIN + payload))}"


def verify_approval_token(token: str, *, run_id: str, amount_cents: int, currency: str) -> dict:
    """Return the approval claims if ``token`` covers this run, amount and currency."""
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = _b64d(payload_b64).decode("utf-8")
        sig = _b64d(sig_b64)
        claims = json.loads(payload)
        exp = int(claims["exp"])
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("malformed approval token") from exc
    if not hmac.compare_digest(sig, _sign(_APPROVAL_DOMAIN + payload)):
        raise ValueError("invalid approval token signature")
    if time.time() >= exp:
        raise ValueError("approval token expired")
    if claims.get("run_id") != run_id:
        raise ValueError("approval token is for another run")
    if (
        isinstance(amount_cents, bool)
        or claims.get("amount_cents") != amount_cents
        or claims.get("currency") != currency
    ):
        raise ValueError("approval token does not cover this amount")
    return claims
