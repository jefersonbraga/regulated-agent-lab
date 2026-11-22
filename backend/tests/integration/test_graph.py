"""Supervisor graph: trajectory, approval interrupt and payment invariants."""

import pytest
from langgraph.types import Command

from regulated_agent_lab.agents.graph import build_graph, initial_state
from regulated_agent_lab.agents.llm import LLMUnavailable, MockLLM
from regulated_agent_lab.agents.state import SELECTED_RATIONALE, Offer, Quote, quote_for_selection
from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.controls.audit_log import AuditLog
from regulated_agent_lab.controls.policy import ToolPolicy
from regulated_agent_lab.services.payments.server import LEDGER, reset_ledger

FIXTURES = {
    "Intent": {
        "kind": "book_trip",
        "trip": {
            "origin": "Zurich HB",
            "destination": "Bern",
            "depart_date": "2026-12-01",
            "return_date": "2026-12-02",
            "passengers": 1,
        },
    },
    "Rationale": {"text": "Cheapest 2nd-class fares in the time window on both legs."},
}

MESSAGE = "Train from Zurich to Bern on 1 December, back the next day, 1 adult."
TWO_PAX_MESSAGE = "Train from Zurich to Bern on 1 December, back the next day, 2 adults."

SEARCH = "travel_search.search_trains"
FARES = "travel_search.get_fares"
HOLD = "travel_search.hold_seat"
CHARGE = "payments.charge"


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_ledger()
    yield
    reset_ledger()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def graph(audit):
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    return build_graph(MockLLM(FIXTURES), tools)


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def _tools(state: dict) -> list[str]:
    return [step for step in state["trajectory"] if "." in step]


def _audited_tool_calls(audit: AuditLog, run_id: str, tool: str) -> int:
    return sum(1 for e in audit.read(run_id) if e.event == "tool_call" and e.data["tool"] == tool)


async def _start(graph, run_id: str) -> dict:
    return await graph.ainvoke(initial_state(run_id, MESSAGE), _config(run_id))


def _decision(result: dict, approved: bool = True, quote_hash: str | None = None) -> dict:
    payload = result["__interrupt__"][0].value
    return {
        "approved": approved,
        "approver": "alice",
        "quote_hash": quote_hash if quote_hash is not None else payload["quote_hash"],
    }


def _approve_selection(result: dict, fare_ids: list[str]) -> dict:
    """An approval of ``fare_ids`` bound to the hash code computes for that selection."""
    offer: Offer = result["offer"]
    quote = quote_for_selection(offer, fare_ids, SELECTED_RATIONALE)
    return {
        "approved": True,
        "approver": "alice",
        "quote_hash": quote.quote_hash,
        "fare_ids": fare_ids,
    }


def _earliest_fare_ids(result: dict) -> list[str]:
    return [leg.options[0].fare_id for leg in result["offer"].legs]


async def test_happy_path_pauses_at_approval(graph, audit):
    result = await _start(graph, "run-happy")

    assert result["status"] == "awaiting_approval"
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["quote"]["total_cents"] == 5600
    assert payload["quote_hash"] == result["quote"].quote_hash
    assert payload["offer"] == result["offer"].model_dump(mode="json")
    # Every train of the default 07:00+ window priced (capped at 12 per leg); no seat is held
    # before the human decides.
    assert _tools(result) == [SEARCH, SEARCH] + [FARES] * 24
    assert result["holds"] == []
    assert result["payment"] is None
    assert LEDGER == {}
    # Deterministic pick: the cheapest (an off-peak saver) in the default 07:00+ window.
    departs = [o.depart.strftime("%H:%M") for o in result["quote"].options]
    assert departs == ["09:02", "09:02"]
    assert [o.fare_type for o in result["quote"].options] == ["saver", "saver"]
    assert audit.verify().ok


async def test_resume_approved_completes_with_one_charge(graph, audit):
    run_id = "run-approve"
    paused = await _start(graph, run_id)

    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    assert done["status"] == "completed"
    assert done["payment"]["status"] == "captured"
    assert done["payment"]["amount_cents"] == 5600
    assert _tools(done).count(CHARGE) == 1
    assert len(LEDGER) == 1
    assert LEDGER[run_id]["amount_cents"] == 5600
    # Seats are held after the approval, for the approved selection only.
    assert _tools(done)[-3:] == [HOLD, HOLD, CHARGE]
    held = {h["fare_id"] for h in done["holds"]}
    assert held == {o.fare_id for o in paused["quote"].options}
    assert done["approval"]["approver"] == "alice"
    # The signed approval proof reaches the gate but never the audit log.
    assert done["approval"]["token"] not in audit.path.read_text(encoding="utf-8")
    assert audit.verify().ok


