"""The agent-platform wallet: saved card, mandate, scoped single-use tokens, SCA fallback.

Hermetic: the processor is an in-memory fake behind httpx.MockTransport (Customers, setup
sessions, SetupIntents, PaymentMethods, off-session PaymentIntents and the hosted Checkout of
tests/integration/test_checkout.py). No network, no real keys, no card number anywhere in the lab.
"""

import asyncio
import json
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from langgraph.types import Command

import regulated_agent_lab.services.payments.server as payments
from regulated_agent_lab.agents import booking
from regulated_agent_lab.agents.graph import build_graph, initial_state
from regulated_agent_lab.agents.llm import MockLLM
from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.controls.audit_log import AuditLog
from regulated_agent_lab.controls.identity import (
    issue_approval_token,
    issue_token,
    verify_approval_token,
)
from regulated_agent_lab.controls.policy import PolicyViolation, ToolPolicy
from regulated_agent_lab.services.payments import events, processor
from regulated_agent_lab.services.payments.server import LEDGER, reset_ledger
from regulated_agent_lab.services.wallet import server as wallet_server
from regulated_agent_lab.services.wallet import vault
from tests.integration.test_api import FIXTURES, MESSAGE, serve
from tests.integration.test_checkout import FakeCheckout, _session_event
from tests.integration.test_processor_events import SECRET, TEST_KEY
from tests.wallet_fixtures import CUSTOMER, LOCAL_PAYER, PM_VISA, seed_card

URL = "/webhooks/stripe"
TOTAL = 5600  # the mock round trip, Zurich HB - Bern and back, 1 adult
CARD_SECRETS = ("fp_secret_fingerprint", "alice@example.com", "Alice Example")


class FakeWalletProcessor(FakeCheckout):
    """FakeCheckout plus what the wallet uses: Customers, setup sessions, off-session charges."""

    def __init__(self) -> None:
        super().__init__()
        self.customers: dict[str, dict] = {}
        self.setups: dict[str, dict] = {}
        self.methods: dict[str, dict] = {}
        self.setup_intents: dict[str, dict] = {}
        # PaymentMethods whose bank asks for authentication off-session (e.g. 4000 0027 6000 3184).
        self.sca_methods: set[str] = set()
        self.detached: list[str] = []
        # The processor rejects the PaymentIntent request itself (e.g. an outdated parameter).
        self.reject_intents = False

    def complete_setup(self, sid: str, *, last4: str = "4242", brand: str = "visa") -> str:
        """The customer typed a card on the hosted setup form: the session completes."""
        session = self.setups[sid]
        pm = f"pm_test_{uuid.uuid4().hex[:10]}"
        self.methods[pm] = {
            "id": pm,
            "object": "payment_method",
            "type": "card",
            "customer": session["customer"],
            "card": {
                "brand": brand,
                "last4": last4,
                "exp_month": 12,
                "exp_year": 2034,
                "fingerprint": "fp_secret_fingerprint",
                "country": "US",
            },
            "billing_details": {"email": "alice@example.com", "name": "Alice Example"},
        }
        seti = f"seti_{uuid.uuid4().hex[:10]}"
        self.setup_intents[seti] = {"id": seti, "status": "succeeded", "payment_method": pm}
        session.update(status="complete", setup_intent=seti)
        return pm

    def _setup_body(self, sid: str) -> dict:
        body = dict(self.setups[sid])
        if body["setup_intent"]:
            intent = dict(self.setup_intents[body["setup_intent"]])
            intent["payment_method"] = self.methods[intent["payment_method"]]
            body["setup_intent"] = intent
        return body

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.method == "POST" and path == "/v1/customers":
            self.requests.append(request)
            cid = f"cus_{uuid.uuid4().hex[:12]}"
            self.customers[cid] = {"id": cid, "metadata": {"lab_user": form["metadata[lab_user]"]}}
            return httpx.Response(200, json={**self.customers[cid], "livemode": False})
        if request.method == "POST" and path == "/v1/checkout/sessions" and form["mode"] == "setup":
            self.requests.append(request)
            sid = f"cs_test_{uuid.uuid4().hex[:12]}"
            self.setups[sid] = {
                "id": sid,
                "object": "checkout.session",
                "mode": "setup",
                "url": f"https://checkout.stripe.com/c/setup/{sid}",
                "status": "open",
                "customer": form["customer"],
                "client_reference_id": form["client_reference_id"],
                "metadata": {"lab_user": form["metadata[lab_user]"]},
                "setup_intent": None,
                "livemode": False,
                "customer_details": {"email": "alice@example.com", "name": "Alice Example"},
            }
            return httpx.Response(200, json=self.setups[sid])
        if path.startswith("/v1/checkout/sessions/") and path.split("/")[4] in self.setups:
            self.requests.append(request)
            assert request.url.params.get("expand[]") == "setup_intent.payment_method"
            return httpx.Response(200, json=self._setup_body(path.split("/")[4]))
        if request.method == "POST" and path.startswith("/v1/payment_methods/"):
            self.requests.append(request)
            pm = path.split("/")[3]
            self.detached.append(pm)
            return httpx.Response(200, json={**self.methods.get(pm, {"id": pm}), "livemode": False})
        if request.method == "POST" and path == "/v1/payment_intents":
            if self.reject_intents:
                self.requests.append(request)
                error = {
                    "type": "invalid_request_error",
                    "message": "parameter no longer supported",
                }
                return httpx.Response(400, json={"error": error})
            if form.get("payment_method") in self.sca_methods:
                self.requests.append(request)
                pid = f"pi_test_{uuid.uuid4().hex[:10]}"
                error = {
                    "type": "card_error",
                    "code": "authentication_required",
                    "payment_intent": {"id": pid, "status": "requires_payment_method"},
                }
                return httpx.Response(402, json={"error": error})
        return super().handler(request)

    def posts(self, path: str) -> list[dict]:
        return [
            {k: v[0] for k, v in parse_qs(r.content.decode()).items()} | {"_headers": r.headers}
            for r in self.requests
            if r.method == "POST" and r.url.path == path
        ]


