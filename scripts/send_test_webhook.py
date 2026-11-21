"""Send a SYNTHETIC, signed processor event to the local API (dev and demo only).

This is not an event from the card processor. It is built here, signed with the endpoint secret
in ``STRIPE_WEBHOOK_SECRET`` (the same ``v1`` HMAC scheme the processor uses) and posted to
``/webhooks/stripe``, so the webhook path can be shown without the processor's CLI. Its id
starts with ``evt_synthetic_`` and its object carries ``metadata.synthetic_test_event``.
Reconciliation (``POST /reconcile``) compares runs with the processor's own record, so a
synthetic refund of a payment that was not refunded shows up there as ``settlement_mismatch``
and is corrected back.

    uv run python scripts/send_test_webhook.py refunded --latest
    uv run python scripts/send_test_webhook.py confirmed --payment-id pi_123
    uv run python scripts/send_test_webhook.py disputed --latest --livemode   # rejected (test only)
    # the same --event-id twice shows an idempotent replay (second answer: duplicate)
    uv run python scripts/send_test_webhook.py confirmed --latest --event-id evt_synthetic_1

``--latest`` reads the newest test-mode capture from the payments ledger (``PAYMENTS_DB_PATH``,
read-only). The secret is read from the environment or ``.env``; it is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_servers.payments_sandbox import events  # noqa: E402

KINDS = {
    "confirmed": "payment_intent.succeeded",
    "failed": "payment_intent.payment_failed",
    "refunded": "charge.refunded",
    "disputed": "charge.dispute.created",
}
DEFAULT_URL = "http://localhost:8000/webhooks/stripe"


def latest_capture(db_path: str) -> tuple[str, int, str]:
    """(payment_id, amount_cents, currency) of the newest test-mode capture."""
    if not Path(db_path).exists():
        raise SystemExit(f"no payments ledger at {db_path}: book a run in stripe_test mode first")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payment_id, amount_cents, currency FROM payments "
            "WHERE provider = 'stripe_test' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit("no test-mode capture in the ledger: set PAYMENTS_PROVIDER=stripe_test")
    return str(row[0]), int(row[1]), str(row[2])


def build_event(
    etype: str, payment_id: str, amount_cents: int, currency: str, *, livemode: bool, event_id: str
) -> dict:
    metadata = {"synthetic_test_event": "true"}
    cur = currency.lower()
    if etype.startswith("payment_intent."):
        status = "succeeded" if etype.endswith("succeeded") else "requires_payment_method"
        obj = {
            "id": payment_id,
            "object": "payment_intent",
            "amount": amount_cents,
            "currency": cur,
            "status": status,
        }
    elif etype == "charge.refunded":
        obj = {
            "id": f"ch_synthetic_{uuid.uuid4().hex[:12]}",
            "object": "charge",
            "payment_intent": payment_id,
            "amount": amount_cents,
            "amount_refunded": amount_cents,
            "refunded": True,
            "currency": cur,
        }
    else:
        obj = {
            "id": f"dp_synthetic_{uuid.uuid4().hex[:12]}",
            "object": "dispute",
            "payment_intent": payment_id,
            "amount": amount_cents,
            "currency": cur,
            "reason": "general",
            "status": "needs_response",
        }
    obj["metadata"] = metadata
    return {
        "id": event_id,
        "object": "event",
        "api_version": "synthetic",
        "type": etype,
        "created": int(time.time()),
        "livemode": livemode,
        "data": {"object": obj},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a SYNTHETIC signed processor event.")
    parser.add_argument("kind", choices=sorted(KINDS), help="which event to simulate")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--payment-id", help="PaymentIntent id (pi_...) of a test-mode run")
    target.add_argument("--latest", action="store_true", help="newest test-mode capture")
    parser.add_argument("--amount-cents", type=int, help="integer cents (default: the ledger's)")
    parser.add_argument("--currency", default="CHF")
    parser.add_argument("--event-id", help="reuse an id to demonstrate an idempotent replay")
    parser.add_argument("--livemode", action="store_true", help="mark live mode (is rejected)")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args(argv)

    load_dotenv()
    secret = events.webhook_secret()
    if secret is None:
        print(f"{events.WEBHOOK_SECRET_ENV} is not set (env or .env)", file=sys.stderr)
        return 2
    amount, currency = args.amount_cents, args.currency
    if args.latest:
        db = os.environ.get("PAYMENTS_DB_PATH") or "var/payments.db"
        payment_id, ledger_amount, currency = latest_capture(db)
        amount = ledger_amount if amount is None else amount
    else:
        payment_id = args.payment_id
    if amount is None:
        amount = 5600
    if amount <= 0:
        print("--amount-cents must be a positive integer", file=sys.stderr)
        return 2
    event_id = args.event_id or f"evt_synthetic_{uuid.uuid4().hex}"
    event = build_event(
        KINDS[args.kind], payment_id, amount, currency, livemode=args.livemode, event_id=event_id
    )
    body = json.dumps(event, separators=(",", ":")).encode()
    headers = {
        events.SIGNATURE_HEADER: events.sign(body, secret, int(time.time())),
        "Content-Type": "application/json",
    }
    print("SYNTHETIC TEST EVENT (not from the processor)")
    print(f"  {event['type']}  {event_id}  payment {payment_id}  {amount} cents {currency}")
    try:
        response = httpx.post(args.url, content=body, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        print(f"could not reach {args.url}: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"  -> HTTP {response.status_code} {response.text}")
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
