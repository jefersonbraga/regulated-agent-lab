"""After the payment: signed processor events (webhooks) and the run's settlement.

Hermetic: the processor is an httpx.MockTransport, the endpoint secret is a test value, and
every event is built and signed here. No network, no real keys.
"""

import json
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

import regulated_agent_lab.services.payments.server as payments
from regulated_agent_lab.agents.llm import MockLLM
from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.api.server import create_app
from regulated_agent_lab.controls.audit_log import AuditLog
from regulated_agent_lab.controls.policy import ToolPolicy
from regulated_agent_lab.services.payments import events, processor
from regulated_agent_lab.services.payments.server import reset_ledger
from tests.integration.test_api import FIXTURES, MESSAGE, _approval, serve
from tests.wallet_fixtures import seed_card

SECRET = "whsec_test_" + "s" * 24
TEST_KEY = "sk_test_" + "x" * 24
URL = "/webhooks/stripe"


# ---------------------------------------------------------------------------------------------
# Signature verification (pure)
# ---------------------------------------------------------------------------------------------


def test_good_signature_verifies():
    body = b'{"id": "evt_1"}'
    now = 1_800_000_000
    events.verify_signature(body, events.sign(body, SECRET, now), SECRET, now=now)


def test_any_v1_of_several_may_match():
    body = b"{}"
    now = 1_800_000_000
    good = events.sign(body, SECRET, now).split("v1=")[1]
    header = f"t={now},v1={'0' * 64},v1={good},v0=ignored"
    events.verify_signature(body, header, SECRET, now=now)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "t=1800000000",
        "v1=" + "0" * 64,
        "t=notanumber,v1=" + "0" * 64,
        "t=1800000000,v1=" + "0" * 64,
        "t=1800000000,v0=whatever",
    ],
)
def test_bad_or_missing_signature_is_refused(header):
    with pytest.raises(events.SignatureError):
        events.verify_signature(b"{}", header, SECRET, now=1_800_000_000)


def test_signature_with_another_secret_or_body_is_refused():
    now = 1_800_000_000
    header = events.sign(b"{}", "whsec_other", now)
    with pytest.raises(events.SignatureError):
        events.verify_signature(b"{}", header, SECRET, now=now)
    with pytest.raises(events.SignatureError):
        events.verify_signature(b"{ }", events.sign(b"{}", SECRET, now), SECRET, now=now)


@pytest.mark.parametrize("skew", [events.TOLERANCE_S + 1, -(events.TOLERANCE_S + 1), 86_400])
def test_timestamp_outside_tolerance_is_refused(skew):
    now = 1_800_000_000
    header = events.sign(b"{}", SECRET, now - skew)
    with pytest.raises(events.SignatureError, match="tolerance"):
        events.verify_signature(b"{}", header, SECRET, now=now)


def test_timestamp_inside_tolerance_is_accepted():
    now = 1_800_000_000
    header = events.sign(b"{}", SECRET, now - events.TOLERANCE_S)
    events.verify_signature(b"{}", header, SECRET, now=now)


def test_settlement_precedence_ignores_arrival_order():
    assert events.derive_settlement([]) is None
    assert events.derive_settlement(["payment_intent.succeeded"]) == "confirmed"
    assert events.derive_settlement(["charge.refunded", "payment_intent.succeeded"]) == "refunded"
    assert events.derive_settlement(["payment_intent.payment_failed"]) == "failed"
    assert (
        events.derive_settlement(["payment_intent.payment_failed", "payment_intent.succeeded"])
        == "confirmed"
    )
    assert events.derive_settlement(["charge.dispute.created", "charge.refunded"]) == "disputed"


def test_float_amount_is_malformed():
    body = json.dumps(_event("payment_intent.succeeded", "pi_x", amount=56.0)).encode()
    with pytest.raises(events.MalformedEvent):
        events.parse_event(body)


# ---------------------------------------------------------------------------------------------
# The webhook endpoint, end to end: a test-mode run, then processor events
# ---------------------------------------------------------------------------------------------


def _event(
    etype: str,
    payment_id: str | None,
    *,
    amount: int | float = 5600,
    event_id: str | None = None,
    livemode: bool = False,
    currency: str = "chf",
) -> dict:
    if etype.startswith("payment_intent."):
        obj = {"id": payment_id, "object": "payment_intent", "amount": amount, "status": "x"}
    elif etype == "charge.refunded":
        obj = {
            "id": "ch_1",
            "object": "charge",
            "payment_intent": payment_id,
            "amount": 5600,
            "amount_refunded": amount,
            "refunded": True,
        }
    else:
        obj = {"id": "dp_1", "object": "dispute", "payment_intent": payment_id, "amount": amount}
    obj["currency"] = currency
    # Fields a real event carries that must never reach the audit log.
    obj["billing_details"] = {"name": "Alice Example", "email": "alice@example.com"}
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "type": etype,
        "created": int(time.time()),
        "livemode": livemode,
        "data": {"object": obj},
    }


