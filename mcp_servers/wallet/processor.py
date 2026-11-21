"""The wallet's calls to the card processor, in TEST MODE only (setup, never a payment).

The wallet never sees a card number: the customer types it on the processor's hosted page in
``mode=setup`` and the processor keeps it (the processor is the real vault). The wallet only
learns the ids it needs to have the card charged later (Customer, PaymentMethod) and what it
shows the user (brand, last four digits, expiry). Same fail-closed key check as the payments
institution: only ``sk_test_``/``rk_test_`` keys, refused before any request; a live-mode
answer is rejected.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx

from mcp_servers.payments_sandbox.processor import (
    REQUEST_TIMEOUT_S,
    ProcessorNotConfigured,
    ProcessorUnavailable,
    require_test_key,
)

API_BASE = "https://api.stripe.com/v1"
CUSTOMER_RE = re.compile(r"^cus_[A-Za-z0-9]{1,200}$")
PAYMENT_METHOD_RE = re.compile(r"^pm_[A-Za-z0-9_]{1,200}$")
SETUP_SESSION_RE = re.compile(r"^cs_test_[A-Za-z0-9_]{1,200}$")


class SetupNotFound(LookupError):
    """The processor has no such setup session (HTTP 404) or the id is not one."""


def _post(path: str, form: dict[str, str], idempotency_key: str, transport: Any) -> dict:
    key = require_test_key()
    headers = {"Idempotency-Key": idempotency_key}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.post(f"{API_BASE}{path}", data=form, headers=headers, auth=(key, ""))
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    return _body(response)


def _body(response: httpx.Response) -> dict:
    if response.status_code == 404:
        raise SetupNotFound("not found")
    if response.status_code != 200:
        raise ProcessorUnavailable(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ProcessorUnavailable("unexpected response") from exc
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise ProcessorUnavailable("unexpected response")
    if body.get("livemode") is not False:  # test mode only, ever
        raise ProcessorNotConfigured("processor answered in live mode")
    return body


def create_customer(user: str, transport: Any = None) -> str:
    """A processor Customer for one wallet user (created once, reused for every card)."""
    body = _post(
        "/customers",
        {"metadata[lab_user]": user, "description": "regulated-agent-lab wallet user"},
        f"regulated-agent-lab-wallet-customer-{uuid.uuid4().hex}",
        transport,
    )
    if not CUSTOMER_RE.fullmatch(body["id"]):
        raise ProcessorUnavailable("unexpected customer id")
    return body["id"]


def create_setup_session(
    *, customer_id: str, user: str, success_url: str, cancel_url: str, transport: Any = None
) -> dict:
    """A hosted Checkout Session in ``mode=setup``: the customer saves a card, nothing is paid."""
    body = _post(
        "/checkout/sessions",
        {
            "mode": "setup",
            "customer": customer_id,
            # The processor no longer accepts payment_method_types on Checkout Sessions (methods
            # come from the dashboard settings); a setup session then needs its currency.
            "currency": "chf",
            "client_reference_id": user,
            "metadata[lab_user]": user,
            "setup_intent_data[metadata][lab_user]": user,
            "success_url": success_url,
            "cancel_url": cancel_url,
        },
        f"regulated-agent-lab-wallet-setup-{uuid.uuid4().hex}",
        transport,
    )
    if not SETUP_SESSION_RE.fullmatch(body["id"]):
        raise ProcessorUnavailable("unexpected checkout session id")
    if not isinstance(body.get("url"), str) or not body["url"].startswith("https://"):
        raise ProcessorUnavailable("setup session without a page")
    return body


def retrieve_setup_session(session_id: str, transport: Any = None) -> dict:
    """Read a setup session back with its SetupIntent and that intent's PaymentMethod."""
    key = require_test_key()
    if not SETUP_SESSION_RE.fullmatch(session_id or ""):
        raise SetupNotFound("not a test-mode checkout session id")
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.get(
                f"{API_BASE}/checkout/sessions/{session_id}",
                params={"expand[]": "setup_intent.payment_method"},
                auth=(key, ""),
            )
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    body = _body(response)
    if body["id"] != session_id:
        raise ProcessorUnavailable("unexpected response")
    return body


def detach_payment_method(payment_method_id: str, transport: Any = None) -> bool:
    """Detach a saved card from its Customer at the processor; False when it refuses."""
    if not PAYMENT_METHOD_RE.fullmatch(payment_method_id or ""):
        return False
    try:
        _post(
            f"/payment_methods/{payment_method_id}/detach",
            {},
            f"regulated-agent-lab-wallet-detach-{uuid.uuid4().hex}",
            transport,
        )
    except (SetupNotFound, ProcessorUnavailable):
        return False
    return True


__all__ = [
    "CUSTOMER_RE",
    "PAYMENT_METHOD_RE",
    "SETUP_SESSION_RE",
    "ProcessorNotConfigured",
    "ProcessorUnavailable",
    "SetupNotFound",
    "create_customer",
    "create_setup_session",
    "detach_payment_method",
    "retrieve_setup_session",
]
