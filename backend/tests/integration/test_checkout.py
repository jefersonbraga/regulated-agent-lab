"""Hosted Checkout (PAYMENTS_FLOW=checkout): the customer pays on the processor's page.

Hermetic: the processor is an in-memory fake behind httpx.MockTransport, the webhook secret is a
test value and every event is built and signed here. No network, no real keys.
"""

import asyncio
import json
import time
import uuid
from urllib.parse import parse_qs

import httpx
import pytest

import regulated_agent_lab.services.payments.server as payments
from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.controls.identity import issue_approval_token
from regulated_agent_lab.controls.policy import PolicyViolation, ToolPolicy
from regulated_agent_lab.services.payments import checkout, events, processor
from regulated_agent_lab.services.payments.server import LEDGER, reset_ledger
from tests.integration.test_api import MESSAGE, _approval
from tests.integration.test_processor_events import SECRET, TEST_KEY, app_client
from tests.wallet_fixtures import seed_card

URL = "/webhooks/stripe"
RECEIPT = "https://pay.stripe.com/receipts/payment/test_receipt_1"


class FakeCheckout:
    """Checkout Sessions, PaymentIntents and their latest charge, in memory (test mode)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.intents: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []

    def pay(self, session_id: str, *, amount: int | None = None, currency: str = "chf") -> str:
        """The customer pays on the hosted page: the session completes (maybe tampered with)."""
        session = self.sessions[session_id]
        pid = f"pi_test_{uuid.uuid4().hex[:10]}"
        self.intents[pid] = {
            "id": pid,
            "object": "payment_intent",
            "amount": amount if amount is not None else session["amount_total"],
            "currency": currency,
            "status": "succeeded",
            "livemode": False,
            "latest_charge": {"id": "ch_1", "receipt_url": RECEIPT, "amount_refunded": 0},
        }
        session.update(
            status="complete",
            payment_status="paid",
            payment_intent=pid,
            amount_total=amount if amount is not None else session["amount_total"],
            currency=currency,
        )
        return pid

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/checkout/sessions":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            sid = f"cs_test_{uuid.uuid4().hex[:12]}"
            self.sessions[sid] = {
                "id": sid,
                "object": "checkout.session",
                "url": f"https://checkout.stripe.com/c/pay/{sid}",
                "status": "open",
                "payment_status": "unpaid",
                "amount_total": int(form["line_items[0][price_data][unit_amount]"]),
                "currency": form["line_items[0][price_data][currency]"],
                "client_reference_id": form["client_reference_id"],
                "metadata": {"run_id": form["metadata[run_id]"]},
                "payment_intent": None,
                "expires_at": int(form["expires_at"]),
                "livemode": False,
                "customer_details": {"email": "alice@example.com", "name": "Alice Example"},
            }
            return httpx.Response(200, json=self.sessions[sid])
        if path.startswith("/v1/checkout/sessions/"):
            parts = path.split("/")
            sid = parts[4]
            if sid not in self.sessions:
                return httpx.Response(404, json={"error": {"code": "resource_missing"}})
            session = self.sessions[sid]
            if request.method == "POST" and path.endswith("/expire"):
                if session["status"] != "open":
                    return httpx.Response(400, json={"error": {"code": "invalid_state"}})
                session["status"] = "expired"
                return httpx.Response(200, json=session)
            assert request.url.params.get("expand[]") == "payment_intent.latest_charge"
            body = dict(session)
            if body["payment_intent"]:
                body["payment_intent"] = self.intents[body["payment_intent"]]
            return httpx.Response(200, json=body)
        if request.method == "POST" and path == "/v1/payment_intents":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["expand[]"] == "latest_charge"
            pid = f"pi_test_{uuid.uuid4().hex[:10]}"
            self.intents[pid] = {
                "id": pid,
                "object": "payment_intent",
                "amount": int(form["amount"]),
                "currency": form["currency"],
                "status": "succeeded",
                "livemode": False,
                "latest_charge": {"id": "ch_2", "receipt_url": RECEIPT, "amount_refunded": 0},
            }
            return httpx.Response(200, json=self.intents[pid])
        if request.method == "GET" and path.startswith("/v1/payment_intents/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.intents:
                return httpx.Response(404, json={"error": {"code": "resource_missing"}})
            return httpx.Response(200, json=self.intents[pid])
        raise AssertionError(f"unexpected request {request.method} {path}")


@pytest.fixture
def fake(monkeypatch):
    fake = FakeCheckout()
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.delenv("PAYMENTS_FLOW", raising=False)  # default with stripe_test: checkout
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    monkeypatch.setenv(events.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture(autouse=True)
def _clean():
    reset_ledger()
    events.reset_events()
    yield
    reset_ledger()
    events.reset_events()


@pytest.fixture
def audit(tmp_path):
    from regulated_agent_lab.controls.audit_log import AuditLog

    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
async def client(audit, tmp_path):
    async with app_client(audit, tmp_path) as c:
        yield c


async def _awaiting_payment(client) -> dict:
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    resp = await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run))
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["status"] == "awaiting_payment", view
    return view


def _session_event(session: dict, etype: str = events.CHECKOUT_COMPLETED) -> tuple[bytes, dict]:
    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "type": etype,
        "created": int(time.time()),
        "livemode": False,
        "data": {"object": session},
    }
    body = json.dumps(event).encode()
    header = events.sign(body, SECRET, int(time.time()))
    return body, {"Stripe-Signature": header, "Content-Type": "application/json"}


def _events(audit, run_id, name):
    return [e for e in audit.read(run_id) if e.event == name]


# --- opening the checkout ---------------------------------------------------------------------


async def test_approval_opens_a_checkout_for_exactly_the_approved_total(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id = view["run_id"]

    (create,) = [r for r in fake.requests if r.url.path == "/v1/checkout/sessions"]
    form = {k: v[0] for k, v in parse_qs(create.content.decode()).items()}
    quote_hash = view["quote"]["quote_hash"]
    assert form["mode"] == "payment"
    assert form["line_items[0][quantity]"] == "1"
    assert form["line_items[0][price_data][unit_amount]"] == "5600"
    assert form["line_items[0][price_data][currency]"] == "chf"
    assert form["line_items[0][price_data][product_data][name]"] == (
        f"Demo Rail trip {quote_hash[:8]}"
    )
    assert form["client_reference_id"] == run_id
    for prefix in ("metadata", "payment_intent_data[metadata]"):
        assert form[f"{prefix}[run_id]"] == run_id
        assert form[f"{prefix}[quote_hash]"] == quote_hash
        assert form[f"{prefix}[approval_ref]"] == f"alice:{quote_hash}"
    assert form["success_url"] == f"http://localhost:5173/?run={run_id}&checkout=success"
    assert form["cancel_url"] == f"http://localhost:5173/?run={run_id}&checkout=cancel"
    assert int(form["expires_at"]) >= int(time.time()) + processor.CHECKOUT_MIN_EXPIRY_S
    assert create.headers["Idempotency-Key"] == f"regulated-agent-lab-checkout-{run_id}"

    checkout_view = view["checkout"]
    assert checkout_view["checkout_url"].startswith("https://checkout.stripe.com/")
    assert checkout_view["amount_cents"] == 5600 and checkout_view["currency"] == "CHF"
    assert view["interrupt"] is None and view["payment"] is None
    assert "payments.create_checkout" in view["trajectory"]
    assert "payments.charge" not in view["trajectory"]
    assert LEDGER[run_id]["status"] == "checkout_pending"
    assert LEDGER[run_id]["checkout_session_id"] == checkout_view["session_id"]
    (created,) = _events(audit, run_id, "checkout_created")
    assert created.data["amount_cents"] == 5600
    # The browser's return URL alone changes nothing: the run still waits.
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_payment"
    listed = (await client.get("/runs")).json()[0]
    assert listed["status"] == "awaiting_payment"


async def test_public_app_url_sets_the_return_urls(client, fake, monkeypatch):
    monkeypatch.setenv("PUBLIC_APP_URL", "https://lab.example.org/")
    view = await _awaiting_payment(client)
    (create,) = [r for r in fake.requests if r.url.path == "/v1/checkout/sessions"]
    form = {k: v[0] for k, v in parse_qs(create.content.decode()).items()}
    assert form["success_url"] == f"https://lab.example.org/?run={view['run_id']}&checkout=success"


async def test_an_approval_body_cannot_answer_the_payment_pause(client, fake):
    view = await _awaiting_payment(client)
    again = await client.post(
        f"/runs/{view['run_id']}/approval",
        json={"approved": True, "approver": "x", "quote_hash": view["quote"]["quote_hash"]},
    )
    assert again.status_code == 409
    quote = await client.post(f"/runs/{view['run_id']}/quote", json={"fare_ids": ["a", "b"]})
    assert quote.status_code == 409


def test_policy_create_checkout_is_booking_only_and_approval_gated():
    policy = ToolPolicy.from_yaml()
    for agent in ("planner", "pricing", "supervisor"):
        with pytest.raises(PolicyViolation):
            policy.check(agent, "payments.create_checkout", approved=True)
    with pytest.raises(PolicyViolation, match="approval"):
        policy.check("booking", "payments.create_checkout", approved=False)
    policy.check("booking", "payments.create_checkout", approved=True)
    policy.check("booking", "payments.checkout_status")  # read-only, no approval needed


@pytest.mark.parametrize(
    "case", ["no_token", "other_amount", "other_quote_hash", "other_run", "other_approver"]
)
async def test_gate_requires_a_matching_approval_token(tmp_path, fake, case):
    from regulated_agent_lab.controls.audit_log import AuditLog

    audit = AuditLog(tmp_path / "a.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    claims = {
        "run_id": "run-1",
        "approver": "alice",
        "quote_hash": "h" * 64,
        "amount_cents": 5600,
        "currency": "CHF",
    }
    args = {
        "hold_id": "H-1",
        "amount_cents": 5600,
        "currency": "CHF",
        "idempotency_key": "run-1",
        "approval_ref": f"alice:{'h' * 64}",
        "quote_hash": "h" * 64,
    }
    if case == "other_amount":
        args["amount_cents"] = 9999
    elif case == "other_quote_hash":
        args["quote_hash"] = "x" * 64
    elif case == "other_run":
        claims["run_id"] = "run-2"
    elif case == "other_approver":
        args["approval_ref"] = f"mallory:{'h' * 64}"
    token = None if case == "no_token" else issue_approval_token(**claims)
    with pytest.raises(PolicyViolation, match="approval"):
        await tools.call("run-1", "booking", "payments.create_checkout", args, approval_token=token)
    assert not [r for r in fake.requests if r.url.path == "/v1/checkout/sessions"]
    assert LEDGER == {}
    (denied,) = [e for e in audit.read("run-1") if e.event == "policy_denied"]
    assert denied.data["tool"] == "payments.create_checkout"


async def test_checkout_tool_needs_the_test_mode_processor(monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from regulated_agent_lab.controls.identity import issue_token

    monkeypatch.delenv("PAYMENTS_PROVIDER", raising=False)
    args = {
        "agent_token": issue_token("booking", "payments"),
        "hold_id": "H-1",
        "amount_cents": 5600,
        "currency": "CHF",
        "idempotency_key": "run-1",
        "approval_ref": "alice:hash",
        "quote_hash": "hash",
    }
    async with Client(payments.mcp) as c:
        with pytest.raises(ToolError, match="test-mode"):
            await c.call_tool("create_checkout", args)
        with pytest.raises(ToolError, match="unauthorized"):
            await c.call_tool(
                "create_checkout", {**args, "agent_token": issue_token("pricing", "payments")}
            )
    assert LEDGER == {}


async def test_simulated_provider_keeps_the_agent_flow(client, monkeypatch):
    monkeypatch.delenv("PAYMENTS_PROVIDER", raising=False)
    monkeypatch.setenv("PAYMENTS_FLOW", "checkout")
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    done = (await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run))).json()
    assert done["status"] == "completed"
    assert done["payment"]["payment_id"].startswith("PAY-")
    assert done["payment"]["receipt_url"] is None
    assert "payments.create_checkout" not in done["trajectory"]


# --- completion on processor truth -----------------------------------------------------------


async def test_signed_webhook_completes_the_run(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id, sid = view["run_id"], view["checkout"]["session_id"]
    pid = fake.pay(sid)

    body, headers = _session_event(fake.sessions[sid])
    resp = await client.post(URL, content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["disposition"] == "matched"
    done = (await client.get(f"/runs/{run_id}")).json()
    assert done["status"] == "completed"
    assert done["checkout"] is None
    assert done["payment"] == {
        "payment_id": pid,
        "status": "captured",
        "amount_cents": 5600,
        "currency": "CHF",
        "receipt_url": RECEIPT,
    }
    assert done["settlement"] == "confirmed"
    assert LEDGER[run_id]["status"] == "captured" and LEDGER[run_id]["payment_id"] == pid
    (confirmed,) = _events(audit, run_id, "payment_confirmed")
    assert confirmed.actor == "processor"
    assert confirmed.data["source"] == "webhook" and confirmed.data["payment_id"] == pid
    assert done["trajectory"].count("payments.checkout_status") == 1
    raw = audit.path.read_text()
    assert "alice@example.com" not in raw and "Alice Example" not in raw
    assert audit.verify().ok


async def test_refresh_completes_the_run_without_a_webhook(client, fake, audit, monkeypatch):
    monkeypatch.delenv(events.WEBHOOK_SECRET_ENV, raising=False)
    view = await _awaiting_payment(client)
    run_id = view["run_id"]
    # Not paid yet: a refresh (e.g. the browser came back early) leaves the run waiting.
    early = await client.post(f"/runs/{run_id}/payment/refresh")
    assert early.status_code == 200 and early.json()["status"] == "awaiting_payment"

    pid = fake.pay(view["checkout"]["session_id"])
    resp = await client.post(f"/runs/{run_id}/payment/refresh")

    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "completed"
    assert done["payment"]["payment_id"] == pid and done["payment"]["receipt_url"] == RECEIPT
    (confirmed,) = _events(audit, run_id, "payment_confirmed")
    assert confirmed.data["source"] == "refresh"


async def test_webhook_and_refresh_racing_capture_once(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id, sid = view["run_id"], view["checkout"]["session_id"]
    fake.pay(sid)
    body, headers = _session_event(fake.sessions[sid])

    results = await asyncio.gather(
        client.post(URL, content=body, headers=headers),
        client.post(f"/runs/{run_id}/payment/refresh"),
        client.post(f"/runs/{run_id}/payment/refresh"),
    )
    assert [r.status_code for r in results] == [200, 200, 200]
    # A late redelivery of the same event, and one more refresh.
    again = await client.post(URL, content=body, headers=headers)
    assert again.json()["duplicate"] is True
    late = await client.post(f"/runs/{run_id}/payment/refresh")
    assert late.status_code == 200 and late.json()["status"] == "completed"

    done = (await client.get(f"/runs/{run_id}")).json()
    assert done["status"] == "completed"
    assert len(_events(audit, run_id, "payment_confirmed")) == 1
    assert done["trajectory"].count("payments.checkout_status") == 1
    assert len(LEDGER) == 1


async def test_amount_mismatch_is_refused_and_flagged(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id = view["run_id"]
    fake.pay(view["checkout"]["session_id"], amount=1)

    done = (await client.post(f"/runs/{run_id}/payment/refresh")).json()

    assert done["status"] == "refused"
    assert "amount_mismatch" in done["flags"]
    assert done["payment"] is None
    assert LEDGER[run_id]["status"] == "amount_mismatch"
    (entry,) = _events(audit, run_id, "checkout_amount_mismatch")
    assert entry.data["amount_cents"] == 1
    assert _events(audit, run_id, "payment_confirmed") == []


async def test_currency_mismatch_is_refused(client, fake):
    view = await _awaiting_payment(client)
    fake.pay(view["checkout"]["session_id"], currency="eur")
    done = (await client.post(f"/runs/{view['run_id']}/payment/refresh")).json()
    assert done["status"] == "refused" and "amount_mismatch" in done["flags"]


async def test_session_of_another_run_is_never_applied(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id, sid = view["run_id"], view["checkout"]["session_id"]
    fake.pay(sid)
    fake.sessions[sid]["client_reference_id"] = "someone-else"
    body, headers = _session_event(fake.sessions[sid])
    assert (await client.post(URL, content=body, headers=headers)).status_code == 200
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_payment"
    assert LEDGER[run_id]["status"] == "checkout_pending"
    assert _events(audit, run_id, "checkout_refused")


async def test_expired_checkout_fails_the_run(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id, sid = view["run_id"], view["checkout"]["session_id"]
    fake.sessions[sid]["status"] = "expired"
    body, headers = _session_event(fake.sessions[sid], events.CHECKOUT_EXPIRED)

    assert (await client.post(URL, content=body, headers=headers)).status_code == 200

    done = (await client.get(f"/runs/{run_id}")).json()
    assert done["status"] == "failed" and done["payment"] is None
    assert LEDGER[run_id]["status"] == "checkout_expired"
    (entry,) = _events(audit, run_id, "checkout_expired")
    assert entry.data["source"] == "webhook"
    (ended,) = _events(audit, run_id, "checkout_ended")
    assert ended.data["reason"] == "checkout expired"


async def test_cancel_closes_the_session_and_fails_the_run(client, fake, audit):
    view = await _awaiting_payment(client)
    run_id, sid = view["run_id"], view["checkout"]["session_id"]

    done = (await client.post(f"/runs/{run_id}/payment/cancel")).json()

    assert done["status"] == "failed"
    assert fake.sessions[sid]["status"] == "expired"
    assert LEDGER[run_id]["status"] == "checkout_canceled"
    (entry,) = _events(audit, run_id, "checkout_expired")
    assert entry.data["source"] == "cancel" and entry.data["reason"] == "canceled by customer"


async def test_cancel_after_paying_still_captures(client, fake):
    view = await _awaiting_payment(client)
    fake.pay(view["checkout"]["session_id"])
    done = (await client.post(f"/runs/{view['run_id']}/payment/cancel")).json()
    assert done["status"] == "completed" and done["payment"]["receipt_url"] == RECEIPT


async def test_refresh_on_a_run_without_checkout_is_409(client, fake):
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    assert (await client.post(f"/runs/{run['run_id']}/payment/refresh")).status_code == 409
    assert (await client.post("/runs/nope/payment/refresh")).status_code == 404


async def test_processor_down_on_refresh_is_502_and_changes_nothing(client, fake, monkeypatch):
    view = await _awaiting_payment(client)

    def down(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", httpx.MockTransport(down))
    resp = await client.post(f"/runs/{view['run_id']}/payment/refresh")
    assert resp.status_code == 502
    assert (await client.get(f"/runs/{view['run_id']}")).json()["status"] == "awaiting_payment"


def test_apply_session_refuses_a_livemode_session(fake):
    with pytest.raises(processor.ProcessorNotConfigured):
        checkout.apply_session({"id": "cs_test_x", "livemode": True})


async def test_reconcile_skips_checkouts_that_never_completed(client, fake):
    view = await _awaiting_payment(client)
    report = (await client.post("/reconcile")).json()["report"]
    assert report["checked"] == 0 and report["skipped_simulated"] == 0
    (skipped,) = report["skipped"]
    assert skipped["run_id"] == view["run_id"] and "checkout" in skipped["reason"]


# --- the agent pays from the wallet, with a receipt too --------------------------------------


async def test_wallet_flow_stores_the_receipt_url(client, fake, monkeypatch):
    monkeypatch.setenv("PAYMENTS_FLOW", "wallet")
    seed_card()
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    done = (await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run))).json()
    assert done["status"] == "completed"
    assert done["payment"]["payment_id"].startswith("pi_test_")
    assert done["payment"]["receipt_url"] == RECEIPT
    assert "payments.charge" in done["trajectory"]
    assert LEDGER[run["run_id"]]["receipt_url"] == RECEIPT


def test_receipt_url_must_be_on_the_processor_domain():
    assert processor.receipt_url({"latest_charge": {"receipt_url": RECEIPT}}) == RECEIPT
    for bad in ("http://pay.stripe.com/x", "https://evil.example/x", "javascript:alert(1)", 5):
        assert processor.receipt_url({"latest_charge": {"receipt_url": bad}}) is None


def test_invalid_payments_flow_fails_closed(monkeypatch):
    from regulated_agent_lab.agents.booking import payments_flow

    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.setenv("PAYMENTS_FLOW", "browser")
    with pytest.raises(ValueError):
        payments_flow()