def _signed(event: dict, secret: str = SECRET, ts: int | None = None) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    header = events.sign(body, secret, int(time.time()) if ts is None else ts)
    return body, {"Stripe-Signature": header, "Content-Type": "application/json"}


class FakeProcessor:
    """Captures and PaymentIntent reads, in memory (test mode)."""

    def __init__(self) -> None:
        self.intents: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/payment_intents":
            form = dict(httpx.QueryParams(request.content.decode()))
            pid = f"pi_test_{uuid.uuid4().hex[:10]}"
            self.intents[pid] = {
                "id": pid,
                "object": "payment_intent",
                "amount": int(form["amount"]),
                "currency": form["currency"],
                "status": "succeeded",
                "livemode": False,
                "latest_charge": {"id": "ch_1", "amount_refunded": 0, "disputed": False},
            }
            return httpx.Response(200, json=self.intents[pid])
        if request.method == "GET" and path.startswith("/v1/payment_intents/"):
            assert request.url.params.get("expand[]") == "latest_charge"
            pid = path.rsplit("/", 1)[1]
            if pid not in self.intents:
                return httpx.Response(404, json={"error": {"code": "resource_missing"}})
            return httpx.Response(200, json=self.intents[pid])
        raise AssertionError(f"unexpected request {request.method} {path}")


def install_fake_processor(monkeypatch) -> FakeProcessor:
    """Test-mode payments against an in-memory processor, with the webhook secret set."""
    fake = FakeProcessor()
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    # These tests cover the merchant-initiated capture (the agent pays from the wallet, with a
    # card already saved); test_checkout.py the hosted page, test_wallet.py the rest.
    monkeypatch.setenv("PAYMENTS_FLOW", "wallet")
    seed_card()
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    monkeypatch.setenv(events.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@asynccontextmanager
async def app_client(audit: AuditLog, tmp_path):
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        yield c


@pytest.fixture
def fake_processor(monkeypatch):
    return install_fake_processor(monkeypatch)


@pytest.fixture(autouse=True)
def _clean():
    reset_ledger()
    events.reset_events()
    yield
    reset_ledger()
    events.reset_events()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
async def client(audit, tmp_path):
    async with app_client(audit, tmp_path) as c:
        yield c


async def _paid_run(client) -> dict:
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    done = await client.post(f"/runs/{run['run_id']}/approval", json=_approval(run))
    assert done.status_code == 200, done.text
    view = done.json()
    assert view["status"] == "completed"
    return view


def _processor_entries(audit: AuditLog, run_id: str | None = None) -> list:
    return [e for e in audit.read(run_id) if e.actor == "processor"]


async def test_processor_capture_starts_pending(client, fake_processor):
    view = await _paid_run(client)
    assert view["payment"]["payment_id"].startswith("pi_test_")
    assert view["settlement"] == "pending"
    assert (await client.get(f"/runs/{view['run_id']}")).json()["settlement"] == "pending"
    listed = (await client.get("/runs")).json()
    assert listed[0]["settlement"] == "pending"


async def test_simulated_capture_has_no_settlement(client, monkeypatch):
    monkeypatch.delenv("PAYMENTS_PROVIDER", raising=False)
    view = await _paid_run(client)
    assert view["payment"]["payment_id"].startswith("PAY-")
    assert view["settlement"] is None


async def test_run_without_payment_has_no_settlement(client):
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    assert run["settlement"] is None


@pytest.mark.parametrize(
    ("etype", "settlement", "audit_event"),
    [
        ("payment_intent.succeeded", "confirmed", "payment_confirmed"),
        ("payment_intent.payment_failed", "failed", "payment_failed"),
        ("charge.refunded", "refunded", "payment_refunded"),
        ("charge.dispute.created", "disputed", "dispute_opened"),
    ],
)
async def test_each_event_type_sets_settlement_and_audits(
    client, fake_processor, audit, etype, settlement, audit_event
):
    view = await _paid_run(client)
    run_id, pid = view["run_id"], view["payment"]["payment_id"]
    body, headers = _signed(_event(etype, pid))

    resp = await client.post(URL, content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True, "duplicate": False, "disposition": "matched"}
    assert (await client.get(f"/runs/{run_id}")).json()["settlement"] == settlement
    assert (await client.get("/runs")).json()[0]["settlement"] == settlement
    (entry,) = _processor_entries(audit, run_id)
    assert entry.event == audit_event
    assert entry.data["payment_id"] == pid
    assert entry.data["amount_cents"] == 5600 and isinstance(entry.data["amount_cents"], int)
    assert entry.data["settlement"] == settlement
    assert entry.data["event_type"] == etype
    raw = audit.path.read_text()
    assert "Alice" not in raw and "alice@example.com" not in raw
    assert audit.verify().ok


async def test_refund_after_confirmation_and_out_of_order_events(client, fake_processor):
    view = await _paid_run(client)
    run_id, pid = view["run_id"], view["payment"]["payment_id"]
    for etype in ("charge.refunded", "payment_intent.succeeded"):  # late confirmation
        body, headers = _signed(_event(etype, pid))
        assert (await client.post(URL, content=body, headers=headers)).status_code == 200
    assert (await client.get(f"/runs/{run_id}")).json()["settlement"] == "refunded"


async def test_replayed_event_is_acknowledged_without_a_second_effect(
    client, fake_processor, audit
):
    view = await _paid_run(client)
    run_id, pid = view["run_id"], view["payment"]["payment_id"]
    event = _event("charge.refunded", pid, event_id="evt_replayed_1")
    body, headers = _signed(event)
    first = await client.post(URL, content=body, headers=headers)
    # The same event delivered again (freshly signed, as the processor does on a retry).
    body, headers = _signed(event)
    again = await client.post(URL, content=body, headers=headers)

    assert first.status_code == again.status_code == 200
    assert again.json() == {"received": True, "duplicate": True}
    assert len(_processor_entries(audit, run_id)) == 1
    assert len(events.event_store().all()) == 1


async def test_event_effects_are_retried_when_the_audit_write_failed(
    client, fake_processor, audit, monkeypatch
):
    view = await _paid_run(client)
    body, headers = _signed(_event("payment_intent.succeeded", view["payment"]["payment_id"]))
    original = audit.append

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "append", broken)
    with pytest.raises(OSError):
        await client.post(URL, content=body, headers=headers)
    monkeypatch.setattr(audit, "append", original)
    # The processor redelivers: this time the effect is applied, once.
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.json()["duplicate"] is False
    assert len(_processor_entries(audit, view["run_id"])) == 1


