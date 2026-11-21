"""FastAPI layer: runs, human approval and audit endpoints (mock LLM, in-memory MCP)."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from agents.llm import MockLLM
from agents.tools import GatedToolClient, make_clients
from api.server import create_app
from controls.audit_log import AuditLog
from controls.policy import ToolPolicy
from mcp_servers.payments_sandbox.server import LEDGER, reset_ledger

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
    "Rationale": {"text": "Cheapest 2nd-class fares after 07:00 on both legs."},
}

MESSAGE = "Train from Zurich to Bern on 1 December, back the next day, 1 adult."
CHARGE = "payments.charge"


@asynccontextmanager
async def serve(app):
    """An HTTP client over ``app`` with its lifespan running (it opens the SQLite store)."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_ledger()
    yield
    reset_ledger()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
async def client(audit, tmp_path):
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        yield c


async def _create(client) -> dict:
    resp = await client.post("/runs", json={"message": MESSAGE})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approval(run: dict, approved: bool = True) -> dict:
    return {
        "approved": approved,
        "approver": "alice",
        "quote_hash": run["interrupt"]["quote_hash"],
    }


async def test_create_run_returns_interrupt_with_quote(client):
    run = await _create(client)

    assert len(run["run_id"]) == 32
    assert run["status"] == "awaiting_approval"
    assert run["quote"]["total_cents"] == 5600
    assert run["quote"]["currency"] == "CHF"
    assert run["interrupt"]["quote_hash"] == run["quote"]["quote_hash"]
    assert run["interrupt"]["quote"]["total_cents"] == 5600
    # No seat is held before the human has approved a selection.
    assert "travel_search.hold_seat" not in run["trajectory"]
    assert CHARGE not in run["trajectory"]
    assert LEDGER == {}

    fetched = await client.get(f"/runs/{run['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "awaiting_approval"
    assert fetched.json()["interrupt"]["quote_hash"] == run["quote"]["quote_hash"]


async def test_approve_completes(client):
    run = await _create(client)

    resp = await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run))

    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "completed"
    assert done["interrupt"] is None
    assert done["trajectory"].count(CHARGE) == 1
    assert LEDGER[run["run_id"]]["amount_cents"] == 5600

    audit = (await client.get("/audit", params={"run_id": run["run_id"]})).json()
    assert audit and all(e["run_id"] == run["run_id"] for e in audit)
    assert any(e["event"] == "approval_decision" for e in audit)


async def test_double_approval_single_charge(client):
    run = await _create(client)
    url = f"/runs/{run['run_id']}/approval"

    # Double click: two concurrent approvals, then a late third one.
    first, second = await asyncio.gather(
        client.post(url, json=_approval(run)), client.post(url, json=_approval(run))
    )
    third = await client.post(url, json=_approval(run))

    codes = sorted([first.status_code, second.status_code, third.status_code])
    assert codes == [200, 409, 409]
    assert len(LEDGER) == 1
    assert LEDGER[run["run_id"]]["amount_cents"] == 5600
    final = (await client.get(f"/runs/{run['run_id']}")).json()
    assert final["status"] == "completed"
    assert final["trajectory"].count(CHARGE) == 1


async def test_audit_verify_ok(client):
    run = await _create(client)
    await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run, approved=False))

    resp = await client.get("/audit/verify")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["entries"] > 0
    assert body["first_bad_seq"] is None


async def test_unknown_run_404(client):
    assert (await client.get("/runs/doesnotexist")).status_code == 404
    resp = await client.post(
        "/runs/doesnotexist/approval",
        json={"approved": True, "approver": "alice", "quote_hash": "x"},
    )
    assert resp.status_code == 404