class RecordingLLM(MockLLM):
    """The mock model, keeping every prompt it was sent."""

    def __init__(self, fixtures: dict) -> None:
        super().__init__(fixtures)
        self.prompts: list[str] = []

    async def extract(self, schema, system: str, user: str):
        self.prompts.append(f"{system}\n{user}")
        return await super().extract(schema, system, user)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeWalletProcessor()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.delenv("PAYMENTS_FLOW", raising=False)  # default with stripe_test: both
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    monkeypatch.setenv(events.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", transport)
    monkeypatch.setattr(vault, "PROCESSOR_TRANSPORT", transport)
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
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def llm():
    return RecordingLLM(FIXTURES)


@pytest.fixture
async def client(audit, tmp_path, llm):
    from regulated_agent_lab.api.server import create_app

    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(llm=llm, tools=tools, audit=audit, db_path=tmp_path / "lab.db")
    async with serve(app) as c:
        yield c


async def _paused(client) -> dict:
    run = (await client.post("/runs", json={"message": MESSAGE})).json()
    assert run["status"] == "awaiting_approval", run
    return run


def _decision(run: dict, model: str | None = None) -> dict:
    body = {"approved": True, "approver": "alice", "quote_hash": run["interrupt"]["quote_hash"]}
    if model is not None:
        body["payment_model"] = model
    return body


async def _approve(client, run: dict, model: str | None = None) -> dict:
    resp = await client.post(f"/runs/{run['run_id']}/approval", json=_decision(run, model))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _events(audit, run_id, name):
    return [e for e in audit.read(run_id) if e.event == name]


async def _save_card_through_the_form(client, fake, *, last4: str = "4242") -> str:
    setup = await client.post("/wallet/setup")
    assert setup.status_code == 200, setup.text
    (sid,) = list(fake.setups)[-1:]
    assert setup.json()["setup_url"] == fake.setups[sid]["url"]
    pm = fake.complete_setup(sid, last4=last4)
    done = await client.post("/wallet/setup/complete")
    assert done.status_code == 200, done.text
    assert done.json()["setup_state"] == "saved"
    return pm


# --- saving a card (mode=setup) ----------------------------------------------------------------


async def test_setup_session_is_mode_setup_for_a_reused_customer(client, fake, audit):
    first = await client.post("/wallet/setup")
    second = await client.post("/wallet/setup")

    assert first.status_code == second.status_code == 200
    assert first.json()["setup_url"].startswith("https://checkout.stripe.com/")
    (customer,) = fake.posts("/v1/customers")  # created once, reused for the second form
    assert customer["metadata[lab_user]"] == LOCAL_PAYER
    forms = [f for f in fake.posts("/v1/checkout/sessions") if f.get("mode") == "setup"]
    assert len(forms) == 2
    for form in forms:
        assert form["customer"] == next(iter(fake.customers))
        assert form["client_reference_id"] == LOCAL_PAYER
        assert form["metadata[lab_user]"] == LOCAL_PAYER
        assert form["setup_intent_data[metadata][lab_user]"] == LOCAL_PAYER
        assert "payment_method_types[0]" not in form  # rejected by the processor's current API
        assert form["currency"] == "chf"
        assert form["success_url"] == "http://localhost:5173/?wallet=saved"
        assert form["cancel_url"] == "http://localhost:5173/?wallet=canceled"
        assert "line_items[0][price_data][unit_amount]" not in form  # nothing is paid
    assert len(_events(audit, "wallet", "wallet_setup_started")) == 2


async def test_saving_a_card_keeps_only_ids_brand_last4_and_expiry(client, fake, audit):
    before = (await client.get("/wallet")).json()
    assert before["card"] is None and before["mandate_cents"] == vault.DEFAULT_MANDATE_CENTS
    assert before["enabled"] is True

    pm = await _save_card_through_the_form(client, fake)

    view = (await client.get("/wallet")).json()
    assert view["card"]["brand"] == "visa" and view["card"]["last4"] == "4242"
    assert (view["card"]["exp_month"], view["card"]["exp_year"]) == (12, 2034)
    assert "payment_method_id" not in view["card"] and "customer_id" not in view["card"]
    stored = vault.store().card(LOCAL_PAYER)
    assert stored["payment_method_id"] == pm
    assert set(stored) == {
        "user",
        "customer_id",
        "payment_method_id",
        "brand",
        "last4",
        "exp_month",
        "exp_year",
        "created_at",
    }
    # Nothing else from the processor's answer reached the wallet's file.
    raw = _db_dump(vault.wallet_db_path())
    for secret in CARD_SECRETS:
        assert secret.encode() not in raw
    (saved,) = _events(audit, "wallet", "wallet_card_saved")
    assert saved.data == {
        "user": LOCAL_PAYER,
        "source": "return",
        "session_id": saved.data["session_id"],
    }
    # A second return changes nothing and audits nothing.
    again = (await client.post("/wallet/setup/complete")).json()
    assert again["setup_state"] == "saved"
    assert len(_events(audit, "wallet", "wallet_card_saved")) == 1


def _db_dump(path: str) -> bytes:
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump()).encode()


