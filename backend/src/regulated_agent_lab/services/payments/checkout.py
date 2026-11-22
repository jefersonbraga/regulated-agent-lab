"""Hosted Checkout completion: the ledger follows the processor's record, never the browser.

``PAYMENTS_FLOW=checkout`` (see ``agents.booking``): after approval the booking agent opens a
Checkout Session (``server.create_checkout``) and the run pauses. The customer pays on the
processor's own page. Nothing the browser says (the ``?checkout=success`` return) completes
anything: a ``checkout_pending`` row only moves when the processor's own record of the session
is read here, either from a signed ``checkout.session.*`` webhook (``apply_session``, body
verified by ``events.ingest``) or on demand (``refresh``: ``GET /v1/checkout/sessions/{id}``),
which serves local development without a webhook forwarder.

Rules, in order:

- the session must be the one recorded for the run (session id, ``client_reference_id`` and
  ``metadata.run_id`` all equal), else nothing changes (``reference_mismatch``);
- ``status=complete`` and ``payment_status=paid`` with ``amount_total``/``currency`` equal to the
  ledger's approved amount: ``captured`` with the PaymentIntent id and its public receipt URL;
- the same with another amount or currency: ``amount_mismatch``, never captured;
- ``status=expired``: ``checkout_expired`` (``checkout_canceled`` when the customer cancelled and
  ``cancel`` closed the session).

Every transition is ``Ledger.transition`` (conditional UPDATE), so a webhook racing a refresh
captures once; the caller learns whether it made the change (``changed``) and audits only then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from regulated_agent_lab.services.payments import processor
from regulated_agent_lab.services.payments import server as payments

# Outcome states (ledger statuses, plus "pending" and "not_found").
CAPTURED = payments.CAPTURED
EXPIRED = payments.CHECKOUT_EXPIRED
CANCELED = payments.CHECKOUT_CANCELED
MISMATCH = payments.AMOUNT_MISMATCH
PENDING = payments.CHECKOUT_PENDING
NOT_FOUND = "not_found"
TERMINAL = frozenset({CAPTURED, EXPIRED, CANCELED, MISMATCH})


@dataclass(frozen=True)
class CheckoutOutcome:
    """What the processor's record did to the run's checkout. No PII: ids, cents, URLs."""

    run_id: str | None
    state: str
    # True only for the call whose transition took effect (audit once).
    changed: bool
    session_id: str | None = None
    payment_id: str | None = None
    # What the processor reported (to show a mismatch without trusting it).
    amount_cents: int | None = None
    currency: str | None = None
    receipt_url: str | None = None
    reason: str | None = None

    def audit_data(self, source: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": source,
            "session_id": self.session_id,
            "payment_id": self.payment_id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
        }
        if self.reason:
            data["reason"] = self.reason
        return data


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _from_row(row: dict, *, changed: bool = False, reason: str | None = None) -> CheckoutOutcome:
    pid = row["payment_id"] if row["status"] == CAPTURED else None
    return CheckoutOutcome(
        run_id=row["idempotency_key"],
        state=row["status"],
        changed=changed,
        session_id=row.get("checkout_session_id"),
        payment_id=pid,
        amount_cents=row["amount_cents"],
        currency=str(row["currency"]).upper(),
        receipt_url=row.get("receipt_url"),
        reason=reason,
    )


def _intent_id(session: dict) -> tuple[str | None, dict | None]:
    intent = session.get("payment_intent")
    if isinstance(intent, dict):
        pid = intent.get("id")
        return (pid if isinstance(pid, str) else None), intent
    return (intent if isinstance(intent, str) else None), None


def _receipt(intent_id: str, expanded: dict | None, transport: Any) -> str | None:
    """Best effort: a missing receipt never blocks the capture (the payment happened)."""
    if expanded is not None and processor.receipt_url(expanded):
        return processor.receipt_url(expanded)
    try:
        return processor.receipt_url(processor.retrieve(intent_id, transport=transport))
    except (
        processor.PaymentNotFound,
        processor.ProcessorUnavailable,
        processor.ProcessorNotConfigured,
    ):
        return None


def apply_session(
    session: dict, *, transport: Any = None, expired_state: str = EXPIRED
) -> CheckoutOutcome:
    """Apply the processor's record of one Checkout Session to the ledger (see module doc)."""
    session_id = session.get("id")
    if not isinstance(session_id, str):
        return CheckoutOutcome(run_id=None, state=NOT_FOUND, changed=False)
    if session.get("livemode") is not False:
        raise processor.ProcessorNotConfigured("checkout session is not in test mode")
    store = payments.ledger()
    row = store.get_by_session_id(session_id)
    if row is None:
        return CheckoutOutcome(run_id=None, state=NOT_FOUND, changed=False, session_id=session_id)
    if row["status"] != PENDING:
        return _from_row(row)
    run_id = row["idempotency_key"]
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    if session.get("client_reference_id") != run_id or metadata.get("run_id") != run_id:
        return _from_row(row, reason="reference_mismatch")

    amount, currency = _int(session.get("amount_total")), session.get("currency")
    currency = currency.upper() if isinstance(currency, str) else None
    reported = {"amount_cents": amount, "currency": currency, "session_id": session_id}
    status, paid = session.get("status"), session.get("payment_status") == "paid"
    if status == "complete" and paid:
        intent_id, expanded = _intent_id(session)
        if amount != row["amount_cents"] or currency != str(row["currency"]).upper():
            after, changed = store.transition(run_id, PENDING, MISMATCH)
            outcome = _from_row(after or row, changed=changed, reason="amount_mismatch")
            return CheckoutOutcome(**{**outcome.__dict__, **reported, "payment_id": intent_id})
        if intent_id is None:
            return _from_row(row, reason="no payment intent")
        receipt = _receipt(intent_id, expanded, transport)
        after, changed = store.transition(
            run_id, PENDING, CAPTURED, payment_id=intent_id, receipt_url=receipt
        )
        return _from_row(after or row, changed=changed)
    if status == "expired":
        after, changed = store.transition(run_id, PENDING, expired_state)
        return _from_row(after or row, changed=changed)
    # Open, or complete but not yet paid (an asynchronous payment method): still pending.
    return _from_row(row)