async def test_cors_allows_only_local_ui(client):
    ok = await client.options(
        "/runs",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    preview = await client.options(
        "/runs",
        headers={"Origin": "http://localhost:4173", "Access-Control-Request-Method": "POST"},
    )
    assert preview.headers.get("access-control-allow-origin") == "http://localhost:4173"
    bad = await client.options(
        "/runs",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in bad.headers


async def test_llm_down_returns_terminal_failed_not_500(audit, tmp_path):
    class DownLLM:
        async def extract(self, schema, system, user):
            raise httpx.ConnectError("llm endpoint unreachable")

    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=DownLLM(), tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        resp = await c.post("/runs", json={"message": MESSAGE})
        assert resp.status_code == 200, resp.text
        run = resp.json()
        assert run["status"] == "failed"
        assert run["interrupt"] is None
        again = await c.get(f"/runs/{run['run_id']}")
        assert again.json()["status"] == "failed"


async def test_payment_step_crash_after_approval_is_terminal(client, monkeypatch):
    import agents.tools

    run = await _create(client)

    def down(agent, server):
        raise ConnectionError("token service unreachable")

    monkeypatch.setattr(agents.tools, "issue_token", down)
    resp = await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert LEDGER == {}
    view = (await client.get(f"/runs/{run['run_id']}")).json()
    assert view["status"] == "failed" and view["interrupt"] is None


async def test_run_view_payment_is_typed_in_openapi(client):
    schema = (await client.get("/openapi.json")).json()
    payment = schema["components"]["schemas"]["Payment"]
    assert set(payment["required"]) == {"payment_id", "status", "amount_cents", "currency"}
    assert payment["properties"]["amount_cents"]["type"] == "integer"


async def test_interrupt_keys_match_the_ui_type(client):
    # ui/src/types.ts ApprovalInterrupt mirrors this payload: a new key must update both.
    run = await _create(client)
    assert set(run["interrupt"]) == {"quote", "quote_hash", "offer", "approve_before"}
    types_ts = (Path(__file__).resolve().parent.parent / "ui" / "src" / "types.ts").read_text()
    block = types_ts.split("export interface ApprovalInterrupt {", 1)[1].split("}", 1)[0]
    declared = {
        line.strip().split(":", 1)[0]
        for line in block.splitlines()
        if ":" in line and not line.strip().startswith(("/", "*"))
    }
    assert declared == set(run["interrupt"])


async def test_mode_reports_what_is_real(client, monkeypatch):
    monkeypatch.delenv("TRAVEL_SEARCH_SOURCE", raising=False)
    monkeypatch.delenv("PAYMENTS_PROVIDER", raising=False)
    assert (await client.get("/mode")).json() == {
        "timetable": "simulated",
        "payments": "simulated",
        "payments_flow": "agent",
        "payment_models": ["agent"],
    }
    monkeypatch.setenv("TRAVEL_SEARCH_SOURCE", "live")
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    assert (await client.get("/mode")).json() == {
        "timetable": "live",
        "payments": "stripe_test",
        "payments_flow": "both",
        "payment_models": ["checkout", "wallet"],
    }
    monkeypatch.setenv("PAYMENTS_FLOW", "checkout")
    assert (await client.get("/mode")).json()["payment_models"] == ["checkout"]
    # The old fixed-test-card agent flow is gone: "agent" now means the agent pays via the wallet.
    monkeypatch.setenv("PAYMENTS_FLOW", "agent")
    mode = (await client.get("/mode")).json()
    assert mode["payments_flow"] == "wallet" and mode["payment_models"] == ["wallet"]


def _fare_ids(run: dict, pick) -> list[str]:
    """One fare id per leg of the offer, chosen by ``pick(leg_offer)``."""
    return [pick(leg) for leg in run["interrupt"]["offer"]["legs"]]


def _earliest(leg: dict) -> str:
    return leg["options"][0]["fare_id"]


async def test_interrupt_offer_has_alternatives_and_defaulted_window(client):
    run = await _create(client)
    offer = run["interrupt"]["offer"]

    assert offer["passengers"] == 1 and offer["currency"] == "CHF"
    assert datetime.fromisoformat(offer["expires_at"]) == datetime.fromisoformat(
        run["interrupt"]["approve_before"]
    )
    for leg in offer["legs"]:
        assert leg["window"] == {"earliest": "07:00:00", "latest": None}
        assert leg["window_defaulted"] is True
        # Every train in the window is priced, capped at 12 (07:02 .. 18:02), earliest first.
        assert [o["depart"][11:16] for o in leg["options"]] == [f"{h:02d}:02" for h in range(7, 19)]
        assert [o["fare_type"] for o in leg["options"]] == (
            ["standard"] * 2 + ["saver"] * 7 + ["standard"] * 3
        )
        assert {o["product"] for o in leg["options"]} == {"IC"}
        assert leg["recommended_fare_id"].endswith("-0902-2nd-saver")
    assert [o["fare_id"] for o in run["quote"]["options"]] == [
        leg["recommended_fare_id"] for leg in offer["legs"]
    ]


async def test_quote_endpoint_prices_a_selection_without_side_effects(client, audit):
    run = await _create(client)
    url = f"/runs/{run['run_id']}/quote"
    before = len(audit.read(run["run_id"]))

    recommended = await client.post(
        url, json={"fare_ids": _fare_ids(run, lambda leg: leg["recommended_fare_id"])}
    )
    assert recommended.status_code == 200, recommended.text
    assert recommended.json() == run["quote"]

    other = await client.post(url, json={"fare_ids": _fare_ids(run, _earliest)})
    assert other.status_code == 200, other.text
    body = other.json()
    assert body["total_cents"] == 11200
    assert body["quote_hash"] != run["quote"]["quote_hash"]
    assert [o["fare_type"] for o in body["options"]] == ["standard", "standard"]
    # Pure: no audit entry, no state change, still awaiting the same decision.
    assert len(audit.read(run["run_id"])) == before
    view = (await client.get(f"/runs/{run['run_id']}")).json()
    assert view["interrupt"]["quote_hash"] == run["quote"]["quote_hash"]
    assert LEDGER == {}


async def test_quote_endpoint_errors(client):
    run = await _create(client)
    url = f"/runs/{run['run_id']}/quote"
    one = _fare_ids(run, _earliest)

    assert (await client.post("/runs/nope/quote", json={"fare_ids": one})).status_code == 404
    for bad in ([one[0]], [one[0], one[0]], [one[0], "DR-BER-ZUE-20261202-1502-2nd"], []):
        resp = await client.post(url, json={"fare_ids": bad})
        assert resp.status_code == 422, bad

    await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run, approved=False))
    assert (await client.post(url, json={"fare_ids": one})).status_code == 409