async def test_return_before_the_form_was_filled_saves_nothing(client, fake):
    await client.post("/wallet/setup")
    done = (await client.post("/wallet/setup/complete")).json()
    assert done["setup_state"] == "pending" and done["card"] is None


async def test_a_setup_session_of_another_user_is_refused(client, fake):
    await client.post("/wallet/setup")
    sid = next(iter(fake.setups))
    fake.complete_setup(sid)
    fake.setups[sid]["client_reference_id"] = "mallory"
    done = (await client.post("/wallet/setup/complete")).json()
    assert done["setup_state"] == "refused" and done["card"] is None


async def test_setup_webhook_saves_the_card_once(client, fake, audit):
    await client.post("/wallet/setup")
    sid = next(iter(fake.setups))
    fake.complete_setup(sid)
    # The event body is the processor's; the wallet still reads the session back itself.
    event_session = {**fake.setups[sid], "setup_intent": fake.setups[sid]["setup_intent"]}
    body, headers = _session_event(event_session)

    resp = await client.post(URL, content=body, headers=headers)
    again = await client.post(URL, content=body, headers=headers)

    assert resp.status_code == 200 and resp.json()["disposition"] == "wallet"
    assert again.json()["duplicate"] is True
    assert (await client.get("/wallet")).json()["card"]["last4"] == "4242"
    (saved,) = _events(audit, "wallet", "wallet_card_saved")
    assert saved.data["source"] == "webhook" and saved.data["session_id"] == sid
    # The browser's later return finds the card saved and audits nothing more.
    assert (await client.post("/wallet/setup/complete")).json()["setup_state"] == "saved"
    assert len(_events(audit, "wallet", "wallet_card_saved")) == 1


async def test_setup_webhook_for_an_unknown_session_is_recorded_unmatched(client, fake, audit):
    session = {
        "id": "cs_test_unknown",
        "object": "checkout.session",
        "mode": "setup",
        "status": "complete",
        "livemode": False,
    }
    body, headers = _session_event(session)
    assert (await client.post(URL, content=body, headers=headers)).status_code == 200
    (entry,) = _events(audit, "processor", "wallet_setup_unmatched")
    assert entry.data["session_id"] == "cs_test_unknown"
    assert vault.card_summary(LOCAL_PAYER) is None


async def test_removing_the_card_detaches_it_and_is_audited(client, fake, audit):
    pm = await _save_card_through_the_form(client, fake)
    view = (await client.post("/wallet/card/remove")).json()
    assert view["card"] is None
    assert fake.detached == [pm]
    assert _events(audit, "wallet", "wallet_card_removed")
    # Removing twice is harmless and audits nothing more.
    await client.post("/wallet/card/remove")
    assert len(_events(audit, "wallet", "wallet_card_removed")) == 1


