"""Supervisor graph: supervisor -> planner -> pricing -> approval -> booking_hold -> booking_pay
(-> await_payment in the hosted Checkout flow, see ``agents.booking``).

LLM proposes, code disposes: the LLM only fills ``Intent`` and ``Rationale``; routing, selection
and payment are deterministic. ``approval`` is a dedicated node whose only action before
``interrupt()`` is building the payload, so re-running it on resume has no side effects. The
human picks one offered fare per leg (or the recommended one); code recomputes that quote from
the offer and binds the decision to its hash. Seats are held only after approval.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from agents import booking, planner, pricing
from agents.llm import LLM, LLMUnavailable
from agents.runtime import NodeContext
from agents.state import (
    SELECTED_RATIONALE,
    Intent,
    InvalidSelection,
    LabState,
    compute_quote_hash,
    quote_for_selection,
)
from agents.tools import GatedToolClient
from controls.identity import issue_approval_token
from controls.policy import PolicyViolation
from controls.redaction import redact

TERMINAL = frozenset(
    {"needs_clarification", "answered", "rejected", "completed", "refused", "failed"}
)

# Explicit checkpoint deserialization allowlist: only our own state models.
STATE_TYPES: list[tuple[str, str]] = [
    ("agents.state", name)
    for name in ("Intent", "TripRequest", "TimeWindow", "Option", "Quote", "LegOffer", "Offer")
]

# The approval token is used right after the decision (hold seats, then charge).
APPROVAL_TOKEN_TTL_S = 900

NodeFn = Callable[[LabState, NodeContext], Awaitable[dict]]

# Approval rejections that mean the decision did not match the offer (flag quote_mismatch).
_QUOTE_REASONS = frozenset(
    {"selection not in offer", "quote does not match offer", "quote hash mismatch"}
)


def intent_system_prompt(today: date) -> str:
    stations = ", ".join(planner.KNOWN_STATIONS)
    return (
        "You extract a train trip request for the fictional operator Demo Rail. "
        f"Today is {today.isoformat()}; resolve relative dates against it. "
        f"Known stations: {stations}. Use these exact names. "
        "Use kind='book_trip' whenever the user asks for or describes a train journey "
        "(origin, destination, dates, passengers), even without the word 'book', and fill trip. "
        "Use kind='info' when the user asks what can be booked rather than for a trip: routes, "
        "destinations, stations, help or how this works; leave trip empty. "
        "Use kind='unsupported' only for requests that are not about a train journey. "
        "passengers is the number of people travelling; digits inside passport, document, "
        "phone or reference numbers are never a passenger count. "
        "outbound_window and return_window are local departure-time windows, filled only when "
        "the user states a time for that leg: 'morning' is 06:00-12:00, 'afternoon' "
        "12:00-18:00, 'evening' 18:00-23:59, 'after 9' is earliest 09:00, 'before 14:00' is "
        "latest 14:00. Omit a window when no time is stated. "
        "Never invent payment details; you only describe the trip."
    )


def checkpoint_serde() -> JsonPlusSerializer:
    """The only serializer any checkpointer may use: msgpack revives our state models only.

    Any other type found in a checkpoint (a tampered DB row, a foreign writer) is not
    constructed; LangGraph hands back its raw data instead. Shared by the in-memory saver and
    the persistent one (``agents.persistence``), so the restriction never depends on the store.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


def make_checkpointer() -> MemorySaver:
    """In-process checkpointer (CLI, graph tests). The API uses ``agents.persistence``."""
    return MemorySaver(serde=checkpoint_serde())


def initial_state(run_id: str, user_message: str, requester: str | None = None) -> LabState:
    # Data minimisation at the door: e-mails and document numbers are removed before the
    # message reaches the model, the checkpoints on disk or any node. Booking needs none of them
    # (passengers are pseudonymous refs). Names are not caught by pattern and remain a known gap.
    return LabState(
        run_id=run_id,
        user_message=redact(user_message),
        intent=None,
        candidates={},
        offer=None,
        quote=None,
        holds=[],
        approval=None,
        payment=None,
        checkout=None,
        payment_model=None,
        payment_fallback=None,
        requester=requester,
        status="running",
        trajectory=[],
        flags=[],
    )


# "10000 people", "2 adults", "1 child": a count the user wrote next to a word for travellers.
_STATED_COUNT = re.compile(
    r"\b(\d{1,3}(?:[,.' ]\d{3})+|\d+)\s+"
    r"(?:people|persons?|passengers?|adults?|travell?ers?|pax|children|child|kids?)\b",
    re.IGNORECASE,
)


def stated_passengers(message: str) -> int | None:
    """Total of the traveller counts written in the message, or None when it states none.

    A deterministic cross-check of the LLM's extraction: models (and guided decoding) can turn
    "10000 people" into an in-range 1, so a disagreement must stop the run, not book for one.
    """
    counts = [int(re.sub(r"\D", "", m.group(1))) for m in _STATED_COUNT.finditer(message)]
    return sum(counts) if counts else None