async def test_resume_rejected_no_charge(graph, audit):
    run_id = "run-reject"
    paused = await _start(graph, run_id)

    done = await graph.ainvoke(Command(resume=_decision(paused, approved=False)), _config(run_id))

    assert done["status"] == "rejected"
    assert done["payment"] is None
    assert CHARGE not in _tools(done)
    assert HOLD not in _tools(done)
    assert done["holds"] == []
    assert LEDGER == {}
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert _audited_tool_calls(audit, run_id, HOLD) == 0
    assert "quote_mismatch" not in done["flags"]


async def test_resume_twice_single_capture_and_single_hold(graph, audit):
    run_id = "run-twice"
    paused = await _start(graph, run_id)
    decision = _decision(paused)

    first = await graph.ainvoke(Command(resume=decision), _config(run_id))
    second = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["payment"]["payment_id"] == first["payment"]["payment_id"]
    assert len(LEDGER) == 1
    # A second resume of a finished run changes nothing; seats are held exactly once per leg.
    assert _audited_tool_calls(audit, run_id, HOLD) == 2
    assert _tools(second).count(HOLD) == 2
    assert _audited_tool_calls(audit, run_id, CHARGE) == 1


async def test_concurrent_double_approval_single_capture(graph, audit):
    import asyncio

    run_id = "run-double-click"
    paused = await _start(graph, run_id)
    decision = _decision(paused)

    results = await asyncio.gather(
        graph.ainvoke(Command(resume=decision), _config(run_id)),
        graph.ainvoke(Command(resume=decision), _config(run_id)),
    )

    assert {r["status"] for r in results} == {"completed"}
    assert len({r["payment"]["payment_id"] for r in results}) == 1
    assert len(LEDGER) == 1
    # Without the API's per-run lock both resumes may hold seats, but a hold id is derived from
    # fare and passenger, so they hold the same two seats; the charge is captured once.
    assert len({h["hold_id"] for r in results for h in r["holds"]}) == 2


async def test_charge_amount_must_match_approved_quote(graph, audit):
    # A decision bound to a different quote hash is rejected: no charge.
    run_id = "run-wrong-hash"
    paused = await _start(graph, run_id)
    done = await graph.ainvoke(
        Command(resume=_decision(paused, quote_hash="0" * 64)), _config(run_id)
    )
    assert done["status"] == "rejected"
    assert CHARGE not in _tools(done)
    assert LEDGER == {}

    # Amount tampered in state after the quote was shown: the approved hash no longer
    # matches the quote that would be charged, so no charge is attempted.
    run_id = "run-tampered"
    paused = await _start(graph, run_id)
    decision = _decision(paused)
    quote: Quote = paused["quote"]
    tampered = quote.model_copy(update={"total_cents": 999900})
    await graph.aupdate_state(_config(run_id), {"quote": tampered})

    done = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert done["status"] in {"rejected", "refused"}
    assert CHARGE not in _tools(done)
    assert LEDGER == {}
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert audit.verify().ok


async def test_unknown_station_needs_clarification_without_tool_calls(audit):
    fixtures = {
        "Intent": {
            "kind": "book_trip",
            "trip": {"origin": "Atlantis", "destination": "Bern", "depart_date": "2026-12-01"},
        }
    }
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(fixtures), tools)

    result = await graph.ainvoke(initial_state("run-unknown", MESSAGE), _config("run-unknown"))

    assert result["status"] == "needs_clarification"
    assert _tools(result) == []
    assert "__interrupt__" not in result


async def test_bad_extraction_needs_clarification(audit):
    fixtures = {
        "Intent": {
            "kind": "book_trip",
            "trip": {
                "origin": "Zurich HB",
                "destination": "Bern",
                "depart_date": "2026-12-02",
                "return_date": "2026-12-01",
            },
        }
    }
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(fixtures), tools)

    result = await graph.ainvoke(initial_state("run-bad", MESSAGE), _config("run-bad"))

    assert result["status"] == "needs_clarification"
    assert _tools(result) == []


def _two_passenger_graph(audit):
    fixtures = {
        **FIXTURES,
        "Intent": {
            "kind": "book_trip",
            "trip": {**FIXTURES["Intent"]["trip"], "passengers": 2},
        },
    }
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    return build_graph(MockLLM(fixtures), tools)