async def test_wallet_setup_is_409_without_the_wallet_model(client, fake, monkeypatch):
    monkeypatch.setenv("PAYMENTS_FLOW", "checkout")
    assert (await client.post("/wallet/setup")).status_code == 409
    assert (await client.get("/wallet")).json()["enabled"] is False


# --- the mandate ---------------------------------------------------------------------------


async def test_mandate_is_integer_cents_and_audited_when_changed(client, fake, audit):
    view = (await client.post("/wallet/mandate", json={"limit_cents": 5000})).json()
    assert view["mandate_cents"] == 5000
    (changed,) = _events(audit, "wallet", "wallet_mandate_changed")
    assert changed.data == {
        "user": LOCAL_PAYER,
        "previous_cents": 20000,
        "limit_cents": 5000,
        "currency": "CHF",
    }
    await client.post("/wallet/mandate", json={"limit_cents": 5000})  # unchanged: not audited
    assert len(_events(audit, "wallet", "wallet_mandate_changed")) == 1
    for bad in (50.5, "5000", 0, 99, 2_000_000, True):
        assert (await client.post("/wallet/mandate", json={"limit_cents": bad})).status_code == 422


async def test_over_the_mandate_the_wallet_is_not_proposed_and_falls_back(client, fake, audit):
    seed_card()
    await client.post("/wallet/mandate", json={"limit_cents": TOTAL - 1})
    run = await _paused(client)
    options = run["payment_options"]
    assert options["models"] == ["checkout", "wallet"]
    assert options["default"] == "checkout" and options["wallet_unavailable"] == "over_mandate"
    assert options["mandate_cents"] == TOTAL - 1 and options["card"]["last4"] == "4242"

    # The approver insists on the wallet: the wallet refuses, the run opens the hosted page.
    view = await _approve(client, run, "wallet")

    assert view["status"] == "awaiting_payment"
    assert view["payment_model"] == "checkout" and view["payment_fallback"] == "over_mandate"
    assert view["checkout"]["amount_cents"] == TOTAL
    assert "payments.charge" not in view["trajectory"]
    assert LEDGER[run["run_id"]]["status"] == "checkout_pending"
    (fallback,) = _events(audit, run["run_id"], "wallet_fallback")
    assert fallback.data == {"reason": "over_mandate", "to": "checkout"}


def test_issue_refuses_over_the_mandate_and_without_a_card():
    args = {
        "user": LOCAL_PAYER,
        "amount_cents": 20001,
        "currency": "CHF",
        "run_id": "run-1",
        "merchant_id": vault.MERCHANT_ID,
        "approval_ref": "alice:h",
    }
    assert vault.issue_payment_token(**args) == {"issued": False, "reason": "no_saved_card"}
    seed_card()
    assert vault.issue_payment_token(**args) == {
        "issued": False,
        "reason": "over_mandate",
        "mandate_cents": 20000,
    }
    assert vault.issue_payment_token(**{**args, "amount_cents": 20000})["issued"] is True
    seed_card(exp_month=1, exp_year=2020, payment_method_id="pm_test_old")
    assert vault.issue_payment_token(**args | {"amount_cents": 100})["reason"] == "card_expired"
    with pytest.raises(vault.WalletRefused):
        vault.issue_payment_token(**args | {"merchant_id": "other-shop"})
    with pytest.raises(vault.WalletRefused):
        vault.issue_payment_token(**args | {"amount_cents": 100.0})


# --- the scoped token ----------------------------------------------------------------------


def _issue(**overrides) -> dict:
    seed_card()
    args = {
        "user": LOCAL_PAYER,
        "amount_cents": TOTAL,
        "currency": "CHF",
        "run_id": "run-1",
        "merchant_id": vault.MERCHANT_ID,
        "approval_ref": "alice:h",
    } | overrides
    return vault.issue_payment_token(**args)


def _redeem(token: str, **overrides) -> dict:
    args = {
        "payment_token": token,
        "amount_cents": TOTAL,
        "currency": "CHF",
        "merchant_id": vault.MERCHANT_ID,
        "run_id": "run-1",
    } | overrides
    return vault.redeem_payment_token(**args)


def test_token_is_opaque_bound_and_stored_as_a_hash_only():
    issued = _issue()
    token = issued["payment_token"]
    assert token.startswith("wpt_") and len(token) > 40
    assert issued["amount_cents"] == TOTAL and issued["run_id"] == "run-1"
    assert issued["merchant_id"] == vault.MERCHANT_ID and issued["currency"] == "CHF"
    expires = issued["expires_at"]
    left = time.mktime(time.strptime(expires[:19], "%Y-%m-%dT%H:%M:%S")) - time.mktime(
        time.gmtime()
    )
    assert vault.TOKEN_TTL_S - 60 <= left <= vault.TOKEN_TTL_S + 60
    assert "card" not in issued and "payment_method_id" not in issued and "last4" not in issued
    assert token.encode() not in _db_dump(vault.wallet_db_path())


