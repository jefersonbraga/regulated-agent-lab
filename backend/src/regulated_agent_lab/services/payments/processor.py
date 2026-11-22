"""Card processor in TEST MODE: a real payment API call that never moves money.

Opt-in with ``PAYMENTS_PROVIDER=stripe_test`` and ``STRIPE_TEST_SECRET_KEY`` set to a test-mode
secret key (``sk_test_...``) or, better, a test-mode restricted key (``rk_test_...``).
Fails closed: any key that is not a test-mode secret key is refused before a request is made,
so a live key pasted by mistake can never charge a card. The run id is sent as the idempotency
key, so resuming an approval twice captures once at the processor too, not only in the local
ledger.

Two payment models use this module (``PAYMENTS_FLOW``, see ``regulated_agent_lab.agents.booking``):

- ``wallet``: ``capture`` creates and confirms an off-session PaymentIntent for the Customer and
  PaymentMethod the wallet institution handed over when it redeemed the run's scoped token
  (merchant-initiated, under the user's mandate). A card that needs strong customer
  authentication answers ``authentication_required`` / ``requires_action``: that is
  ``AuthenticationRequired``, and the run falls back to the hosted Checkout.
- ``checkout``: ``create_checkout_session`` creates a hosted Checkout Session; the customer
  confirms the payment on the processor's own page (and authenticates there if strong customer
  authentication asks for it). ``retrieve_checkout_session`` and ``expire_checkout_session``
  read and close it. Completion is only ever taken from the processor's own record.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

API_URL = "https://api.stripe.com/v1/payment_intents"
CHECKOUT_API_URL = "https://api.stripe.com/v1/checkout/sessions"
KEY_ENV = "STRIPE_TEST_SECRET_KEY"
# Test-mode secret keys and test-mode restricted keys (least privilege: e.g. PaymentIntents
# write only). Live keys (sk_live_, rk_live_) and publishable keys (pk_) are always refused.
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")
REQUEST_TIMEOUT_S = 8.0  # below the MCP call deadline (MCP_CALL_TIMEOUT_S)


class ProcessorNotConfigured(RuntimeError):
    """Missing key, or a key that is not a test-mode secret key."""


class ProcessorUnavailable(RuntimeError):
    """Network error, timeout or an unexpected processor answer."""


class PaymentDeclined(RuntimeError):
    """The processor refused the (test) payment."""


class AuthenticationRequired(PaymentDeclined):
    """The card asks for strong customer authentication: an off-session charge cannot do it."""


def require_test_key() -> str:
    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key.startswith(TEST_KEY_PREFIXES):
        # Never echo the key, not even a prefix of it.
        raise ProcessorNotConfigured(f"{KEY_ENV} must be a test-mode secret key")
    return key


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict):
        return None
    code = error.get("code") or error.get("decline_code")
    intent = error.get("payment_intent")
    if isinstance(intent, dict) and intent.get("status") == "requires_action":
        return "authentication_required"
    return code if isinstance(code, str) else None


def capture(
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    metadata: dict[str, str],
    *,
    customer: str,
    payment_method: str,
    transport: Any = None,
) -> dict:
    """Create and confirm an off-session test-mode payment; ``{"payment_id", "status"}``.

    ``customer`` and ``payment_method`` are the processor ids the wallet redeemed the run's
    token for: the card was saved by its holder on the processor's own page.
    """
    key = require_test_key()
    form: dict[str, str] = {
        "amount": str(amount_cents),
        "currency": currency.lower(),
        "customer": customer,
        "payment_method": payment_method,
        # The processor no longer accepts payment_method_types (methods come from the dashboard):
        # automatic methods, but never a redirect, since an off-session charge has no customer
        # present to send anywhere.
        "automatic_payment_methods[enabled]": "true",
        "automatic_payment_methods[allow_redirects]": "never",
        "off_session": "true",
        "confirm": "true",
        # The latest charge carries the public, processor-hosted receipt URL: one call, not two.
        "expand[]": "latest_charge",
        **{f"metadata[{k}]": v for k, v in metadata.items()},
    }
    headers = {"Idempotency-Key": f"regulated-agent-lab-{idempotency_key}"}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.post(API_URL, data=form, headers=headers, auth=(key, ""))
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    if response.status_code == 402:
        if _error_code(response) == "authentication_required":
            raise AuthenticationRequired("authentication_required")
        raise PaymentDeclined("declined")
    if response.status_code != 200:
        raise ProcessorUnavailable(f"HTTP {response.status_code}")
    try:
        body = response.json()
        payment_id, status, livemode = body["id"], body["status"], body.get("livemode")
    except (ValueError, KeyError, TypeError) as exc:
        raise ProcessorUnavailable("unexpected response") from exc
    if livemode:  # defence in depth: a test key can never yield a live payment
        raise ProcessorNotConfigured("processor answered in live mode")
    if status == "requires_action":
        raise AuthenticationRequired(str(status))
    if status != "succeeded":
        raise PaymentDeclined(str(status))
    result = {"payment_id": str(payment_id), "status": "captured"}
    receipt = receipt_url(body)
    if receipt is not None:
        result["receipt_url"] = receipt
    return result


_RECEIPT_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.stripe\.com/[^\s\"'<>]{1,1024}$")


def receipt_url(intent: Any) -> str | None:
    """The public receipt URL of a PaymentIntent whose ``latest_charge`` is expanded.

    Only an https URL on the processor's own domain is accepted: the UI links to it.
    """
    charge = intent.get("latest_charge") if isinstance(intent, dict) else None
    url = charge.get("receipt_url") if isinstance(charge, dict) else None
    return url if isinstance(url, str) and _RECEIPT_RE.fullmatch(url) else None


class PaymentNotFound(LookupError):
    """The processor has no PaymentIntent with that id (HTTP 404)."""


_PAYMENT_ID_RE = re.compile(r"^pi_[A-Za-z0-9_]{1,128}$")


def retrieve(payment_id: str, transport: Any = None) -> dict:
    """Read one PaymentIntent (with its latest charge expanded) in test mode.

    Same fail-closed key check as ``capture``; read access to PaymentIntents is enough, so a
    restricted key (``rk_test_``) works. Returns the processor's JSON object. Raises
    ``PaymentNotFound`` for a 404, ``ProcessorUnavailable`` for anything unexpected and
    ``ProcessorNotConfigured`` for a bad key or a live-mode answer.
    """
    key = require_test_key()
    if not _PAYMENT_ID_RE.fullmatch(payment_id or ""):
        # Never interpolate an unexpected value into the URL path.
        raise PaymentNotFound("not a processor PaymentIntent id")
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.get(
                f"{API_URL}/{payment_id}",
                params={"expand[]": "latest_charge"},
                auth=(key, ""),
            )
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    if response.status_code == 404:
        raise PaymentNotFound(payment_id)
    if response.status_code != 200:
        raise ProcessorUnavailable(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ProcessorUnavailable("unexpected response") from exc
    if not isinstance(body, dict) or body.get("id") != payment_id:
        raise ProcessorUnavailable("unexpected response")
    if body.get("livemode"):
        raise ProcessorNotConfigured("processor answered in live mode")
    return body


# --- hosted Checkout (PAYMENTS_FLOW=checkout) -------------------------------------------------

# The processor refuses an ``expires_at`` less than 30 minutes ahead (and more than 24 hours).
CHECKOUT_MIN_EXPIRY_S = 30 * 60
_SESSION_ID_RE = re.compile(r"^cs_test_[A-Za-z0-9_]{1,200}$")


class CheckoutNotFound(LookupError):
    """The processor has no Checkout Session with that id (HTTP 404)."""


def _session_body(response: httpx.Response, session_id: str | None) -> dict:
    if response.status_code == 404:
        raise CheckoutNotFound(session_id or "checkout session")
    if response.status_code == 402:
        raise PaymentDeclined("declined")
    if response.status_code != 200:
        raise ProcessorUnavailable(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ProcessorUnavailable("unexpected response") from exc
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise ProcessorUnavailable("unexpected response")
    if session_id is not None and body["id"] != session_id:
        raise ProcessorUnavailable("unexpected response")
    if body.get("livemode") is not False:  # defence in depth: test mode only, ever
        raise ProcessorNotConfigured("processor answered in live mode")
    if not _SESSION_ID_RE.fullmatch(body["id"]):
        raise ProcessorUnavailable("unexpected checkout session id")
    return body


def create_checkout_session(
    *,
    amount_cents: int,
    currency: str,
    run_id: str,
    quote_hash: str,
    approval_ref: str,
    success_url: str,
    cancel_url: str,
    expires_at: int,
    transport: Any = None,
) -> dict:
    """Create a hosted Checkout Session for exactly the approved total; returns its JSON.

    One line item (quantity 1, ``unit_amount`` = the approved total in cents), the run id as
    ``client_reference_id`` and in the metadata of both the session and its PaymentIntent, and
    an idempotency key derived from the run id, so a retried call creates one session.
    """
    key = require_test_key()
    metadata = {"run_id": run_id, "quote_hash": quote_hash, "approval_ref": approval_ref}
    form: dict[str, str] = {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": str(amount_cents),
        "line_items[0][price_data][product_data][name]": f"Demo Rail trip {quote_hash[:8]}",
        "client_reference_id": run_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "expires_at": str(int(expires_at)),
        **{f"metadata[{k}]": v for k, v in metadata.items()},
        **{f"payment_intent_data[metadata][{k}]": v for k, v in metadata.items()},
    }
    headers = {"Idempotency-Key": f"regulated-agent-lab-checkout-{run_id}"}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.post(CHECKOUT_API_URL, data=form, headers=headers, auth=(key, ""))
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    body = _session_body(response, None)
    if not isinstance(body.get("url"), str) or not body["url"].startswith("https://"):
        raise ProcessorUnavailable("checkout session without a payment page")
    return body


def _session_path(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        # Never interpolate an unexpected value into the URL path.
        raise CheckoutNotFound("not a test-mode checkout session id")
    return f"{CHECKOUT_API_URL}/{session_id}"


def retrieve_checkout_session(session_id: str, transport: Any = None) -> dict:
    """Read one Checkout Session, with its PaymentIntent and that intent's latest charge."""
    key = require_test_key()
    url = _session_path(session_id)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.get(
                url, params={"expand[]": "payment_intent.latest_charge"}, auth=(key, "")
            )
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    return _session_body(response, session_id)


def expire_checkout_session(session_id: str, transport: Any = None) -> dict | None:
    """Close an open Checkout Session; None when the processor refuses (e.g. already paid)."""
    key = require_test_key()
    url = f"{_session_path(session_id)}/expire"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.post(url, auth=(key, ""))
    except httpx.HTTPError as exc:
        raise ProcessorUnavailable(type(exc).__name__) from exc
    if response.status_code == 400:
        return None
    return _session_body(response, session_id)