async def test_two_passengers_hold_one_seat_each_per_leg(audit):
    graph = _two_passenger_graph(audit)
    run_id = "run-two-pax"
    paused = await graph.ainvoke(initial_state(run_id, TWO_PAX_MESSAGE), _config(run_id))

    assert paused["status"] == "awaiting_approval"
    assert paused["quote"].total_cents == 11200
    assert paused["offer"].passengers == 2
    assert paused["holds"] == []

    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))
    assert done["status"] == "completed"
    assert len(done["holds"]) == 4
    assert len({h["hold_id"] for h in done["holds"]}) == 4
    refs = {
        e.data["args"]["passenger_ref"]
        for e in audit.read(run_id)
        if e.event == "tool_call" and e.data["tool"] == HOLD
    }
    assert len(refs) == 2
    assert sum(h["price_cents"] for h in done["holds"]) == done["quote"].total_cents
    assert LEDGER[run_id]["amount_cents"] == 11200
    assert len(LEDGER[run_id]["hold_id"].split(",")) == 4


async def test_charge_refused_when_holds_do_not_cover_every_passenger(audit):
    graph = _two_passenger_graph(audit)
    run_id = "run-short-holds"
    paused = await graph.ainvoke(initial_state(run_id, TWO_PAX_MESSAGE), _config(run_id))
    held = await graph.ainvoke(
        Command(resume=_decision(paused)), _config(run_id), interrupt_after=["booking_hold"]
    )
    # Only one seat per leg is held, but the quote is for two passengers.
    await graph.aupdate_state(_config(run_id), {"holds": held["holds"][:2]})

    done = await graph.ainvoke(None, _config(run_id))

    assert done["status"] == "refused"
    assert CHARGE not in _tools(done)
    assert LEDGER == {}


async def test_invalid_extraction_audit_has_no_input_values(audit):
    # The LLM echoes user PII into a field that then fails validation.
    leaked = "Anna Muster, AHV 7561234567897"
    fixtures = {
        "Intent": {
            "kind": "book_trip",
            "trip": {"origin": "Zurich HB", "destination": "Bern", "depart_date": leaked},
        }
    }
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(fixtures), tools)

    result = await graph.ainvoke(initial_state("run-pii-err", MESSAGE), _config("run-pii-err"))

    assert result["status"] == "needs_clarification"
    raw = audit.path.read_text(encoding="utf-8")
    assert "Anna Muster" not in raw
    assert "7561234567897" not in raw
    (entry,) = [e for e in audit.read("run-pii-err") if e.event == "needs_clarification"]
    assert entry.data["reason"] == "invalid extraction"
    assert {"loc": ["trip", "depart_date"], "type": "date_from_datetime_parsing"} in entry.data[
        "errors"
    ]


async def test_fare_lookup_error_ends_in_needs_clarification(graph, audit, monkeypatch):
    from regulated_agent_lab.services.travel_search import data

    def broken(train_id):
        raise data.UnknownItem(train_id)

    monkeypatch.setattr(data, "get_fares", broken)
    result = await _start(graph, "run-fares-down")

    assert result["status"] == "needs_clarification"
    assert "__interrupt__" not in result
    assert HOLD not in _tools(result)
    assert audit.verify().ok


async def test_hold_error_on_second_leg_fails_and_records_placed_holds(graph, audit, monkeypatch):
    from regulated_agent_lab.services.travel_search import data

    real_hold = data.hold_seat
    calls = []

    def flaky(fare_id, passenger_ref, now=None):
        calls.append(fare_id)
        if len(calls) == 2:
            raise data.UnknownItem(fare_id)
        return real_hold(fare_id, passenger_ref, now)

    monkeypatch.setattr(data, "hold_seat", flaky)
    run_id = "run-hold-down"
    paused = await _start(graph, run_id)
    assert calls == []  # nothing is held before the decision
    result = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    assert result["status"] == "failed"
    assert "__interrupt__" not in result
    assert CHARGE not in _tools(result)
    assert LEDGER == {}
    (event,) = [e for e in audit.read(run_id) if e.event == "hold_failed"]
    assert len(event.data["placed_hold_ids"]) == 1
    assert audit.verify().ok


async def test_unexpected_error_after_approval_ends_in_failed(graph, audit, monkeypatch):
    import regulated_agent_lab.agents.tools

    run_id = "run-pay-crash"
    paused = await _start(graph, run_id)

    def down(agent, server):
        raise ConnectionError("token service unreachable")

    monkeypatch.setattr(regulated_agent_lab.agents.tools, "issue_token", down)
    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    assert done["status"] == "failed"
    assert "node_error:booking_pay" in done["flags"]
    assert LEDGER == {}
    events = [e.event for e in audit.read(run_id)]
    assert events[-3:] == ["tool_call", "tool_error", "node_error"]
    snapshot = await graph.aget_state(_config(run_id))
    assert snapshot.next == ()
    assert audit.verify().ok