def test_token_is_single_use():
    issued = _issue()
    token = issued["payment_token"]
    assert _redeem(token) == {
        "token_id": issued["token_id"],
        "customer_id": CUSTOMER,
        "payment_method_id": PM_VISA,
    }
    with pytest.raises(vault.WalletRefused, match="already used"):
        _redeem(token)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"amount_cents": TOTAL + 1}, "amount"),
        ({"amount_cents": TOTAL - 1}, "amount"),
        ({"currency": "EUR"}, "amount or currency"),
        ({"merchant_id": "other-shop"}, "merchant"),
        ({"run_id": "run-2"}, "another run"),
    ],
)
def test_token_must_match_amount_currency_merchant_and_run(overrides, error):
    token = _issue()["payment_token"]
    with pytest.raises(vault.WalletRefused, match=error):
        _redeem(token, **overrides)
    assert _redeem(token)["payment_method_id"] == PM_VISA  # a refusal spends nothing


def test_expired_or_unknown_token_is_refused():
    token = _issue()["payment_token"]
    with pytest.raises(vault.WalletRefused, match="expired"):
        _redeem(token, now=time.time() + vault.TOKEN_TTL_S + 1)
    with pytest.raises(vault.WalletRefused, match="unknown"):
        _redeem("wpt_" + "x" * 43)


def test_token_is_refused_once_the_card_is_removed():
    token = _issue()["payment_token"]
    vault.store().delete_card(LOCAL_PAYER)
    with pytest.raises(vault.WalletRefused, match="no longer"):
        _redeem(token)


def test_racing_redemptions_have_one_winner():
    token = _issue()["payment_token"]
    barrier = threading.Barrier(8)
    wins: list[bool] = []

    def attempt() -> None:
        barrier.wait()
        try:
            _redeem(token)
            wins.append(True)
        except vault.WalletRefused:
            wins.append(False)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


async def test_wallet_server_checks_who_calls():
    seed_card()
    issue = {
        "wallet_user": LOCAL_PAYER,
        "amount_cents": TOTAL,
        "currency": "CHF",
        "run_id": "run-1",
        "merchant_id": vault.MERCHANT_ID,
        "approval_ref": "alice:h",
    }
    async with Client(wallet_server.mcp) as c:
        for caller in ("pricing", "payments"):
            with pytest.raises(ToolError, match="unauthorized"):
                await c.call_tool(
                    "issue_payment_token", {**issue, "agent_token": issue_token(caller, "wallet")}
                )
        with pytest.raises(ToolError, match="unauthorized"):  # scoped to another server
            await c.call_tool(
                "issue_payment_token", {**issue, "agent_token": issue_token("booking", "payments")}
            )
        token = (
            await c.call_tool(
                "issue_payment_token", {**issue, "agent_token": issue_token("booking", "wallet")}
            )
        ).data["payment_token"]
        redeem = {
            "payment_token": token,
            "amount_cents": TOTAL,
            "currency": "CHF",
            "merchant_id": vault.MERCHANT_ID,
            "run_id": "run-1",
        }
        with pytest.raises(ToolError, match="unauthorized"):  # an agent may never redeem
            await c.call_tool(
                "redeem_payment_token", {**redeem, "agent_token": issue_token("booking", "wallet")}
            )
        spent = await c.call_tool(
            "redeem_payment_token", {**redeem, "agent_token": issue_token("payments", "wallet")}
        )
        assert spent.data["payment_method_id"] == PM_VISA
        with pytest.raises(ToolError, match="already used"):
            await c.call_tool(
                "redeem_payment_token",
                {**redeem, "agent_token": issue_token("payments", "wallet")},
            )


# --- policy and the gate -------------------------------------------------------------------


def test_policy_wallet_token_is_booking_only_and_approval_gated():
    policy = ToolPolicy.from_yaml()
    for agent in ("planner", "pricing", "supervisor"):
        with pytest.raises(PolicyViolation):
            policy.check(agent, "wallet.issue_payment_token", approved=True)
    with pytest.raises(PolicyViolation, match="approval"):
        policy.check("booking", "wallet.issue_payment_token", approved=False)
    policy.check("booking", "wallet.issue_payment_token", approved=True)
    for agent in ("planner", "pricing", "booking", "supervisor"):
        with pytest.raises(PolicyViolation):
            policy.check(agent, "wallet.redeem_payment_token", approved=True)