async def test_approve_alternative_selection_charges_the_recomputed_total(client, audit):
    run = await _create(client)
    fare_ids = _fare_ids(run, _earliest)
    quote = (await client.post(f"/runs/{run['run_id']}/quote", json={"fare_ids": fare_ids})).json()

    resp = await client.post(
        f"/runs/{run['run_id']}/approval",
        json={
            "approved": True,
            "approver": "alice",
            "quote_hash": quote["quote_hash"],
            "fare_ids": fare_ids,
        },
    )

    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "completed"
    assert done["quote"]["quote_hash"] == quote["quote_hash"]
    assert done["payment"]["amount_cents"] == quote["total_cents"] == 11200
    assert LEDGER[run["run_id"]]["amount_cents"] == 11200
    held = [
        e.data["args"]["fare_id"]
        for e in audit.read(run["run_id"])
        if e.event == "tool_call" and e.data["tool"] == "travel_search.hold_seat"
    ]
    assert held == fare_ids


async def test_approval_with_fare_outside_offer_is_rejected_without_charge(client, audit):
    run = await _create(client)
    fare_ids = [*_fare_ids(run, _earliest)[:1], "DR-BER-ZUE-20261202-1502-2nd"]

    resp = await client.post(
        f"/runs/{run['run_id']}/approval",
        json={**_approval(run), "fare_ids": fare_ids},
    )

    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "rejected"
    assert "quote_mismatch" in done["flags"]
    assert "travel_search.hold_seat" not in done["trajectory"]
    assert CHARGE not in done["trajectory"]
    assert LEDGER == {}
    (decision,) = [e for e in audit.read(run["run_id"]) if e.event == "approval_decision"]
    assert decision.data["approved"] is False
    assert decision.data["reason"] == "selection not in offer"