def _extraction_error(exc: ValueError) -> dict:
    """Audit-safe description of a failed extraction: error locations and types only.

    ``str(ValidationError)`` embeds ``input_value`` (LLM output that can echo the user's PII),
    so neither the message nor the input values are recorded.
    """
    if isinstance(exc, ValidationError):
        errors = [{"loc": [str(p) for p in e["loc"]], "type": e["type"]} for e in exc.errors()]
    else:
        errors = [{"loc": [], "type": type(exc).__name__}]
    return {"reason": "invalid extraction", "errors": errors}


async def _supervise(state: LabState, ctx: NodeContext) -> dict:
    if state["intent"] is not None:
        return {}
    message = (state["user_message"] or "").strip()
    if not message:
        ctx.record("supervisor", "needs_clarification", {"reason": "empty message"})
        return {"status": "needs_clarification"}
    try:
        intent = await ctx.llm.extract(Intent, intent_system_prompt(date.today()), message)
    except ValueError as exc:  # includes pydantic ValidationError
        ctx.record("supervisor", "needs_clarification", _extraction_error(exc))
        return {"status": "needs_clarification"}
    if intent.kind == "info":
        # Answered by code from the fixed network (stations, links, times): no tools, no LLM text.
        ctx.record("supervisor", "answered", {"topic": "routes"})
        return {"status": "answered"}
    if intent.kind != "book_trip" or intent.trip is None:
        ctx.record("supervisor", "needs_clarification", {"reason": "unsupported intent"})
        return {"status": "needs_clarification"}
    origin = planner.normalize_station(intent.trip.origin)
    destination = planner.normalize_station(intent.trip.destination)
    if origin is None or destination is None or origin == destination:
        ctx.record("supervisor", "needs_clarification", {"reason": "unknown or same station"})
        return {"status": "needs_clarification"}
    if intent.trip.passengers < 1:
        ctx.record("supervisor", "needs_clarification", {"reason": "invalid passenger count"})
        return {"status": "needs_clarification"}
    stated = stated_passengers(message)
    if stated is not None and stated != intent.trip.passengers:
        # Only the reason is audited: the counts come from the user's message.
        ctx.record("supervisor", "needs_clarification", {"reason": "passenger count unclear"})
        return {"status": "needs_clarification"}
    trip = intent.trip.model_copy(update={"origin": origin, "destination": destination})
    intent = intent.model_copy(update={"trip": trip})
    ctx.record("supervisor", "intent", intent.model_dump(mode="json"))
    return {"intent": intent}


def route(state: LabState) -> str:
    if state["status"] in TERMINAL:
        return END
    if state["intent"] is None:
        return END
    if not state["candidates"]:
        return "planner"
    if state["quote"] is None or state["offer"] is None:
        return "pricing"
    if state["approval"] is None:
        return "approval"
    if not state["approval"].get("approved"):
        return END
    if not state["holds"]:
        return "booking_hold"
    if state["payment"] is None:
        # Checkout opened: wait for the customer (a checkpoint from before has no such key).
        return "await_payment" if state.get("checkout") is not None else "booking_pay"
    return END