@pytest.mark.parametrize(
    "case", ["no_token", "checkout_chosen", "other_payer", "other_run", "other_amount"]
)
async def test_gate_issues_a_wallet_token_only_for_the_humans_wallet_choice(tmp_path, case):
    seed_card()
    audit = AuditLog(tmp_path / "a.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    claims = {
        "run_id": "run-1",
        "approver": "alice",
        "quote_hash": "h" * 64,
        "amount_cents": TOTAL,
        "currency": "CHF",
        "payment_model": "wallet",
        "payer": LOCAL_PAYER,
    }
    args = {
        "wallet_user": LOCAL_PAYER,
        "amount_cents": TOTAL,
        "currency": "CHF",
        "run_id": "run-1",
        "merchant_id": vault.MERCHANT_ID,
        "approval_ref": f"alice:{'h' * 64}",
    }
    if case == "checkout_chosen":
        claims["payment_model"] = "checkout"
    elif case == "other_payer":
        args["wallet_user"] = "bob"
    elif case == "other_run":
        args["run_id"] = "run-2"
    elif case == "other_amount":
        args["amount_cents"] = TOTAL + 100
    token = None if case == "no_token" else issue_approval_token(**claims)
    with pytest.raises(PolicyViolation, match="approval"):
        await tools.call(
            "run-1", "booking", "wallet.issue_payment_token", args, approval_token=token
        )
    assert vault.store().tokens() == []
    (denied,) = [e for e in audit.read("run-1") if e.event == "policy_denied"]
    assert denied.data["tool"] == "wallet.issue_payment_token"


# --- a run paid by the agent from the wallet -------------------------------------------------


async def test_wallet_is_proposed_and_pays_off_session_once(client, fake, audit, llm):
    await _save_card_through_the_form(client, fake)
    run = await _paused(client)
    run_id = run["run_id"]
    options = run["payment_options"]
    assert options["default"] == "wallet" and options["wallet_unavailable"] is None
    assert options["payer"] == LOCAL_PAYER and options["mandate_cents"] == 20000

    done = await _approve(client, run)  # no model named: the proposed one (the wallet)

    assert done["status"] == "completed", done
    assert done["payment_model"] == "wallet" and done["payment_fallback"] is None
    assert done["wallet_payment"] == {"brand": "visa", "last4": "4242", "mandate_cents": 20000}
    assert "wallet.issue_payment_token" in done["trajectory"]
    assert "payments.create_checkout" not in done["trajectory"]
    (charge,) = fake.posts("/v1/payment_intents")
    stored = vault.store().card(LOCAL_PAYER)
    assert charge["customer"] == stored["customer_id"]
    assert charge["payment_method"] == stored["payment_method_id"]
    assert charge["off_session"] == "true" and charge["confirm"] == "true"
    # The processor's current API refuses payment_method_types; an off-session charge must never
    # redirect, so automatic methods are enabled with redirects forbidden.
    assert "payment_method_types[0]" not in charge
    assert charge["automatic_payment_methods[enabled]"] == "true"
    assert charge["automatic_payment_methods[allow_redirects]"] == "never"
    assert charge["amount"] == str(TOTAL) and charge["currency"] == "chf"
    assert charge["expand[]"] == "latest_charge"
    assert charge["_headers"]["Idempotency-Key"] == f"regulated-agent-lab-{run_id}"
    quote_hash = done["quote"]["quote_hash"]
    assert charge["metadata[run_id]"] == run_id
    assert charge["metadata[quote_hash]"] == quote_hash
    assert charge["metadata[approval_ref]"] == f"alice:{quote_hash}"
    assert done["payment"]["receipt_url"].startswith("https://pay.stripe.com/")
    assert LEDGER[run_id]["status"] == "captured" and len(LEDGER) == 1
    (issued,) = _events(audit, run_id, "wallet_token_issued")
    (redeemed,) = _events(audit, run_id, "wallet_token_redeemed")
    assert issued.actor == redeemed.actor == "wallet"
    assert issued.data["amount_cents"] == TOTAL and issued.data["merchant_id"] == vault.MERCHANT_ID
    assert redeemed.data["payment_id"] == done["payment"]["payment_id"]
    (decision,) = _events(audit, run_id, "approval_decision")
    assert decision.data["payment_model"] == "wallet" and decision.data["payer"] == LOCAL_PAYER
    # A replayed approval charges nothing more.
    again = await client.post(f"/runs/{run_id}/approval", json=_decision(run, "wallet"))
    assert again.status_code == 409
    assert len(fake.posts("/v1/payment_intents")) == 1

    # No card data, and no payment token, in the model's prompts or in the audit file.
    raw = audit.path.read_text()
    for needle in (stored["payment_method_id"], stored["customer_id"], "wpt_", "last4", "brand"):
        assert needle not in raw, needle
        assert all(needle not in p for p in llm.prompts), needle
    assert "[REDACTED:payment_token]" in raw
    for secret in CARD_SECRETS:
        assert secret not in raw
    assert audit.verify().ok


