"""Processor events (webhooks): verified, stored once, matched to the ledger. Payments side.

The card processor is the source of truth after the capture: a payment can still fail late,
be refunded or be disputed, and the synchronous answer at capture time may have been lost.
It reports those facts as signed webhook events. This module belongs to the payments
institution: it holds the endpoint secret (``STRIPE_WEBHOOK_SECRET``), verifies each delivery
and records it in the institution's own database (``PAYMENTS_DB_PATH``, table
``processor_events``), next to the ledger it is matched against. The agents' API only hosts
the HTTP route: it hands over the raw body and the signature header, and gets back a minimal,
verified notice (run id, payment id, settlement, integer cents) to mirror into its run history
and audit log. It never parses or trusts an unverified body.

Signature (the processor's ``v1`` scheme): header ``Stripe-Signature: t=<unix>,v1=<hex>[,...]``,
``v1 = HMAC-SHA256(secret, f"{t}.{raw_body}")``, compared in constant time against every
``v1`` value; ``t`` must be within ``TOLERANCE_S`` (300 s) of now, so a captured delivery
cannot be replayed later. The event id is the table's PRIMARY KEY, so a redelivered event is
acknowledged without a second effect.

Hosted Checkout (``PAYMENTS_FLOW=checkout``): ``checkout.session.completed`` and
``checkout.session.expired`` are stored the same way and matched to the ledger by the session id;
the verified session fields (no customer details) travel back in ``Ingested.checkout_session``
so the API can apply them with ``checkout.apply_session`` and resume the paused run.

Only test mode is ever operated: a ``livemode: true`` event is stored with the disposition
``livemode_rejected`` and never touches a run (see ``docs/threat-model.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regulated_agent_lab.services.payments import server as payments

WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
SIGNATURE_HEADER = "Stripe-Signature"
TOLERANCE_S = 300
# Real events are a few KiB; anything far larger is refused before it is hashed or parsed.
MAX_BODY_BYTES = 256 * 1024

# Handled event type -> (audit event name, settlement it implies for the payment).
HANDLED: dict[str, tuple[str, str]] = {
    "payment_intent.succeeded": ("payment_confirmed", "confirmed"),
    "payment_intent.payment_failed": ("payment_failed", "failed"),
    "charge.refunded": ("payment_refunded", "refunded"),
    "charge.dispute.created": ("dispute_opened", "disputed"),
}
# Hosted Checkout sessions (completion and expiry): handled by ``checkout.apply_session``.
CHECKOUT_COMPLETED = "checkout.session.completed"
CHECKOUT_EXPIRED = "checkout.session.expired"
CHECKOUT_EVENTS = frozenset({CHECKOUT_COMPLETED, CHECKOUT_EXPIRED})
# The session fields kept from a checkout event: what completion needs, no customer details.
CHECKOUT_FIELDS = (
    "id",
    "object",
    # "payment" (a run's hosted Checkout) or "setup" (a wallet user saving a card).
    "mode",
    "status",
    "payment_status",
    "amount_total",
    "currency",
    "client_reference_id",
    "payment_intent",
    "livemode",
)
# Strongest fact wins, whatever order the deliveries arrive in: a dispute or refund outlives
# the confirmation, and a success after a failed attempt (a retry) means the money arrived.
SETTLEMENT_PRECEDENCE = ("disputed", "refunded", "confirmed", "failed")
SETTLEMENTS = (*SETTLEMENT_PRECEDENCE, "pending")

MATCHED, UNMATCHED, LIVEMODE_REJECTED = "matched", "unmatched", "livemode_rejected"


class WebhookNotConfigured(RuntimeError):
    """No endpoint secret: the endpoint is closed (503) and stores nothing."""


class SignatureError(ValueError):
    """Missing, malformed, wrong or stale signature (400, nothing stored)."""


class MalformedEvent(ValueError):
    """A correctly signed body that is not a processor event we can read (400)."""


def webhook_secret() -> str | None:
    secret = (os.environ.get(WEBHOOK_SECRET_ENV) or "").strip()
    return secret or None


def sign(payload: bytes, secret: str, timestamp: int) -> str:
    """The ``Stripe-Signature`` header value for ``payload`` (tests and the demo script)."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_signature(
    payload: bytes, header: str | None, secret: str, *, now: int | None = None
) -> None:
    """Raise ``SignatureError`` unless ``header`` signs ``payload`` with ``secret``, recently."""
    if not header:
        raise SignatureError("missing signature header")
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("malformed timestamp") from exc
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        raise SignatureError("no timestamp or v1 signature")
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > TOLERANCE_S:
        raise SignatureError("timestamp outside tolerance")
    expected = sign(payload, secret, timestamp).split("v1=", 1)[1]
    # Check every v1 value (the processor sends several while a secret is being rolled).
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise SignatureError("signature mismatch")