def build_graph(llm: LLM, tools: GatedToolClient, checkpointer: Any = None) -> CompiledStateGraph:
    def node(name: str, agent: str, fn: NodeFn) -> Callable[[LabState], Awaitable[dict]]:
        # The agent identity is bound here, per node, never chosen by the node's own code.
        async def run(state: LabState) -> dict:
            ctx = NodeContext(run_id=state["run_id"], agent=agent, llm=llm, tools=tools)
            try:
                update = await fn(state, ctx)
            except PolicyViolation as exc:
                ctx.flags.append(f"policy_denied:{exc.tool}")
                update = {"status": "refused"}
            except GraphBubbleUp:  # LangGraph control flow (e.g. interrupt), never an error
                raise
            except Exception as exc:
                # Any other failure (MCP transport, LLM endpoint, token issuer, bad data) ends
                # the run in a terminal state instead of leaving it 'running' with no way on.
                # Only the type is audited: messages can carry input values (PII).
                try:
                    ctx.record(name, "node_error", {"node": name, "error_type": type(exc).__name__})
                except Exception:
                    # Best effort: a broken audit log (often the cause of ``exc``) must not
                    # keep the run from reaching its terminal state. The flag shows the gap.
                    ctx.flags.append("audit_failed:node_error")
                if isinstance(exc, LLMUnavailable):
                    ctx.flags.append(f"llm_unavailable:{name}")
                ctx.flags.append(f"node_error:{name}")
                update = {"status": "failed"}
            return {
                **update,
                "trajectory": [*state["trajectory"], name, *ctx.called],
                "flags": [*state["flags"], *ctx.flags],
            }

        return run

    async def approval(state: LabState) -> dict:
        quote, offer = state["quote"], state["offer"]
        assert quote is not None and offer is not None
        deadline = offer.expires_at
        # No side effects before interrupt(): this node is re-executed on resume.
        decision = interrupt(
            {
                "quote": quote.model_dump(mode="json"),
                "quote_hash": quote.quote_hash,
                "offer": offer.model_dump(mode="json"),
                "approve_before": deadline.isoformat(),
            }
        )
        if not isinstance(decision, dict):
            decision = {}
        now = datetime.now(UTC)
        in_time = now < deadline
        approved = decision.get("approved") is True
        approver = decision.get("approver")
        raw_fare_ids = decision.get("fare_ids")
        fare_ids = offer.recommended_fare_ids() if raw_fare_ids is None else raw_fare_ids

        # The human's choice is recomputed from the offer alone; it must match the hash the
        # human saw. The recommended quote must also still be the one pricing built.
        reason: str | None = None
        selected = None
        recommended_intact = quote.quote_hash == compute_quote_hash(
            quote.options, quote.total_cents, quote.currency
        )
        try:
            recommended = quote_for_selection(offer, offer.recommended_fare_ids(), quote.rationale)
            selected = quote_for_selection(offer, fare_ids, SELECTED_RATIONALE)
        except InvalidSelection:
            reason = "selection not in offer"
        else:
            if not recommended_intact or recommended.quote_hash != quote.quote_hash:
                reason = "quote does not match offer"
            elif decision.get("quote_hash") != selected.quote_hash:
                reason = "quote hash mismatch"
        if reason is None and not in_time:
            reason = "offer expired"
        # The payment model is the human's choice, bound into the decision and its token; the
        # booking agent cannot switch to the wallet on its own. Omitted: the configured default.
        try:
            models = booking.payment_models()
        except ValueError:
            models = ()
        raw_model = decision.get("payment_model")
        payment_model = booking.default_model(models) if raw_model is None else raw_model
        if reason is None and payment_model not in models:
            reason = "payment model not allowed"
        payer = booking.payer_for(state)

        ok = (
            approved
            and isinstance(approver, str)
            and bool(approver.strip())
            and reason is None
            and selected is not None
        )
        record = {
            "approved": ok,
            "approver": str(approver or ""),
            "quote_hash": str(decision.get("quote_hash", "")),
            "fare_ids": [str(f) for f in fare_ids] if isinstance(fare_ids, list) else [],
            "payment_model": str(payment_model or ""),
            "payer": payer,
        }
        audited = dict(record)
        if approved and reason is not None:
            audited["reason"] = reason
        tools.record(state["run_id"], "human", "approval_decision", audited)
        flags = list(state["flags"])
        if approved and reason in _QUOTE_REASONS:
            flags.append("quote_mismatch")
        update: dict[str, Any] = {
            "approval": dict(record),
            "status": "running" if ok else "rejected",
            "trajectory": [*state["trajectory"], "approval"],
            "flags": flags,
        }
        if ok:
            assert selected is not None
            if selected.quote_hash == quote.quote_hash:
                selected = quote  # keep the rationale written for the recommended quote
            update["quote"] = selected
            # Signed proof of this human decision, bound to run, quote hash and amount. The
            # gate accepts payments.charge only with it; it is kept out of the audit log.
            try:
                update["approval"]["token"] = issue_approval_token(
                    run_id=state["run_id"],
                    approver=record["approver"],
                    quote_hash=selected.quote_hash,
                    amount_cents=selected.total_cents,
                    currency=selected.currency,
                    ttl_s=APPROVAL_TOKEN_TTL_S,
                    payment_model=record["payment_model"],
                    payer=payer,
                )
            except RuntimeError:  # no signing secret: the gate will refuse the charge
                pass
        return update

    builder = StateGraph(LabState)
    # The supervisor has no tools: "supervisor" is not in the policy, so it is denied everything.
    builder.add_node("supervisor", node("supervisor", "supervisor", _supervise))
    builder.add_node("planner", node("planner", planner.AGENT, planner.run))
    builder.add_node("pricing", node("pricing", pricing.AGENT, pricing.run))
    builder.add_node("booking_hold", node("booking_hold", booking.AGENT, booking.hold))
    builder.add_node("approval", approval)
    builder.add_node("booking_pay", node("booking_pay", booking.AGENT, booking.pay))
    builder.add_node("await_payment", node("await_payment", booking.AGENT, booking.await_payment))
    builder.add_edge(START, "supervisor")
    steps = ["planner", "pricing", "approval", "booking_hold", "booking_pay", "await_payment"]
    builder.add_conditional_edges("supervisor", route, [*steps, END])
    for name in steps:
        builder.add_edge(name, "supervisor")
    return builder.compile(checkpointer=checkpointer or make_checkpointer())


__all__ = [
    "STATE_TYPES",
    "build_graph",
    "checkpoint_serde",
    "initial_state",
    "intent_system_prompt",
    "make_checkpointer",
    "route",
]