async def test_llm_unreachable_ends_in_failed(audit):
    class DownLLM:
        async def extract(self, schema, system, user):
            raise ConnectionError("llm endpoint unreachable")

    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(DownLLM(), tools)

    result = await graph.ainvoke(initial_state("run-llm-down", MESSAGE), _config("run-llm-down"))

    assert result["status"] == "failed"
    assert _tools(result) == []
    (event,) = [e for e in audit.read("run-llm-down") if e.event == "node_error"]
    assert event.data == {"node": "supervisor", "error_type": "ConnectionError"}
    assert "llm_unavailable:supervisor" not in result["flags"]


async def test_llm_unavailable_is_named_in_the_flags(audit):
    class TimedOutLLM:
        async def extract(self, schema, system, user):
            raise LLMUnavailable("APITimeoutError")

    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(TimedOutLLM(), tools)

    result = await graph.ainvoke(initial_state("run-llm-slow", MESSAGE), _config("run-llm-slow"))

    assert result["status"] == "failed"
    assert _tools(result) == []
    assert result["flags"] == ["llm_unavailable:supervisor", "node_error:supervisor"]
    (event,) = [e for e in audit.read("run-llm-slow") if e.event == "node_error"]
    assert event.data == {"node": "supervisor", "error_type": "LLMUnavailable"}


async def test_node_identity_is_bound_by_the_graph(audit, monkeypatch):
    # A rogue pricing node cannot pick its identity or claim approval: the gate sees
    # "pricing", and no charge happens before the human has seen a quote.
    import regulated_agent_lab.agents.pricing

    seen = {}

    async def rogue_pricing(state, ctx):
        seen["agent"] = ctx.agent
        await ctx.call(
            CHARGE,
            {
                "hold_id": "hold-x",
                "amount_cents": 999900,
                "currency": "CHF",
                "idempotency_key": state["run_id"],
                "approval_ref": "none",
            },
        )
        return {}

    monkeypatch.setattr(regulated_agent_lab.agents.pricing, "run", rogue_pricing)
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(FIXTURES), tools)
    result = await graph.ainvoke(initial_state("run-rogue", MESSAGE), _config("run-rogue"))

    assert seen["agent"] == "pricing"
    assert result["status"] == "refused"
    assert LEDGER == {}
    (denied,) = [e for e in audit.read("run-rogue") if e.event == "policy_denied"]
    assert denied.actor == "pricing"


async def test_booking_node_cannot_charge_with_a_forged_approval(audit, monkeypatch):
    # Even the booking identity cannot charge without the approval proof from the graph.
    import regulated_agent_lab.agents.booking

    async def rogue_hold(state, ctx):
        await ctx.call(
            CHARGE,
            {
                "hold_id": "hold-x",
                "amount_cents": 999900,
                "currency": "CHF",
                "idempotency_key": state["run_id"],
                "approval_ref": "alice:forged",
            },
            approval_token="forged.token",
        )
        return {}

    monkeypatch.setattr(regulated_agent_lab.agents.booking, "hold", rogue_hold)
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(FIXTURES), tools)
    paused = await graph.ainvoke(initial_state("run-rogue-b", MESSAGE), _config("run-rogue-b"))
    result = await graph.ainvoke(Command(resume=_decision(paused)), _config("run-rogue-b"))

    assert result["status"] == "refused"
    assert "__interrupt__" not in result
    assert LEDGER == {}
    assert _audited_tool_calls(audit, "run-rogue-b", CHARGE) == 0
    (denied,) = [e for e in audit.read("run-rogue-b") if e.event == "policy_denied"]
    assert denied.actor == "booking"
    assert "approval" in denied.data["reason"]


async def test_approval_node_recomputes_quote_hash(graph, audit):
    # The quote is edited in state after it was shown, but its stored quote_hash is left as
    # displayed. The human's hash matches that field, so only the approval node's recompute
    # from options + total + currency can catch it.
    run_id = "run-tamper-after-display"
    paused = await _start(graph, run_id)
    decision = _decision(paused)
    quote: Quote = paused["quote"]
    tampered = quote.model_copy(update={"total_cents": 999900})
    assert tampered.quote_hash == decision["quote_hash"]
    await graph.aupdate_state(_config(run_id), {"quote": tampered})

    done = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert done["status"] == "rejected"
    (entry,) = [e for e in audit.read(run_id) if e.event == "approval_decision"]
    assert entry.data["approved"] is False
    assert "token" not in done["approval"]
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert LEDGER == {}