@dataclass(frozen=True)
class ProcessorEvent:
    event_id: str
    type: str
    payment_id: str | None
    amount_cents: int | None
    currency: str | None
    livemode: bool
    created: int
    # checkout.session.* only: the verified session, reduced to CHECKOUT_FIELDS (+ run_id).
    checkout_session: dict | None = None


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedEvent(f"{field} must be an integer")
    return value


def parse_event(payload: bytes) -> ProcessorEvent:
    """Read only what reconciliation needs; billing details and the rest are never kept."""
    try:
        body = json.loads(payload)
    except ValueError as exc:
        raise MalformedEvent("body is not JSON") from exc
    if not isinstance(body, dict):
        raise MalformedEvent("event is not an object")
    event_id, etype, livemode = body.get("id"), body.get("type"), body.get("livemode")
    if not isinstance(event_id, str) or not event_id.startswith("evt_") or len(event_id) > 255:
        raise MalformedEvent("bad event id")
    if not isinstance(etype, str) or len(etype) > 100:
        raise MalformedEvent("bad event type")
    if not isinstance(livemode, bool):
        raise MalformedEvent("livemode must be a boolean")
    created = _int(body.get("created"), "created")
    obj = (body.get("data") or {}).get("object") if isinstance(body.get("data"), dict) else None
    if not isinstance(obj, dict):
        raise MalformedEvent("missing data.object")
    payment_id: Any = None
    amount: Any = None
    session: dict | None = None
    if etype in CHECKOUT_EVENTS:
        session = _checkout_session(obj)
        amount = obj.get("amount_total")
        # Only a paid session names the PaymentIntent the money went through.
        if obj.get("payment_status") == "paid" and isinstance(obj.get("payment_intent"), str):
            payment_id = obj["payment_intent"]
    elif etype.startswith("payment_intent."):
        payment_id, amount = obj.get("id"), obj.get("amount")
    elif etype == "charge.refunded":
        payment_id, amount = obj.get("payment_intent"), obj.get("amount_refunded")
    elif etype.startswith("charge."):
        payment_id, amount = obj.get("payment_intent"), obj.get("amount")
    if payment_id is not None and (not isinstance(payment_id, str) or len(payment_id) > 255):
        raise MalformedEvent("bad payment id")
    currency = obj.get("currency")
    if currency is not None and (not isinstance(currency, str) or len(currency) > 10):
        raise MalformedEvent("bad currency")
    return ProcessorEvent(
        event_id=event_id,
        type=etype,
        payment_id=payment_id,
        amount_cents=None if amount is None else _int(amount, "amount"),
        currency=currency.upper() if isinstance(currency, str) else None,
        livemode=livemode,
        created=created,
        checkout_session=session,
    )


