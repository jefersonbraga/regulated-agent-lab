"""Reconciliation: compare the ledger with what the card processor says, payment by payment.

Webhooks can be late, lost or never configured; the processor's own record is the truth.
For every ``stripe_test`` capture in the ledger this reads the PaymentIntent back from the
processor (``GET /v1/payment_intents/{id}``, latest charge expanded, test-mode key only: read
access to PaymentIntents is enough) and compares status, amount and currency with the ledger
and with the events received. Simulated captures never reached a processor: they are skipped
and counted as such; so are hosted checkouts that never completed (``checkout_pending``,
expired, cancelled or refused for an amount mismatch: no capture to compare). Nothing here
writes to the ledger; the caller decides what to do with the report (the API mirrors
processor-confirmed settlements into its run history).

    uv run python -m mcp_servers.payments_sandbox.reconcile          # human-readable report
    uv run python -m mcp_servers.payments_sandbox.reconcile --json   # the report as JSON

The CLI appends one ``reconciliation_run`` entry with the counts to the audit log
(``AUDIT_LOG_PATH``, ``--no-audit`` to skip). Exit status: 0 no discrepancies, 1 some, 2 the
processor is not configured (no test-mode key). It never opens the agents' database: run
settlements in the UI follow on a webhook or on ``POST /reconcile``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mcp_servers.payments_sandbox import events, processor
from mcp_servers.payments_sandbox import server as payments

DISCREPANCIES = (
    "missing_at_processor",
    "amount_mismatch",
    "refunded",
    "disputed",
    "not_succeeded",
    "unconfirmed",
    "settlement_mismatch",
    "processor_unavailable",
)


class ReconciledPayment(BaseModel):
    """One ledger capture next to the processor's record of it (integer cents)."""

    run_id: str
    payment_id: str
    ledger_amount_cents: int
    ledger_currency: str
    processor_status: str | None = None
    processor_amount_cents: int | None = None
    processor_currency: str | None = None
    refunded_cents: int = 0
    # What the processor's record implies; None when it could not be read.
    processor_settlement: str | None = None
    # What the received events imply; None when no event was received.
    event_settlement: str | None = None
    events_seen: list[str] = []
    discrepancies: list[str] = []


class SkippedPayment(BaseModel):
    run_id: str
    payment_id: str
    provider: str
    reason: str


class ReconciliationReport(BaseModel):
    checked: int
    skipped_simulated: int
    ok: int
    counts: dict[str, int]
    payments: list[ReconciledPayment]
    skipped: list[SkippedPayment]

    def audit_data(self) -> dict[str, Any]:
        """The counts for the audit log: no payment ids, no amounts, no PII."""
        return {
            "checked": self.checked,
            "skipped_simulated": self.skipped_simulated,
            "ok": self.ok,
            "counts": dict(self.counts),
        }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def processor_settlement(intent: dict) -> str:
    """Settlement implied by a PaymentIntent with its latest charge expanded."""
    charge = intent.get("latest_charge")
    charge = charge if isinstance(charge, dict) else {}
    if charge.get("disputed") is True:
        return "disputed"
    if (_int_or_none(charge.get("amount_refunded")) or 0) > 0 or charge.get("refunded") is True:
        return "refunded"
    status = intent.get("status")
    if status == "succeeded":
        return "confirmed"
    if status in ("canceled", "requires_payment_method"):
        return "failed"
    return "pending"