async def test_unknown_payment_id_is_recorded_unmatched(client, fake_processor, audit):
    body, headers = _signed(_event("charge.refunded", "pi_unknown_123"))
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["disposition"] == "unmatched"
    (stored,) = events.event_store().all()
    assert stored["disposition"] == "unmatched" and stored["idempotency_key"] is None
    (entry,) = _processor_entries(audit)
    assert entry.run_id == "processor" and entry.event == "payment_refunded"
    assert entry.data["disposition"] == "unmatched"


async def test_event_for_a_simulated_capture_is_unmatched(client, fake_processor, monkeypatch):
    monkeypatch.delenv("PAYMENTS_PROVIDER", raising=False)
    view = await _paid_run(client)
    body, headers = _signed(_event("charge.refunded", view["payment"]["payment_id"]))
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.json()["disposition"] == "unmatched"
    assert (await client.get(f"/runs/{view['run_id']}")).json()["settlement"] is None


async def test_livemode_event_is_acknowledged_but_never_applied(client, fake_processor, audit):
    view = await _paid_run(client)
    run_id, pid = view["run_id"], view["payment"]["payment_id"]
    body, headers = _signed(_event("charge.refunded", pid, livemode=True))

    resp = await client.post(URL, content=body, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["disposition"] == "livemode_rejected"
    assert (await client.get(f"/runs/{run_id}")).json()["settlement"] == "pending"
    assert _processor_entries(audit, run_id) == []
    (entry,) = _processor_entries(audit)
    assert entry.event == "livemode_event_rejected" and entry.run_id == "processor"
    (stored,) = events.event_store().all()
    assert stored["livemode"] is True and stored["idempotency_key"] is None


async def test_unhandled_event_type_is_acknowledged_and_not_stored(client, fake_processor):
    event = _event("payment_intent.succeeded", "pi_x")
    event["type"] = "customer.created"
    body, headers = _signed(event)
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.status_code == 200
    assert events.event_store().all() == []


async def test_no_secret_means_503_and_nothing_stored(client, monkeypatch, audit):
    monkeypatch.delenv(events.WEBHOOK_SECRET_ENV, raising=False)
    body, headers = _signed(_event("charge.refunded", "pi_x"))
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.status_code == 503
    assert events.event_store().all() == []
    assert _processor_entries(audit) == []


@pytest.mark.parametrize("case", ["missing", "bad", "other_secret", "old", "tampered_body"])
async def test_bad_signature_is_400_and_nothing_stored(client, fake_processor, audit, case):
    body, headers = _signed(_event("charge.refunded", "pi_x"))
    if case == "missing":
        del headers["Stripe-Signature"]
    elif case == "bad":
        headers["Stripe-Signature"] = f"t={int(time.time())},v1={'0' * 64}"
    elif case == "other_secret":
        body, headers = _signed(_event("charge.refunded", "pi_x"), secret="whsec_other")
    elif case == "old":
        body, headers = _signed(
            _event("charge.refunded", "pi_x"), ts=int(time.time()) - events.TOLERANCE_S - 60
        )
    elif case == "tampered_body":
        body = body.replace(b"5600", b"9999")
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid signature"
    assert events.event_store().all() == []
    assert _processor_entries(audit) == []


async def test_signed_but_malformed_body_is_400(client, fake_processor):
    body = b'{"id": "not-an-event"}'
    headers = {"Stripe-Signature": events.sign(body, SECRET, int(time.time()))}
    resp = await client.post(URL, content=body, headers=headers)
    assert resp.status_code == 400
    assert events.event_store().all() == []


async def test_event_before_the_ledger_row_still_settles_the_run(client, fake_processor):
    """A webhook can beat the graph: the run picks up the stored event when it is recorded."""
    view = await _paid_run(client)
    store = events.event_store()
    # Simulate an event stored as unmatched for this payment (it arrived before the capture row).
    store.insert_or_get(
        {
            "event_id": "evt_early",
            "type": "payment_intent.succeeded",
            "payment_id": view["payment"]["payment_id"],
            "amount_cents": 5600,
            "currency": "CHF",
            "livemode": False,
            "created": int(time.time()),
            "disposition": "unmatched",
            "idempotency_key": None,
        }
    )
    assert events.initial_settlement(view["run_id"]) == "confirmed"


# ---------------------------------------------------------------------------------------------
# scripts/send_test_webhook.py: a synthetic event the endpoint accepts (no network here)
# ---------------------------------------------------------------------------------------------


def _load_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "send_test_webhook.py"
    spec = importlib.util.spec_from_file_location("send_test_webhook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_send_test_webhook_signs_a_synthetic_event(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setenv(events.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: False)
    sent: list[tuple[bytes, dict]] = []

    def fake_post(url, content, headers, timeout):
        sent.append((content, headers))
        return httpx.Response(200, json={"received": True})

    monkeypatch.setattr(script.httpx, "post", fake_post)
    assert script.main(["refunded", "--payment-id", "pi_demo_1", "--amount-cents", "5600"]) == 0
    ((body, headers),) = sent
    events.verify_signature(body, headers["Stripe-Signature"], SECRET)
    event = events.parse_event(body)
    assert event.event_id.startswith("evt_synthetic_")
    assert event.type == "charge.refunded" and event.payment_id == "pi_demo_1"
    assert event.amount_cents == 5600 and event.livemode is False
    assert json.loads(body)["data"]["object"]["metadata"]["synthetic_test_event"] == "true"
    out = capsys.readouterr().out
    assert "SYNTHETIC TEST EVENT" in out and SECRET not in out


def test_send_test_webhook_without_a_secret_exits_2(monkeypatch):
    script = _load_script()
    monkeypatch.delenv(events.WEBHOOK_SECRET_ENV, raising=False)
    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: False)
    assert script.main(["confirmed", "--payment-id", "pi_x"]) == 2


def _ts_fields(interface: str) -> set[str]:
    from pathlib import Path

    types_ts = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "types.ts").read_text()
    block = types_ts.split(f"export interface {interface} {{", 1)[1].split("\n}", 1)[0]
    return {
        line.strip().split(":", 1)[0].rstrip("?")
        for line in block.splitlines()
        if ":" in line and not line.strip().startswith(("/", "*"))
    }


async def test_settlement_is_mirrored_in_the_ui_types(client):
    from regulated_agent_lab.agents.persistence import SETTLEMENTS, RunSummary
    from regulated_agent_lab.api.server import RunView

    assert _ts_fields("RunView") == set(RunView.model_fields)
    assert _ts_fields("RunSummary") == set(RunSummary.model_fields)
    assert set(SETTLEMENTS) == set(events.SETTLEMENTS)
    schema = (await client.get("/openapi.json")).json()["components"]["schemas"]
    assert "settlement" in schema["RunView"]["properties"]
