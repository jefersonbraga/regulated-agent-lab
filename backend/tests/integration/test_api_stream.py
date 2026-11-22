"""SSE run events: order, single charge, injection flag and no PII (mock LLM, in-memory MCP)."""

import asyncio
import json

import httpx
import pytest

from regulated_agent_lab.agents.llm import MockLLM
from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.api.server import create_app
from regulated_agent_lab.controls.audit_log import AuditLog
from regulated_agent_lab.controls.policy import ToolPolicy
from regulated_agent_lab.services.payments.server import LEDGER, reset_ledger
from tests.integration.test_api import CHARGE, FIXTURES, MESSAGE, serve

PII_NAME = "Anna Muster"
PII_EMAIL = "anna.muster@example.org"
PII_MESSAGE = f"{MESSAGE} Passenger {PII_NAME}, email {PII_EMAIL}."


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_ledger()
    yield
    reset_ledger()


@pytest.fixture
async def client(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        yield c


def _parse(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        if block.strip():
            assert block.startswith("data: "), block
            events.append(json.loads(block.removeprefix("data: ")))
    return events


async def _stream(client, url: str, payload: dict) -> tuple[httpx.Response, list[dict]]:
    resp = await client.post(url, json=payload)
    events = _parse(resp.text) if resp.status_code == 200 else []
    return resp, events


async def _start(client, message: str = MESSAGE) -> list[dict]:
    resp, events = await _stream(client, "/runs/stream", {"message": message})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return events


def _approval(view: dict) -> dict:
    return {"approved": True, "approver": "alice", "quote_hash": view["interrupt"]["quote_hash"]}


def _nodes(events: list[dict], kind: str) -> list[str]:
    return [e["node"] for e in events if e["type"] == kind]


async def test_run_stream_event_order_to_interrupt(client):
    events = await _start(client)

    types = [e["type"] for e in events]
    assert types[0] == "run_started"
    assert types[-2:] == ["status", "run_view"]
    run_id = events[0]["run_id"]
    view = events[-1]
    assert view["run_id"] == run_id
    assert view["status"] == "awaiting_approval"
    assert events[-2]["status"] == "awaiting_approval"

    started = _nodes(events, "node_started")
    assert [n for n in started if n != "supervisor"] == [
        "planner",
        "pricing",
        "approval",
    ]
    assert started[0] == "supervisor"
    # Every node_started is closed by a node_finished for the same node, in order.
    pairs = [(e["type"], e["node"]) for e in events if e["type"].startswith("node_")]
    for i in range(0, len(pairs), 2):
        assert pairs[i][0] == "node_started" and pairs[i + 1] == ("node_finished", pairs[i][1])
    last_node = [e for e in events if e["type"] == "node_finished"][-1]
    assert last_node == {"type": "node_finished", "node": "approval", "interrupted": True}

    # Tool events sit between their node's start and finish, call before result.
    planner_start = events.index({"type": "node_started", "node": "planner"})
    first_call = next(i for i, e in enumerate(events) if e["type"] == "tool_call")
    assert first_call > planner_start
    assert events[first_call] == {
        "type": "tool_call",
        "agent": "planner",
        "tool": "travel_search.search_trains",
    }
    assert events[first_call + 1] == {
        "type": "tool_result",
        "agent": "planner",
        "tool": "travel_search.search_trains",
        "flagged": False,
    }
    assert not any(e["type"] == "tool_call" and e["tool"] == CHARGE for e in events)
    assert LEDGER == {}

    # The final run_view is the same RunView as GET /runs/{id}.
    fetched = (await client.get(f"/runs/{run_id}")).json()
    assert {k: v for k, v in view.items() if k != "type"} == fetched


async def test_approval_stream_completes_with_one_charge(client):
    view = (await _start(client))[-1]

    resp, events = await _stream(client, f"/runs/{view['run_id']}/approval/stream", _approval(view))

    assert resp.status_code == 200, resp.text
    assert events[0] == {"type": "run_started", "run_id": view["run_id"]}
    assert "approval" in _nodes(events, "node_started")
    assert _nodes(events, "node_started").index("booking_hold") > 0
    assert "booking_pay" in _nodes(events, "node_started")
    holds = [
        e for e in events if e["type"] == "tool_call" and e["tool"] == "travel_search.hold_seat"
    ]
    assert len(holds) == 2
    charges = [e for e in events if e["type"] == "tool_call" and e["tool"] == CHARGE]
    assert charges == [{"type": "tool_call", "agent": "booking", "tool": CHARGE}]
    assert [e for e in events if e["type"] == "tool_result" and e["tool"] == CHARGE] == [
        {"type": "tool_result", "agent": "booking", "tool": CHARGE, "flagged": False}
    ]
    assert events[-2] == {"type": "status", "status": "completed"}
    assert events[-1]["type"] == "run_view"
    assert events[-1]["status"] == "completed"
    assert events[-1]["payment"]["amount_cents"] == 5600
    assert len(LEDGER) == 1


async def test_poisoned_run_streams_flagged_tool_result(client, monkeypatch):
    monkeypatch.setenv("TRAVEL_SEARCH_POISONED", "1")

    events = await _start(client)

    flagged = [e for e in events if e["type"] == "tool_result" and e["flagged"]]
    assert flagged
    assert flagged[0]["tool"] == "travel_search.search_trains"
    assert flagged[0]["agent"] == "planner"
    assert any(f.startswith("injection_flagged:") for f in events[-1]["flags"])
    assert "Ignore previous instructions" not in json.dumps(events)


async def test_double_approval_stream_and_plain_single_capture(client):
    view = (await _start(client))[-1]
    run_id = view["run_id"]

    (stream_resp, stream_events), plain = await asyncio.gather(
        _stream(client, f"/runs/{run_id}/approval/stream", _approval(view)),
        client.post(f"/runs/{run_id}/approval", json=_approval(view)),
    )
    late_stream, _ = await _stream(client, f"/runs/{run_id}/approval/stream", _approval(view))
    late_plain = await client.post(f"/runs/{run_id}/approval", json=_approval(view))

    assert late_stream.status_code == 409
    assert late_plain.status_code == 409
    # Exactly one of the two concurrent approvals resumed the run.
    stream_won = stream_resp.status_code == 200 and stream_events[-1]["type"] == "run_view"
    plain_won = plain.status_code == 200
    assert stream_won != plain_won
    if not stream_won and stream_resp.status_code == 200:
        assert stream_events == [
            {"type": "error", "status_code": 409, "detail": "not awaiting approval"}
        ]
    assert len(LEDGER) == 1
    final = (await client.get(f"/runs/{run_id}")).json()
    assert final["status"] == "completed"
    assert final["trajectory"].count(CHARGE) == 1


async def test_stream_events_carry_no_pii(client):
    events = await _start(client, PII_MESSAGE)
    view = events[-1]
    resp, approval_events = await _stream(
        client, f"/runs/{view['run_id']}/approval/stream", _approval(view)
    )
    assert resp.status_code == 200

    raw = json.dumps(events + approval_events)
    for secret in (PII_NAME, "Muster", PII_EMAIL, "example.org"):
        assert secret not in raw
    allowed = {"type", "run_id", "node", "interrupted", "agent", "tool", "flagged", "status"}
    for e in events + approval_events:
        if e["type"] not in ("run_view", "policy_denied"):
            assert set(e) <= allowed, e


async def test_policy_denied_is_streamed(client, monkeypatch):
    from regulated_agent_lab.controls.policy import PolicyViolation, ToolPolicy

    original = ToolPolicy.check

    def deny_search(self, agent, tool, *, approved):
        if tool == "travel_search.search_trains":
            raise PolicyViolation(agent, tool, "tool not allowed for agent (default deny)")
        return original(self, agent, tool, approved=approved)

    monkeypatch.setattr(ToolPolicy, "check", deny_search)
    events = await _start(client)

    denied = [e for e in events if e["type"] == "policy_denied"]
    assert denied == [
        {
            "type": "policy_denied",
            "agent": "planner",
            "tool": "travel_search.search_trains",
            "reason": "tool not allowed for agent (default deny)",
        }
    ]
    assert events[-1]["status"] == "refused"


async def test_stream_unknown_run_and_not_awaiting(client):
    resp = await client.post(
        "/runs/doesnotexist/approval/stream",
        json={"approved": True, "approver": "alice", "quote_hash": "x"},
    )
    assert resp.status_code == 404


async def test_extra_cors_origin_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("API_EXTRA_CORS_ORIGINS", "http://localhost:5273, *")
    audit = AuditLog(tmp_path / "audit.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        headers = {"Access-Control-Request-Method": "POST"}
        ok = await c.options("/runs/stream", headers={**headers, "Origin": "http://localhost:5273"})
        bad = await c.options("/runs/stream", headers={**headers, "Origin": "http://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5273"
    assert "access-control-allow-origin" not in bad.headers


async def test_public_app_url_origin_is_allowed(tmp_path, monkeypatch):
    # Same-origin deploy (infra/): the UI at PUBLIC_APP_URL calls the API under /api.
    monkeypatch.setenv("PUBLIC_APP_URL", "https://Lab.Example.org/")
    audit = AuditLog(tmp_path / "audit.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        headers = {"Access-Control-Request-Method": "POST"}
        ok = await c.options(
            "/runs/stream", headers={**headers, "Origin": "https://lab.example.org"}
        )
        bad = await c.options(
            "/runs/stream", headers={**headers, "Origin": "https://lab.example.org.evil.example"}
        )
    assert ok.headers.get("access-control-allow-origin") == "https://lab.example.org"
    assert "access-control-allow-origin" not in bad.headers


async def test_approval_stream_accepts_fare_ids(client):
    view = (await _start(client))[-1]
    fare_ids = [leg["options"][0]["fare_id"] for leg in view["interrupt"]["offer"]["legs"]]
    quote = (await client.post(f"/runs/{view['run_id']}/quote", json={"fare_ids": fare_ids})).json()

    resp, events = await _stream(
        client,
        f"/runs/{view['run_id']}/approval/stream",
        {**_approval(view), "quote_hash": quote["quote_hash"], "fare_ids": fare_ids},
    )

    assert resp.status_code == 200, resp.text
    assert events[-1]["status"] == "completed"
    assert events[-1]["payment"]["amount_cents"] == quote["total_cents"] == 11200
    assert LEDGER[view["run_id"]]["amount_cents"] == 11200