async def test_sca_required_falls_back_to_checkout_and_completes_once(client, fake, audit):
    pm = await _save_card_through_the_form(client, fake, last4="3184")
    fake.sca_methods.add(pm)
    run = await _paused(client)

    view = await _approve(client, run, "wallet")

    run_id = run["run_id"]
    assert view["status"] == "awaiting_payment", view
    assert view["payment_model"] == "checkout"
    assert view["payment_fallback"] == "authentication_required"
    assert view["checkout"]["amount_cents"] == TOTAL and view["checkout"]["currency"] == "CHF"
    assert view["payment"] is None and LEDGER[run_id]["status"] == "checkout_pending"
    assert view["trajectory"].index("payments.charge") < view["trajectory"].index(
        "payments.create_checkout"
    )
    (fallback,) = _events(audit, run_id, "wallet_fallback")
    assert fallback.data["reason"] == "authentication_required"
    assert _events(audit, run_id, "wallet_token_redeemed") == []

    # The customer authenticates on the processor's page: the run completes, one capture.
    fake.pay(view["checkout"]["session_id"])
    done = (await client.post(f"/runs/{run_id}/payment/refresh")).json()
    assert done["status"] == "completed"
    assert done["payment_model"] == "checkout" and done["wallet_payment"] is None
    assert len(LEDGER) == 1 and LEDGER[run_id]["status"] == "captured"


async def test_no_saved_card_falls_back_to_checkout(client, fake, audit):
    run = await _paused(client)
    assert run["payment_options"]["default"] == "checkout"
    assert run["payment_options"]["wallet_unavailable"] == "no_saved_card"

    view = await _approve(client, run, "wallet")

    assert view["status"] == "awaiting_payment"
    assert view["payment_fallback"] == "no_saved_card"
    assert "payments.charge" not in view["trajectory"]
    assert not fake.posts("/v1/payment_intents")


async def test_explicit_checkout_choice_never_touches_the_wallet(client, fake, audit):
    seed_card()
    run = await _paused(client)
    assert run["payment_options"]["default"] == "wallet"

    view = await _approve(client, run, "checkout")

    assert view["status"] == "awaiting_payment"
    assert view["payment_model"] == "checkout" and view["payment_fallback"] is None
    assert "wallet.issue_payment_token" not in view["trajectory"]
    assert vault.store().tokens() == []
    (decision,) = _events(audit, run["run_id"], "approval_decision")
    assert decision.data["payment_model"] == "checkout"


@pytest.mark.parametrize("model", ["checkout", "wallet"])
async def test_both_models_end_completed_with_one_capture(client, fake, model):
    seed_card()
    run = await _paused(client)
    view = await _approve(client, run, model)
    if model == "checkout":
        fake.pay(view["checkout"]["session_id"])
        view = (await client.post(f"/runs/{run['run_id']}/payment/refresh")).json()
    assert view["status"] == "completed"
    assert view["payment_model"] == model
    assert view["payment"]["amount_cents"] == TOTAL
    assert len(LEDGER) == 1 and LEDGER[run["run_id"]]["status"] == "captured"


async def test_a_model_outside_payments_flow_is_rejected(client, fake, monkeypatch):
    seed_card()
    monkeypatch.setenv("PAYMENTS_FLOW", "checkout")
    run = await _paused(client)
    assert run["payment_options"]["models"] == ["checkout"]
    view = await _approve(client, run, "wallet")
    assert view["status"] == "rejected"
    assert "quote_mismatch" not in view["flags"]
    assert LEDGER == {} and vault.store().tokens() == []


async def test_choice_is_bound_into_the_approval_token(fake, tmp_path):
    seed_card()
    audit = AuditLog(tmp_path / "a.jsonl")
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    graph = build_graph(MockLLM(FIXTURES), tools)
    cfg = {"configurable": {"thread_id": "run-token"}}
    state = await graph.ainvoke(initial_state("run-token", MESSAGE, "alice"), cfg)
    decision = {
        "approved": True,
        "approver": "alice",
        "quote_hash": state["quote"].quote_hash,
        "payment_model": "checkout",
    }
    state = await graph.ainvoke(Command(resume=decision), cfg)
    claims = verify_approval_token(
        state["approval"]["token"], run_id="run-token", amount_cents=TOTAL, currency="CHF"
    )
    assert claims["payment_model"] == "checkout" and claims["payer"] == "alice"
    assert state["approval"]["payment_model"] == "checkout"
    assert state["payment_model"] == "checkout"


def test_merchant_ids_agree_across_the_three_institutions():
    assert booking.MERCHANT_ID == payments.MERCHANT_ID == vault.MERCHANT_ID


