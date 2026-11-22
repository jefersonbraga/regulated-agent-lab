"""Booking agent: after human approval, holds the chosen seats, then pays the approved quote.

Tools: travel_search.hold_seat, payments.charge, payments.create_checkout and
wallet.issue_payment_token (all approval-gated by policy), payments.checkout_status (read-only).

Payment models (``PAYMENTS_FLOW``, see ``payments_flow()`` / ``payment_models()``), chosen per
run by the human in the approval decision (``payment_model``) and bound into its signed token:

- ``agent`` (the simulated provider only): ``payments.charge`` records the capture in the
  ledger. No processor, no card.
- ``checkout`` (test-mode processor): ``payments.create_checkout`` opens a hosted Checkout
  Session for exactly the approved total and the run pauses at a second interrupt
  (``await_payment``, status ``awaiting_payment``) carrying the payment page URL. The customer
  confirms (and authenticates, when strong customer authentication asks for it) on the
  processor's page: the agent never confirms a payment on the customer's behalf. The API
  resumes the run only after the processor's own record (signed webhook or an on-demand read)
  moved the ledger; this node then reads the ledger through the gate
  (``payments.checkout_status``) and completes only on a capture of the approved amount.
- ``wallet`` (test-mode processor): the agent pays from the user's wallet. It asks the wallet
  institution for a scoped token (``wallet.issue_payment_token``: single use, exactly the
  approved amount, this merchant and run, 15 minutes), then ``payments.charge`` redeems it at
  the wallet and charges the saved card off-session. The card never reaches this agent, the
  model or the audit log: the agent holds an opaque token for one call. When the wallet cannot
  pay (no saved card, card expired, above the user's mandate) or the bank asks for
  authentication (``authentication_required``) or declines, the run falls back to the hosted
  Checkout for the same amount, and says why (``payment_fallback``).

``PAYMENTS_FLOW`` sets what is allowed with the test-mode processor: ``checkout``, ``wallet`` or
``both`` (the default); ``agent`` is a legacy alias of ``wallet``. The simulated provider is
always ``agent``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastmcp.exceptions import ToolError
from langgraph.types import interrupt

from regulated_agent_lab.agents.runtime import NodeContext
from regulated_agent_lab.agents.state import LabState, compute_quote_hash
from regulated_agent_lab.agents.tools import UnauditedToolResult

AGENT = "booking"
HOLD = "travel_search.hold_seat"
CHARGE = "payments.charge"
CREATE_CHECKOUT = "payments.create_checkout"
CHECKOUT_STATUS = "payments.checkout_status"
WALLET_TOKEN = "wallet.issue_payment_token"
# The merchant this agent platform sells for: the wallet scopes its tokens to it.
MERCHANT_ID = "demo-rail-lab"
# Whose wallet pays when the API runs without authentication (no signed-in requester).
LOCAL_PAYER = "local-dev"

FLOW_ENV = "PAYMENTS_FLOW"
FLOWS = ("checkout", "wallet", "both", "agent")
# Payment interrupt payload keys (the approval interrupt has none of them).
CHECKOUT_KEYS = ("session_id", "checkout_url", "expires_at", "amount_cents", "currency")
# Wallet answers that send the run to the hosted Checkout instead (reason as recorded).
_CHARGE_FALLBACKS = {
    "authentication_required": "authentication_required",
    "declined": "card_declined",
}


def payments_flow() -> str:
    """``agent`` (simulated provider), else ``checkout``, ``wallet`` or ``both`` (default)."""
    raw = (os.environ.get(FLOW_ENV) or "").strip().lower()
    if raw and raw not in FLOWS:
        raise ValueError(f"{FLOW_ENV} must be one of {', '.join(FLOWS[:3])}")
    if os.environ.get("PAYMENTS_PROVIDER", "simulated") != "stripe_test":
        return "agent"
    if raw == "agent":  # the old fixed-test-card flow is gone: the agent now pays via the wallet
        return "wallet"
    return raw or "both"


def payment_models() -> tuple[str, ...]:
    """The payment models a human may choose now (``ValueError`` for a bad ``PAYMENTS_FLOW``)."""
    flow = payments_flow()
    if flow == "both":
        return ("checkout", "wallet")
    return (flow,)


def default_model(models: tuple[str, ...]) -> str | None:
    """The model used when a decision names none: the hosted page when allowed (the customer
    authenticates), else the only one. The API proposes the wallet when it can pay."""
    if not models:
        return None
    return "checkout" if "checkout" in models else models[0]


def payer_for(state: LabState) -> str:
    """Whose wallet pays: the user who asked for the trip (the customer)."""
    return state.get("requester") or LOCAL_PAYER


def passenger_ref(run_id: str, index: int = 0) -> str:
    """Pseudonymous passenger reference: no personal data leaves the lab.

    The first passenger is ``pax-<run_id>``; further passengers get ``pax-<run_id>-<index>``.
    """
    return f"pax-{run_id}" if index == 0 else f"pax-{run_id}-{index}"


def earliest_hold_expiry(holds: list[dict]) -> datetime | None:
    """Earliest ``expires_at`` of the holds; None when any is missing or unreadable.

    None means the holds cannot be shown to be live, so callers treat it as expired.
    """
    if not holds:
        return None
    expiries = []
    for h in holds:
        try:
            expires = datetime.fromisoformat(h["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if expires.tzinfo is None:
            return None
        expiries.append(expires)
    return min(expiries)


def holds_live(holds: list[dict], now: datetime | None = None) -> bool:
    """True only while every hold is still reserved (expiry strictly after ``now``)."""
    expiry = earliest_hold_expiry(holds)
    return expiry is not None and expiry > (now or datetime.now(UTC))


def _passengers(state: LabState) -> int:
    intent = state["intent"]
    return intent.trip.passengers if intent is not None and intent.trip is not None else 1


async def hold(state: LabState, ctx: NodeContext) -> dict:
    quote, approval = state["quote"], state["approval"]
    assert quote is not None
    # Seats are held only for the selection the human approved (the quote approval set).
    if not approval or not approval.get("approved"):
        ctx.record(AGENT, "hold_blocked", {"reason": "no approval"})
        return {"status": "refused"}
    if approval.get("quote_hash") != compute_quote_hash(
        quote.options, quote.total_cents, quote.currency
    ):
        ctx.record(AGENT, "hold_blocked", {"reason": "quote does not match approval"})
        return {"status": "refused"}
    holds = []
    # One seat per passenger per leg: the quote total is per-leg price x passengers.
    for option in quote.options:
        for index in range(_passengers(state)):
            args = {
                "fare_id": option.fare_id,
                "passenger_ref": passenger_ref(state["run_id"], index),
            }
            try:
                guard = await ctx.call(HOLD, args)
            except ToolError:
                # No release tool exists; holds already placed expire on their own (TTL).
                # Record them so the audit trail shows what is left behind.
                placed = [h.get("hold_id") for h in holds if isinstance(h, dict)]
                ctx.record(
                    AGENT, "hold_failed", {"fare_id": option.fare_id, "placed_hold_ids": placed}
                )
                return {"status": "failed"}
            holds.append(guard.sanitized)
    held = sum(h.get("price_cents", 0) for h in holds if isinstance(h, dict))
    if held != quote.total_cents:
        # The seats would cost something other than what the human approved: never charge.
        ctx.record(AGENT, "hold_mismatch", {"held_cents": held, "quote_cents": quote.total_cents})
        return {"holds": holds, "status": "refused"}
    return {"holds": holds}


_PROCESSOR_FAULTS = ("payment processor unavailable", "payments provider not configured")


def _processor_fault(exc: ToolError) -> bool:
    """A processor outage or misconfiguration, as opposed to a refusal by the gate or wallet."""
    return any(fault in str(exc) for fault in _PROCESSOR_FAULTS)


async def pay(state: LabState, ctx: NodeContext) -> dict:
    quote, approval = state["quote"], state["approval"]
    assert quote is not None and approval is not None
    # The amount charged is bound to the approved quote: recompute the hash from the
    # quote we are about to charge and compare with what the human approved.
    current_hash = compute_quote_hash(quote.options, quote.total_cents, quote.currency)
    if not approval.get("approved") or approval.get("quote_hash") != current_hash:
        ctx.record(AGENT, "charge_blocked", {"reason": "quote does not match approval"})
        return {"status": "refused"}
    # Every passenger must have a held seat on every leg, and the holds must sum to the
    # exact amount about to be charged (no multiplier: one hold is one seat).
    holds = state["holds"]
    held = sum(h.get("price_cents", 0) for h in holds)
    if len(holds) != len(quote.options) * _passengers(state) or held != quote.total_cents:
        ctx.record(AGENT, "charge_blocked", {"reason": "holds do not match quote total"})
        return {"status": "refused"}
    # Never capture for seats that are no longer reserved: an approval can arrive long after
    # the holds were placed, and the holds expire on their own (TTL).
    if not holds_live(holds):
        ctx.record(AGENT, "charge_blocked", {"reason": "seat holds expired"})
        return {"status": "refused"}

    args = {
        "hold_id": ",".join(h["hold_id"] for h in state["holds"]),
        "amount_cents": quote.total_cents,
        "currency": quote.currency,
        "idempotency_key": state["run_id"],
        "approval_ref": f"{approval['approver']}:{approval['quote_hash']}",
    }
    if os.environ.get("PAYMENTS_PROVIDER", "simulated") != "stripe_test":
        try:
            guard = await ctx.call(CHARGE, args, approval_token=approval.get("token"))
        except ToolError:
            return {"status": "refused"}
        except UnauditedToolResult as exc:
            # The capture happened but its tool_result could not be audited (flagged by the
            # context as audit_failed:payments.charge): the run must still show the payment.
            return {"payment": exc.guard.sanitized, "status": "completed", "payment_model": "agent"}
        return {"payment": guard.sanitized, "status": "completed", "payment_model": "agent"}
    if approval.get("payment_model") == "wallet":
        return await _pay_from_wallet(state, ctx, args, current_hash)
    update = await _open_checkout(state, ctx, {**args, "quote_hash": current_hash})
    return {**update, "payment_model": "checkout"}


async def _fallback(
    state: LabState, ctx: NodeContext, args: dict, quote_hash: str, reason: str
) -> dict:
    """The wallet could not pay: open the hosted Checkout for the same amount, and say why."""
    ctx.record("wallet", "wallet_fallback", {"reason": reason, "to": "checkout"})
    update = await _open_checkout(state, ctx, {**args, "quote_hash": quote_hash})
    return {**update, "payment_model": "checkout", "payment_fallback": reason}


async def _pay_from_wallet(state: LabState, ctx: NodeContext, args: dict, quote_hash: str) -> dict:
    """Scoped token from the user's wallet, then an off-session charge that redeems it once.

    The token lives only in this function: it is never put in the run state (checkpoints), and
    the audit log redacts it (``regulated_agent_lab.controls.redaction.SECRET_KEYS``).
    """
    quote, approval = state["quote"], state["approval"]
    assert quote is not None and approval is not None
    token = approval.get("token")
    request = {
        "wallet_user": approval.get("payer") or payer_for(state),
        "amount_cents": quote.total_cents,
        "currency": quote.currency,
        "run_id": state["run_id"],
        "merchant_id": MERCHANT_ID,
        "approval_ref": args["approval_ref"],
    }
    try:
        issued = (await ctx.call(WALLET_TOKEN, request, approval_token=token)).sanitized
    except ToolError:
        return {"status": "refused", "payment_model": "wallet"}
    except UnauditedToolResult as exc:
        issued = exc.guard.sanitized
    if not isinstance(issued, dict):
        return {"status": "failed", "payment_model": "wallet"}
    if issued.get("issued") is not True:
        reason = issued.get("reason")
        return await _fallback(state, ctx, args, quote_hash, str(reason or "wallet_unavailable"))
    if (
        issued.get("amount_cents") != quote.total_cents
        or issued.get("currency") != quote.currency
        or issued.get("run_id") != state["run_id"]
        or issued.get("merchant_id") != MERCHANT_ID
        or not isinstance(issued.get("payment_token"), str)
    ):
        ctx.record(AGENT, "charge_blocked", {"reason": "wallet token does not match the quote"})
        return {"status": "refused", "payment_model": "wallet"}
    token_id = issued.get("token_id")
    ctx.record(
        "wallet",
        "wallet_token_issued",
        {
            "token_id": token_id,
            "amount_cents": issued["amount_cents"],
            "currency": issued["currency"],
            "merchant_id": issued["merchant_id"],
            "expires_at": issued.get("expires_at"),
            "mandate_cents": issued.get("mandate_cents"),
        },
    )
    charge = {**args, "payment_token": issued["payment_token"], "quote_hash": quote_hash}
    try:
        payment = (await ctx.call(CHARGE, charge, approval_token=token)).sanitized
    except ToolError as exc:
        if _processor_fault(exc):
            # The processor did not answer usefully: a technical failure, not a refusal.
            ctx.record(AGENT, "payment_failed", {"reason": "payment processor unavailable"})
            return {"status": "failed", "payment_model": "wallet"}
        # Refused at the wallet (replay, mismatch, expiry): misuse, nothing charged.
        return {"status": "refused", "payment_model": "wallet"}
    except UnauditedToolResult as exc:
        payment = exc.guard.sanitized
    status = payment.get("status") if isinstance(payment, dict) else None
    if status in _CHARGE_FALLBACKS:
        return await _fallback(state, ctx, args, quote_hash, _CHARGE_FALLBACKS[status])
    if status != "captured" or not isinstance(payment, dict):
        return {"status": "failed", "payment_model": "wallet"}
    paid = (payment.get("amount_cents"), payment.get("currency"))
    if paid != (quote.total_cents, quote.currency):
        ctx.flags.append("amount_mismatch")
        ctx.record(AGENT, "payment_blocked", {"reason": "capture does not match the quote"})
        return {"status": "refused", "payment_model": "wallet"}
    ctx.record(
        "wallet",
        "wallet_token_redeemed",
        {
            "token_id": token_id,
            "payment_id": payment.get("payment_id"),
            "amount_cents": payment["amount_cents"],
            "currency": payment["currency"],
        },
    )
    return {"payment": payment, "status": "completed", "payment_model": "wallet"}


async def _open_checkout(state: LabState, ctx: NodeContext, args: dict) -> dict:
    """Open the hosted Checkout for the approved total; the run then waits for the customer."""
    quote, approval = state["quote"], state["approval"]
    assert quote is not None and approval is not None
    try:
        guard = await ctx.call(CREATE_CHECKOUT, args, approval_token=approval.get("token"))
        result = guard.sanitized
    except ToolError:
        return {"status": "refused"}
    except UnauditedToolResult as exc:
        result = exc.guard.sanitized
    if not isinstance(result, dict):
        return {"status": "failed"}
    if result.get("amount_cents") != quote.total_cents or result.get("currency") != quote.currency:
        ctx.record(AGENT, "checkout_blocked", {"reason": "checkout does not match the quote"})
        return {"status": "refused"}
    if result.get("status") == "captured":  # already paid (a resumed run): nothing to wait for
        return {"payment": result, "status": "completed"}
    if result.get("status") != "checkout_pending" or not all(k in result for k in CHECKOUT_KEYS):
        return {"status": "failed"}
    checkout = {k: result[k] for k in CHECKOUT_KEYS}
    ctx.record(
        AGENT,
        "checkout_created",
        {k: checkout[k] for k in ("session_id", "expires_at", "amount_cents", "currency")},
    )
    return {"checkout": checkout, "status": "awaiting_payment"}


# Ledger outcomes that end a checkout without a capture -> run status and audited reason.
_CHECKOUT_ENDINGS = {
    "checkout_expired": ("failed", "checkout expired"),
    "checkout_canceled": ("failed", "checkout canceled"),
    "amount_mismatch": ("refused", "amount_mismatch"),
}


async def await_payment(state: LabState, ctx: NodeContext) -> dict:
    """Pause until the processor reported the checkout, then complete from the ledger only.

    No side effects before ``interrupt()``: the node is re-executed on resume. The resume value
    (which API path woke the run) is never trusted for the outcome: the ledger is read through
    the gate. Still pending (a spurious resume): back to waiting.
    """
    checkout = state.get("checkout")
    quote = state["quote"]
    assert checkout is not None and quote is not None
    interrupt({k: checkout.get(k) for k in CHECKOUT_KEYS})

    guard = await ctx.call(CHECKOUT_STATUS, {"idempotency_key": state["run_id"]})
    record = guard.sanitized if isinstance(guard.sanitized, dict) else {}
    status = record.get("status")
    if status == "captured":
        if record.get("amount_cents") != quote.total_cents or record.get("currency") != (
            quote.currency
        ):
            ctx.flags.append("amount_mismatch")
            ctx.record(AGENT, "payment_blocked", {"reason": "capture does not match the quote"})
            return {"status": "refused"}
        # Checkout pages live at least 30 minutes, seat holds 15: a late payment is flagged
        # for the operator (re-hold or refund); the money did move, so the run records it.
        if not holds_live(state["holds"]):
            ctx.flags.append("paid_after_hold_expiry")
            ctx.record(AGENT, "paid_after_hold_expiry", {"payment_id": record.get("payment_id")})
        return {"payment": record, "status": "completed"}
    if status in _CHECKOUT_ENDINGS:
        run_status, reason = _CHECKOUT_ENDINGS[status]
        if status == "amount_mismatch":
            ctx.flags.append("amount_mismatch")
        ctx.record(AGENT, "checkout_ended", {"reason": reason})
        return {"status": run_status}
    return {"status": "awaiting_payment"}