def refresh(run_id: str, *, transport: Any = None) -> CheckoutOutcome:
    """Read the run's session back from the processor and apply it (on demand, no webhook)."""
    row = payments.ledger().get(run_id)
    if row is None or not row.get("checkout_session_id"):
        return CheckoutOutcome(run_id=run_id, state=NOT_FOUND, changed=False)
    if row["status"] != PENDING:
        return _from_row(row)
    session = processor.retrieve_checkout_session(row["checkout_session_id"], transport=transport)
    return apply_session(session, transport=transport)


def cancel(run_id: str, *, transport: Any = None) -> CheckoutOutcome:
    """The customer gave up: close the session at the processor, then apply what it says.

    A session that was paid in the meantime cannot be closed; it is then captured as usual.
    """
    row = payments.ledger().get(run_id)
    if row is None or not row.get("checkout_session_id"):
        return CheckoutOutcome(run_id=run_id, state=NOT_FOUND, changed=False)
    if row["status"] != PENDING:
        return _from_row(row)
    session_id = row["checkout_session_id"]
    closed = processor.expire_checkout_session(session_id, transport=transport)
    session = closed or processor.retrieve_checkout_session(session_id, transport=transport)
    return apply_session(session, transport=transport, expired_state=CANCELED)


__all__ = [
    "CANCELED",
    "CAPTURED",
    "EXPIRED",
    "MISMATCH",
    "NOT_FOUND",
    "PENDING",
    "TERMINAL",
    "CheckoutOutcome",
    "apply_session",
    "cancel",
    "refresh",
]