def _check(record: dict, store: events.EventStore, transport: Any) -> ReconciledPayment:
    received = store.for_payment(record["payment_id"])
    seen = [e["type"] for e in received]
    item = ReconciledPayment(
        run_id=record["idempotency_key"],
        payment_id=record["payment_id"],
        ledger_amount_cents=record["amount_cents"],
        ledger_currency=record["currency"],
        event_settlement=events.derive_settlement(seen),
        events_seen=seen,
    )
    found: list[str] = []
    if "payment_intent.succeeded" not in seen and events.CHECKOUT_COMPLETED not in seen:
        found.append("unconfirmed")
    try:
        intent = processor.retrieve(record["payment_id"], transport=transport)
    except processor.PaymentNotFound:
        item.discrepancies = ["missing_at_processor", *found]
        return item
    except processor.ProcessorUnavailable:
        item.discrepancies = ["processor_unavailable", *found]
        return item
    charge = intent.get("latest_charge")
    charge = charge if isinstance(charge, dict) else {}
    item.processor_status = str(intent.get("status"))
    item.processor_amount_cents = _int_or_none(intent.get("amount"))
    currency = intent.get("currency")
    item.processor_currency = currency.upper() if isinstance(currency, str) else None
    item.refunded_cents = _int_or_none(charge.get("amount_refunded")) or 0
    item.processor_settlement = processor_settlement(intent)
    if (
        item.processor_amount_cents != record["amount_cents"]
        or item.processor_currency != str(record["currency"]).upper()
    ):
        found.insert(0, "amount_mismatch")
    if item.processor_status != "succeeded":
        found.append("not_succeeded")
    if item.processor_settlement == "refunded":
        found.append("refunded")
    if item.processor_settlement == "disputed":
        found.append("disputed")
        if item.refunded_cents > 0:
            found.append("refunded")
    # The events claim something the processor's own record does not show (or the reverse).
    if item.event_settlement is not None and item.event_settlement != item.processor_settlement:
        found.append("settlement_mismatch")
    item.discrepancies = [d for d in DISCREPANCIES if d in found]
    return item


def reconcile(transport: Any = None) -> ReconciliationReport:
    """Check every capture in the current ledger. Raises ``processor.ProcessorNotConfigured``
    (before any request) when there is no test-mode key and something needs checking."""
    store = events.event_store()
    checked: list[ReconciledPayment] = []
    skipped: list[SkippedPayment] = []
    records = list(payments.ledger().all().values())
    if any(r["provider"] == "stripe_test" and r["status"] == "captured" for r in records):
        processor.require_test_key()
    for record in records:
        if record["provider"] == "stripe_test" and record["status"] != "captured":
            skipped.append(
                SkippedPayment(
                    run_id=record["idempotency_key"],
                    payment_id=record["payment_id"],
                    provider=record["provider"],
                    reason=f"checkout not captured ({record['status']})",
                )
            )
            continue
        if record["provider"] != "stripe_test":
            skipped.append(
                SkippedPayment(
                    run_id=record["idempotency_key"],
                    payment_id=record["payment_id"],
                    provider=record["provider"],
                    reason="simulated capture: never sent to the processor",
                )
            )
            continue
        checked.append(_check(record, store, transport))
    counts = {d: sum(d in p.discrepancies for p in checked) for d in DISCREPANCIES}
    return ReconciliationReport(
        checked=len(checked),
        skipped_simulated=sum(s.provider != "stripe_test" for s in skipped),
        ok=sum(not p.discrepancies for p in checked),
        counts=counts,
        payments=checked,
        skipped=skipped,
    )


def _print_report(report: ReconciliationReport) -> None:
    print(
        f"Reconciliation: {report.checked} checked, {report.ok} ok, "
        f"{report.skipped_simulated} simulated skipped"
    )
    for p in report.payments:
        status = ", ".join(p.discrepancies) if p.discrepancies else "ok"
        print(
            f"  run {p.run_id}  {p.payment_id}  ledger {p.ledger_amount_cents} "
            f"{p.ledger_currency}  processor {p.processor_status or '-'} "
            f"{p.processor_amount_cents if p.processor_amount_cents is not None else '-'}  "
            f"settlement {p.processor_settlement or '-'}  -> {status}"
        )
    totals = {k: v for k, v in report.counts.items() if v}
    if totals:
        print("Discrepancies: " + ", ".join(f"{k}={v}" for k, v in totals.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--no-audit", action="store_true", help="do not append to the audit log")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        pass
    try:
        report = reconcile(transport=payments.PROCESSOR_TRANSPORT)
    except processor.ProcessorNotConfigured as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2
    if not args.no_audit:
        from controls.audit_log import AuditLog

        audit = AuditLog(Path(os.environ.get("AUDIT_LOG_PATH", "var/audit.jsonl")))
        audit.append("reconcile", "reconciliation", "reconciliation_run", report.audit_data())
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        _print_report(report)
    return 1 if report.ok < report.checked else 0


if __name__ == "__main__":
    raise SystemExit(main())