def test_payments_flow_values(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    for flow, models in [
        ("", ("checkout", "wallet")),
        ("both", ("checkout", "wallet")),
        ("checkout", ("checkout",)),
        ("wallet", ("wallet",)),
        ("agent", ("wallet",)),
    ]:
        monkeypatch.setenv("PAYMENTS_FLOW", flow)
        assert booking.payment_models() == models
    monkeypatch.delenv("PAYMENTS_PROVIDER")
    assert booking.payment_models() == ("agent",)


# --- the payments institution reaches the wallet over its own stdio subprocess ----------------


async def test_stdio_payments_server_redeems_at_the_wallet(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    env = {
        **__import__("os").environ,
        "FASTMCP_SHOW_CLI_BANNER": "false",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }
    client = Client(
        StdioTransport(
            command=sys.executable,
            args=["-m", "regulated_agent_lab.services.payments.server"],
            env=env,
            keep_alive=False,
        )
    )
    args = {
        "agent_token": issue_token("booking", "payments"),
        "hold_id": "H-1",
        "amount_cents": TOTAL,
        "currency": "CHF",
        "idempotency_key": f"run-stdio-{uuid.uuid4().hex[:6]}",
        "approval_ref": "alice:h",
        "payment_token": "wpt_" + "z" * 43,
    }
    async with client:
        with pytest.raises(ToolError, match="payment token refused: unknown payment token"):
            await asyncio.wait_for(client.call_tool("charge", args), timeout=30)


def test_event_session_keeps_the_mode_and_no_customer_details():
    session = {
        "id": "cs_test_1",
        "mode": "setup",
        "customer_details": {"email": "alice@example.com"},
        "metadata": {"run_id": "r"},
    }
    body = json.dumps(
        {
            "id": "evt_1",
            "type": events.CHECKOUT_COMPLETED,
            "created": 1,
            "livemode": False,
            "data": {"object": session},
        }
    ).encode()
    parsed = events.parse_event(body).checkout_session
    assert parsed["mode"] == "setup" and "customer_details" not in parsed


# --- with authentication on: one wallet per signed-in user -------------------------------------


async def test_each_signed_in_user_has_their_own_wallet_and_posts_need_an_origin(
    fake, audit, tmp_path
):
    from tests.integration.test_auth import ORIGIN, _login, serve_auth

    async with serve_auth(audit, tmp_path) as c:
        assert (await c.get("/wallet")).status_code == 401
        await _login(c, "alice")
        assert (await c.post("/wallet/mandate", json={"limit_cents": 9000})).status_code == 403
        assert (await c.post("/wallet/mandate", json={"limit_cents": 9000}, headers=ORIGIN)).json()[
            "user"
        ] == "alice"
        setup = await c.post("/wallet/setup", headers=ORIGIN)
        assert setup.status_code == 200
        sid = list(fake.setups)[-1]
        assert fake.setups[sid]["client_reference_id"] == "alice"
        fake.complete_setup(sid)
        assert (await c.post("/wallet/setup/complete", headers=ORIGIN)).json()["card"]["last4"] == (
            "4242"
        )
        run = (await c.post("/runs", json={"message": MESSAGE}, headers=ORIGIN)).json()
        assert run["payment_options"]["payer"] == "alice"
        assert run["payment_options"]["mandate_cents"] == 9000

        await c.post("/auth/logout", headers=ORIGIN)
        await _login(c, "bob")
        bob = (await c.get("/wallet")).json()
        assert bob["user"] == "bob" and bob["card"] is None and bob["mandate_cents"] == 20000
        # bob approves alice's run: alice's wallet pays (she is the customer), bound in the audit.
        done = (
            await c.post(
                f"/runs/{run['run_id']}/approval",
                json={
                    "approved": True,
                    "approver": "x",
                    "quote_hash": run["interrupt"]["quote_hash"],
                },
                headers=ORIGIN,
            )
        ).json()
        assert done["status"] == "completed" and done["payment_model"] == "wallet"
        (decision,) = _events(audit, run["run_id"], "approval_decision")
        assert decision.data["approver"] == "bob" and decision.data["payer"] == "alice"


async def test_processor_rejecting_the_wallet_charge_is_a_failure_not_a_refusal(
    client, fake, audit
):
    # Seen live: the processor refused the request (an outdated parameter). That is a technical
    # failure; "refused" is kept for the gate and the wallet saying no.
    await _save_card_through_the_form(client, fake)
    fake.reject_intents = True
    run = await _paused(client)

    done = await _approve(client, run)

    assert done["status"] == "failed", done
    assert LEDGER == {}
    (failed,) = _events(audit, run["run_id"], "payment_failed")
    assert failed.data == {"reason": "payment processor unavailable"}