def _checkout_session(obj: dict) -> dict:
    session_id = obj.get("id")
    if not isinstance(session_id, str) or not session_id.startswith("cs_") or len(session_id) > 255:
        raise MalformedEvent("bad checkout session id")
    session = {k: obj.get(k) for k in CHECKOUT_FIELDS}
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    run_id = metadata.get("run_id")
    session["metadata"] = {"run_id": run_id} if isinstance(run_id, str) else {}
    return session


# A paid Checkout Session is as good a confirmation as the PaymentIntent's own event.
_IMPLIED = {**{t: s for t, (_, s) in HANDLED.items()}, CHECKOUT_COMPLETED: "confirmed"}


def derive_settlement(types: Iterable[str]) -> str | None:
    """The settlement the events of one payment imply (None when they imply nothing)."""
    implied = {_IMPLIED[t] for t in types if t in _IMPLIED}
    return next((s for s in SETTLEMENT_PRECEDENCE if s in implied), None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS processor_events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    payment_id TEXT,
    amount_cents INTEGER CHECK (amount_cents IS NULL OR typeof(amount_cents) = 'integer'),
    currency TEXT,
    livemode INTEGER NOT NULL CHECK (livemode IN (0, 1)),
    created INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('matched', 'unmatched', 'livemode_rejected')),
    idempotency_key TEXT,
    forwarded_at TEXT
);
CREATE INDEX IF NOT EXISTS processor_events_payment ON processor_events (payment_id);
"""
_COLUMNS = (
    "event_id",
    "type",
    "payment_id",
    "amount_cents",
    "currency",
    "livemode",
    "created",
    "received_at",
    "disposition",
    "idempotency_key",
    "forwarded_at",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM processor_events"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class EventStore:
    """``processor_events`` in the payments database (its own connection, same file)."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, timeout=payments.BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False
        )
        self._lock = threading.Lock()
        deadline = time.monotonic() + payments.BUSY_TIMEOUT_S
        with self._lock:
            while True:
                try:
                    if path != ":memory:":
                        self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.executescript(_SCHEMA)
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(0.01)

    @staticmethod
    def _record(row: tuple) -> dict:
        record = dict(zip(_COLUMNS, row, strict=True))
        record["livemode"] = bool(record["livemode"])
        return record

    def insert_or_get(self, record: dict) -> tuple[dict, bool]:
        """Store the event unless its id exists; (the stored row, whether it was new)."""
        values = {**record, "received_at": _now(), "forwarded_at": None}
        values["livemode"] = int(bool(values["livemode"]))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    f"INSERT INTO processor_events ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    tuple(values[c] for c in _COLUMNS),
                )
                inserted = cur.rowcount == 1
                row = self._conn.execute(
                    f"{_SELECT} WHERE event_id = ?", (values["event_id"],)
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._record(row), inserted

    def mark_forwarded(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE processor_events SET forwarded_at = ? WHERE event_id = ?",
                (_now(), event_id),
            )

    def for_payment(self, payment_id: str) -> list[dict]:
        """Test-mode events naming ``payment_id``, oldest first (live-mode ones never count)."""
        with self._lock:
            rows = self._conn.execute(
                f"{_SELECT} WHERE payment_id = ? AND disposition != 'livemode_rejected' "
                "ORDER BY created, received_at",
                (payment_id,),
            ).fetchall()
        return [self._record(r) for r in rows]

    def settlement_for(self, payment_id: str) -> str | None:
        return derive_settlement(e["type"] for e in self.for_payment(payment_id))

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(f"{_SELECT} ORDER BY received_at").fetchall()
        return [self._record(r) for r in rows]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM processor_events")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORES: dict[str, EventStore] = {}
_STORES_LOCK = threading.Lock()


def event_store() -> EventStore:
    """The event store for the current ``PAYMENTS_DB_PATH`` (one connection per path)."""
    path = payments.payments_db_path()
    with _STORES_LOCK:
        if path not in _STORES:
            _STORES[path] = EventStore(path)
        return _STORES[path]


@dataclass(frozen=True)
class Ingested:
    """What the payments side tells the agents' API about one verified delivery."""

    event: ProcessorEvent
    # "matched" | "unmatched" | "livemode_rejected" | "ignored" (type not handled, not stored)
    disposition: str
    # True the first time the event is seen (or while its effects were never applied).
    needs_forward: bool
    run_id: str | None = None
    # Settlement of the matched payment, from all of its stored events.
    settlement: str | None = None
    # The ledger amount of the matched payment (to flag a mismatch without trusting the event).
    ledger_amount_cents: int | None = None
    ledger_currency: str | None = None

    @property
    def is_checkout(self) -> bool:
        return self.event.type in CHECKOUT_EVENTS


def ingest(payload: bytes, signature: str | None, *, now: int | None = None) -> Ingested:
    """Verify, parse, match and store one webhook delivery. Raises before storing anything."""
    secret = webhook_secret()
    if secret is None:
        raise WebhookNotConfigured(f"{WEBHOOK_SECRET_ENV} is not set")
    if len(payload) > MAX_BODY_BYTES:
        raise MalformedEvent("body too large")
    verify_signature(payload, signature, secret, now=now)
    event = parse_event(payload)
    if event.type not in HANDLED and event.type not in CHECKOUT_EVENTS:
        return Ingested(event=event, disposition="ignored", needs_forward=False)

    ledger_row = None
    if event.livemode:
        disposition = LIVEMODE_REJECTED
    else:
        if event.checkout_session is not None:
            # A checkout is matched by its session id: the ledger recorded it at creation.
            ledger_row = payments.ledger().get_by_session_id(event.checkout_session["id"])
        elif event.payment_id:
            row = payments.ledger().get_by_payment_id(event.payment_id)
            # Only captures the processor actually made can be settled by it.
            if row is not None and row["provider"] == "stripe_test":
                ledger_row = row
        disposition = MATCHED if ledger_row is not None else UNMATCHED
    store = event_store()
    stored, _ = store.insert_or_get(
        {
            "event_id": event.event_id,
            "type": event.type,
            "payment_id": event.payment_id,
            "amount_cents": event.amount_cents,
            "currency": event.currency,
            "livemode": event.livemode,
            "created": event.created,
            "disposition": disposition,
            "idempotency_key": ledger_row["idempotency_key"] if ledger_row else None,
        }
    )
    # A redelivery is answered from the stored row: its first disposition stands.
    run_id = stored["idempotency_key"]
    settlement = None
    if stored["disposition"] == MATCHED and stored["payment_id"]:
        settlement = store.settlement_for(stored["payment_id"])
    ledger_row = payments.ledger().get(run_id) if run_id else None
    return Ingested(
        event=event,
        disposition=stored["disposition"],
        needs_forward=stored["forwarded_at"] is None,
        run_id=run_id,
        settlement=settlement,
        ledger_amount_cents=ledger_row["amount_cents"] if ledger_row else None,
        ledger_currency=ledger_row["currency"] if ledger_row else None,
    )


def reset_events() -> None:
    """Empty the current event store (tests only)."""
    event_store().clear()


__all__ = [
    "HANDLED",
    "SETTLEMENTS",
    "SIGNATURE_HEADER",
    "TOLERANCE_S",
    "WEBHOOK_SECRET_ENV",
    "EventStore",
    "Ingested",
    "MalformedEvent",
    "ProcessorEvent",
    "SignatureError",
    "WebhookNotConfigured",
    "derive_settlement",
    "event_store",
    "ingest",
    "parse_event",
    "sign",
    "verify_signature",
]


def initial_settlement(run_id: str) -> str | None:
    """The settlement a run starts with once it is captured: None for a simulated capture (no
    processor will ever report on it), else what its events already say, else ``pending``."""
    row = payments.ledger().get(run_id)
    if row is None or row["provider"] != "stripe_test":
        return None
    return event_store().settlement_for(row["payment_id"]) or "pending"