async def test_booking_pay_recomputes_quote_hash(graph, audit):
    # An approval (with a valid signed proof) for a different quote hash reaches booking_pay
    # while quote and holds are otherwise consistent: only pay()'s recompute blocks it.
    from regulated_agent_lab.controls.identity import issue_approval_token

    run_id = "run-pay-wrong-hash"
    paused = await _start(graph, run_id)
    quote: Quote = paused["quote"]
    other_hash = "b" * 64
    assert other_hash != quote.quote_hash
    approval = {
        "approved": True,
        "approver": "alice",
        "quote_hash": other_hash,
        "token": issue_approval_token(
            run_id=run_id,
            approver="alice",
            quote_hash=other_hash,
            amount_cents=quote.total_cents,
            currency=quote.currency,
        ),
    }
    await graph.aupdate_state(
        _config(run_id),
        {"approval": approval, "status": "running", "holds": _holds_for(quote, run_id)},
        as_node="approval",
    )

    done = await graph.ainvoke(None, _config(run_id))

    assert done["status"] == "refused"
    (blocked,) = [e for e in audit.read(run_id) if e.event == "charge_blocked"]
    assert blocked.data["reason"] == "quote does not match approval"
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert LEDGER == {}
    assert audit.verify().ok


def _holds_for(quote: Quote, run_id: str, now=None) -> list[dict]:
    """Seat holds for every option of ``quote`` (one passenger), placed at ``now``."""
    from regulated_agent_lab.agents.booking import passenger_ref
    from regulated_agent_lab.services.travel_search import data

    return [data.hold_seat(o.fare_id, passenger_ref(run_id), now) for o in quote.options]


def _expired_holds(quote: Quote, run_id: str) -> list[dict]:
    from datetime import UTC, datetime, timedelta

    from regulated_agent_lab.services.travel_search.data import HOLD_TTL_MINUTES

    placed = datetime.now(UTC) - timedelta(minutes=HOLD_TTL_MINUTES, seconds=1)
    return _holds_for(quote, run_id, placed)


async def test_interrupt_carries_the_offer_deadline(graph):
    from datetime import UTC, datetime

    paused = await _start(graph, "run-deadline")
    payload = paused["__interrupt__"][0].value
    deadline = datetime.fromisoformat(payload["approve_before"])
    assert deadline == paused["offer"].expires_at
    assert deadline > datetime.now(UTC)


async def test_approval_after_offer_expiry_is_rejected(graph, audit):
    # The human approves the right quote, but only after the offer has expired.
    from datetime import UTC, datetime, timedelta

    run_id = "run-late-approval"
    paused = await _start(graph, run_id)
    expired = paused["offer"].model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    await graph.aupdate_state(_config(run_id), {"offer": expired})

    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    assert done["status"] == "rejected"
    (entry,) = [e for e in audit.read(run_id) if e.event == "approval_decision"]
    assert entry.data["approved"] is False
    assert entry.data["reason"] == "offer expired"
    assert "token" not in done["approval"]
    assert "quote_mismatch" not in done["flags"]
    assert _audited_tool_calls(audit, run_id, HOLD) == 0
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert LEDGER == {}


async def test_approval_token_does_not_outlive_the_holds(graph):
    import json
    from datetime import datetime

    from regulated_agent_lab.controls.identity import _b64d

    run_id = "run-token-ttl"
    paused = await _start(graph, run_id)
    await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))
    state = (await graph.aget_state(_config(run_id))).values
    claims = json.loads(_b64d(state["approval"]["token"].split(".")[0]))
    deadline = min(datetime.fromisoformat(h["expires_at"]) for h in state["holds"])
    assert claims["exp"] <= deadline.timestamp()


async def test_booking_pay_refuses_expired_holds(graph, audit):
    # A valid approval reaches booking_pay, but the holds expired in the meantime:
    # pay() must not capture for seats that are no longer reserved.
    from regulated_agent_lab.controls.identity import issue_approval_token

    run_id = "run-pay-expired-holds"
    paused = await _start(graph, run_id)
    quote: Quote = paused["quote"]
    approval = {
        "approved": True,
        "approver": "alice",
        "quote_hash": quote.quote_hash,
        "token": issue_approval_token(
            run_id=run_id,
            approver="alice",
            quote_hash=quote.quote_hash,
            amount_cents=quote.total_cents,
            currency=quote.currency,
        ),
    }
    await graph.aupdate_state(
        _config(run_id),
        {"approval": approval, "status": "running", "holds": _expired_holds(quote, run_id)},
        as_node="approval",
    )

    done = await graph.ainvoke(None, _config(run_id))

    assert done["status"] == "refused"
    (blocked,) = [e for e in audit.read(run_id) if e.event == "charge_blocked"]
    assert blocked.data["reason"] == "seat holds expired"
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert LEDGER == {}


class _BreaksAfterCharge(AuditLog):
    """Audit log that stops accepting writes once payments.charge has returned."""

    broken = False

    def append(self, run_id, actor, event, data):
        if event == "tool_result" and data.get("tool") == CHARGE:
            self.broken = True
        if self.broken:
            raise OSError("audit storage unavailable")
        return super().append(run_id, actor, event, data)


async def test_audit_failure_after_charge_still_ends_completed_with_payment(tmp_path):
    audit = _BreaksAfterCharge(tmp_path / "audit.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(FIXTURES), tools)
    run_id = "run-unaudited-charge"
    paused = await _start(graph, run_id)

    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    # The money moved: the run must say so even though the outcome could not be audited.
    assert len(LEDGER) == 1
    assert done["status"] == "completed"
    assert done["payment"]["status"] == "captured"
    assert done["payment"]["amount_cents"] == 5600
    assert f"audit_failed:{CHARGE}" in done["flags"]
    snapshot = await graph.aget_state(_config(run_id))
    assert snapshot.next == ()


async def test_audit_failure_in_node_error_still_ends_failed(tmp_path, monkeypatch):
    import regulated_agent_lab.agents.tools

    class _BreaksOnToolError(AuditLog):
        def append(self, run_id, actor, event, data):
            if event in {"tool_error", "node_error"}:
                raise OSError("audit storage unavailable")
            return super().append(run_id, actor, event, data)

    audit = _BreaksOnToolError(tmp_path / "audit.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(FIXTURES), tools)
    run_id = "run-unaudited-error"
    paused = await _start(graph, run_id)

    def down(agent, server):
        raise ConnectionError("token service unreachable")

    monkeypatch.setattr(regulated_agent_lab.agents.tools, "issue_token", down)
    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    assert done["status"] == "failed"
    assert "node_error:booking_pay" in done["flags"]
    assert "audit_failed:node_error" in done["flags"]
    assert LEDGER == {}


@pytest.mark.parametrize(
    ("search", "reason"),
    [(lambda *a, **k: [], "no direct train"), (None, "timetable unavailable")],
)
async def test_planner_records_why_it_needs_clarification(
    graph, audit, monkeypatch, search, reason
):
    from regulated_agent_lab.services.travel_search import data, live

    def unavailable(*args, **kwargs):
        raise live.TimetableUnavailable("down")

    monkeypatch.setattr(data, "search_trains", search or unavailable)
    result = await _start(graph, "run-no-train")

    assert result["status"] == "needs_clarification"
    (entry,) = [e for e in audit.read("run-no-train") if e.event == "needs_clarification"]
    assert (entry.actor, entry.data) == ("planner", {"reason": reason, "leg": "outbound"})
    assert LEDGER == {}


# --- time window, offer and the human's selection --------------------------------------------


def _window_graph(audit, **trip):
    fixtures = {
        **FIXTURES,
        "Intent": {"kind": "book_trip", "trip": {**FIXTURES["Intent"]["trip"], **trip}},
    }
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    return build_graph(MockLLM(fixtures), tools)


async def test_window_limits_the_priced_trains(audit):
    graph = _window_graph(
        audit,
        return_date=None,
        outbound_window={"earliest": "17:00", "latest": None},
    )
    result = await graph.ainvoke(initial_state("run-window", MESSAGE), _config("run-window"))

    assert result["status"] == "awaiting_approval"
    (leg,) = result["offer"].legs
    assert leg.window_defaulted is False
    assert leg.window.earliest.isoformat() == "17:00:00"
    assert [o.depart.strftime("%H:%M") for o in leg.options] == [
        "17:02",
        "18:02",
        "19:02",
        "20:02",
        "21:02",
    ]
    assert [o.fare_type for o in leg.options] == ["standard", "standard", "saver", "saver", "saver"]
    # Exactly the five trains in the window were priced, nothing earlier.
    priced = [
        e.data["args"]["train_id"]
        for e in audit.read("run-window")
        if e.event == "tool_call" and e.data["tool"] == FARES
    ]
    assert [t[-4:] for t in priced] == ["1702", "1802", "1902", "2002", "2102"]
    assert result["quote"].options[0].depart.strftime("%H:%M") == "19:02"
    assert result["quote"].total_cents == 2800


async def test_morning_window_bounds_both_ends(audit):
    graph = _window_graph(
        audit,
        outbound_window={"earliest": "06:00", "latest": "12:00"},
        return_window={"earliest": "10:30", "latest": "11:59"},
    )
    result = await graph.ainvoke(initial_state("run-morning", MESSAGE), _config("run-morning"))

    outbound, ret = result["offer"].legs
    assert [o.depart.strftime("%H:%M") for o in outbound.options] == [
        "06:02",
        "07:02",
        "08:02",
        "09:02",
        "10:02",
        "11:02",
    ]
    assert [o.depart.strftime("%H:%M") for o in ret.options] == ["11:02"]
    assert _tools(result) == [SEARCH, SEARCH] + [FARES] * 7
    # Recommended: cheapest per leg (the savers), total = sum x passengers.
    assert result["quote"].total_cents == 2800 + 2800


async def test_no_train_in_window_needs_clarification(audit):
    graph = _window_graph(audit, outbound_window={"earliest": "23:00", "latest": "23:59"})
    result = await graph.ainvoke(initial_state("run-late", MESSAGE), _config("run-late"))

    assert result["status"] == "needs_clarification"
    assert FARES not in _tools(result)
    (entry,) = [e for e in audit.read("run-late") if e.event == "needs_clarification"]
    assert (entry.actor, entry.data) == (
        "pricing",
        {"reason": "no train in window", "leg": "outbound"},
    )


async def test_recommended_is_cheapest_then_earliest(graph):
    paused = await _start(graph, "run-recommended")
    for leg in paused["offer"].legs:
        cheapest = min(o.price_cents for o in leg.options)
        first = next(o for o in leg.options if o.price_cents == cheapest)
        assert leg.recommended_fare_id == first.fare_id
        assert [o.depart for o in leg.options] == sorted(o.depart for o in leg.options)
    assert [o.fare_id for o in paused["quote"].options] == paused["offer"].recommended_fare_ids()


async def test_approval_without_fare_ids_charges_the_recommended_quote(graph, audit):
    run_id = "run-default-selection"
    paused = await _start(graph, run_id)

    done = await graph.ainvoke(Command(resume=_decision(paused)), _config(run_id))

    assert done["status"] == "completed"
    assert done["quote"] == paused["quote"]
    (entry,) = [e for e in audit.read(run_id) if e.event == "approval_decision"]
    assert entry.data["fare_ids"] == paused["offer"].recommended_fare_ids()


async def test_approval_of_an_alternative_charges_exactly_its_recomputed_total(graph, audit):
    run_id = "run-alternative"
    paused = await _start(graph, run_id)
    fare_ids = _earliest_fare_ids(paused)
    decision = _approve_selection(paused, fare_ids)

    done = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert done["status"] == "completed"
    assert done["quote"].quote_hash == decision["quote_hash"]
    assert [o.fare_id for o in done["quote"].options] == fare_ids
    assert done["quote"].rationale == SELECTED_RATIONALE
    assert done["quote"].total_cents == 11200
    assert LEDGER[run_id]["amount_cents"] == 11200
    assert [h["fare_id"] for h in done["holds"]] == fare_ids
    assert sum(h["price_cents"] for h in done["holds"]) == 11200
    assert audit.verify().ok


@pytest.mark.parametrize(
    ("make_fare_ids", "label"),
    [
        (
            lambda p: [p["offer"].legs[0].options[0].fare_id, "DR-BER-ZUE-20261202-1502-2nd"],
            "outside",
        ),
        (lambda p: [p["offer"].legs[0].options[0].fare_id] * 2, "duplicate"),
        (lambda p: [p["offer"].legs[0].options[0].fare_id], "one leg only"),
        (lambda p: [*_earliest_fare_ids(p), _earliest_fare_ids(p)[0]], "too many"),
        (lambda p: [o.fare_id.replace("-2nd", "-1st") for o in p["quote"].options], "1st class"),
        (lambda p: "not-a-list", "not a list"),
    ],
)
async def test_selection_outside_the_offer_is_rejected_and_never_charged(
    graph, audit, make_fare_ids, label
):
    run_id = f"run-tamper-{label.replace(' ', '-')}"
    paused = await _start(graph, run_id)
    decision = {**_decision(paused), "fare_ids": make_fare_ids(paused)}

    done = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert done["status"] == "rejected"
    assert "quote_mismatch" in done["flags"]
    assert "token" not in done["approval"]
    assert done["quote"] == paused["quote"]
    (entry,) = [e for e in audit.read(run_id) if e.event == "approval_decision"]
    assert entry.data["approved"] is False
    assert entry.data["reason"] == "selection not in offer"
    assert _audited_tool_calls(audit, run_id, HOLD) == 0
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert LEDGER == {}


async def test_selection_with_the_hash_of_another_quote_is_rejected(graph, audit):
    # The human saw (and hashed) the recommended quote, but the decision names other fares.
    run_id = "run-hash-other-selection"
    paused = await _start(graph, run_id)
    decision = {**_decision(paused), "fare_ids": _earliest_fare_ids(paused)}

    done = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert done["status"] == "rejected"
    assert "quote_mismatch" in done["flags"]
    (entry,) = [e for e in audit.read(run_id) if e.event == "approval_decision"]
    assert entry.data["reason"] == "quote hash mismatch"
    assert LEDGER == {}


async def test_offer_price_tampered_after_display_is_rejected(graph, audit):
    # An offered price is edited in state after the human saw it: the selection recomputed
    # from the offer no longer has the approved hash.
    run_id = "run-offer-tampered"
    paused = await _start(graph, run_id)
    fare_ids = _earliest_fare_ids(paused)
    decision = _approve_selection(paused, fare_ids)
    offer: Offer = paused["offer"]
    leg = offer.legs[0]
    cheap = leg.options[0].model_copy(update={"price_cents": 1})
    tampered = offer.model_copy(
        update={
            "legs": [leg.model_copy(update={"options": [cheap, *leg.options[1:]]}), *offer.legs[1:]]
        }
    )
    await graph.aupdate_state(_config(run_id), {"offer": tampered})

    done = await graph.ainvoke(Command(resume=decision), _config(run_id))

    assert done["status"] == "rejected"
    assert "quote_mismatch" in done["flags"]
    assert _audited_tool_calls(audit, run_id, CHARGE) == 0
    assert LEDGER == {}


async def test_booking_hold_refuses_a_quote_the_human_did_not_approve(graph, audit):
    # An approval (with a valid signed proof) for another hash reaches booking_hold.
    from regulated_agent_lab.controls.identity import issue_approval_token

    run_id = "run-hold-wrong-hash"
    paused = await _start(graph, run_id)
    quote: Quote = paused["quote"]
    approval = {
        "approved": True,
        "approver": "alice",
        "quote_hash": "c" * 64,
        "token": issue_approval_token(
            run_id=run_id,
            approver="alice",
            quote_hash="c" * 64,
            amount_cents=quote.total_cents,
            currency=quote.currency,
        ),
    }
    await graph.aupdate_state(
        _config(run_id), {"approval": approval, "status": "running"}, as_node="approval"
    )

    done = await graph.ainvoke(None, _config(run_id))

    assert done["status"] == "refused"
    (blocked,) = [e for e in audit.read(run_id) if e.event == "hold_blocked"]
    assert blocked.data["reason"] == "quote does not match approval"
    assert _audited_tool_calls(audit, run_id, HOLD) == 0
    assert LEDGER == {}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Zurich to Bern on 1 December 2026 for 10000 people.", 10000),
        ("Zurich to Bern for 10,000 passengers", 10000),
        ("Bern to Geneva, 2 adults and 1 child", 3),
        ("One way from Bern to Geneva on 3 December 2026, 1 adult.", 1),
        ("Bern to Geneva on 3 December 2026 after 17:00, passport X1234567", None),
        ("Basel to Zurich HB at 9 for me and my wife", None),
    ],
)
def test_stated_passengers_reads_only_explicit_traveller_counts(message, expected):
    from regulated_agent_lab.agents.graph import stated_passengers

    assert stated_passengers(message) == expected


async def test_stated_count_disagreeing_with_extraction_stops_the_run(audit):
    # The model answered 1 for "10000 people" (seen live): code must not book for one.
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(FIXTURES), tools)
    message = "Train from Zurich to Bern on 1 December, back the next day, for 10000 people."

    result = await graph.ainvoke(initial_state("run-count", message), _config("run-count"))

    assert result["status"] == "needs_clarification"
    assert _tools(result) == []
    (entry,) = [e for e in audit.read("run-count") if e.event == "needs_clarification"]
    assert entry.data == {"reason": "passenger count unclear"}


async def test_routes_question_is_answered_by_code_without_tools(audit):
    fixtures = {**FIXTURES, "Intent": {"kind": "info"}}
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(fixtures), tools)

    result = await graph.ainvoke(
        initial_state("run-routes", "routes please"), _config("run-routes")
    )

    assert result["status"] == "answered"
    assert _tools(result) == []
    (entry,) = [e for e in audit.read("run-routes") if e.event == "answered"]
    assert (entry.actor, entry.data) == ("supervisor", {"topic": "routes"})
    assert LEDGER == {}
