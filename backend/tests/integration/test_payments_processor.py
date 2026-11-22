"""Test-mode card processor: fails closed on non-test keys, never touches the network here."""

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import regulated_agent_lab.services.payments.server as payments
from regulated_agent_lab.controls.identity import issue_token
from regulated_agent_lab.services.payments import processor
from regulated_agent_lab.services.payments.server import LEDGER, mcp, reset_ledger
from regulated_agent_lab.services.wallet import vault
from tests.wallet_fixtures import CUSTOMER, PM_VISA, seed_card

TEST_KEY = "sk_test_" + "x" * 24
CARD = {"customer": CUSTOMER, "payment_method": PM_VISA}


def _processor(status: int = 200, body: dict | None = None, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        default = {"id": "pi_test_123", "status": "succeeded", "livemode": False}
        return httpx.Response(status, json=body if body is not None else default)

    return httpx.MockTransport(handler)


def _never():
    def handler(request):
        raise AssertionError("no request may be sent")

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_ledger()
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", None)
    yield
    reset_ledger()


@pytest.mark.parametrize(
    "key", [None, "", "sk_live_" + "x" * 24, "rk_live_" + "x" * 24, "pk_test_abc", "test_sk_test_x"]
)
def test_only_test_mode_secret_keys_are_accepted(monkeypatch, key):
    if key is None:
        monkeypatch.delenv(processor.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(processor.KEY_ENV, key)
    with pytest.raises(processor.ProcessorNotConfigured) as info:
        processor.capture(11200, "CHF", "run-1", {}, **CARD, transport=_never())
    assert "sk_live" not in str(info.value)


@pytest.mark.parametrize("key", [TEST_KEY, "rk_test_" + "y" * 24])
def test_test_mode_secret_and_restricted_keys_are_accepted(monkeypatch, key):
    monkeypatch.setenv(processor.KEY_ENV, key)
    assert (
        processor.capture(11200, "CHF", "run-1", {}, **CARD, transport=_processor())["status"]
        == "captured"
    )


def test_capture_is_an_off_session_charge_of_the_saved_card(monkeypatch):
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    seen: list[httpx.Request] = []
    result = processor.capture(
        11200, "CHF", "run-1", {"hold_id": "H-1"}, **CARD, transport=_processor(seen=seen)
    )
    assert result == {"payment_id": "pi_test_123", "status": "captured"}
    (request,) = seen
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form["amount"] == "11200" and form["currency"] == "chf"
    assert form["customer"] == CUSTOMER and form["payment_method"] == PM_VISA
    assert form["off_session"] == "true" and form["confirm"] == "true"
    assert form["expand[]"] == "latest_charge"
    assert form["metadata[hold_id]"] == "H-1"
    assert request.headers["Idempotency-Key"] == "regulated-agent-lab-run-1"
    # No fixed test card any more: the card is the one its holder saved in the wallet.
    assert not hasattr(processor, "TEST_CARD")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (402, {"error": {}}, processor.PaymentDeclined),
        (
            402,
            {"error": {"code": "authentication_required", "type": "card_error"}},
            processor.AuthenticationRequired,
        ),
        (
            200,
            {"id": "pi_1", "status": "requires_action", "livemode": False},
            processor.AuthenticationRequired,
        ),
        (500, {}, processor.ProcessorUnavailable),
        (200, {"unexpected": True}, processor.ProcessorUnavailable),
        (
            200,
            {"id": "pi_1", "status": "succeeded", "livemode": True},
            processor.ProcessorNotConfigured,
        ),
    ],
)
def test_processor_answers_map_to_errors(monkeypatch, status, body, error):
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    with pytest.raises(error):
        processor.capture(11200, "CHF", "run-1", {}, **CARD, transport=_processor(status, body))


def _args(**overrides):
    args = {
        "agent_token": issue_token("booking", "payments"),
        "hold_id": "H-1",
        "amount_cents": 11200,
        "currency": "CHF",
        "idempotency_key": "run-1",
        "approval_ref": "approval-1",
    }
    return args | overrides


def _wallet_token(run_id: str = "run-1", amount: int = 11200) -> str:
    seed_card()
    issued = vault.issue_payment_token(
        user="local-dev",
        amount_cents=amount,
        currency="CHF",
        run_id=run_id,
        merchant_id=vault.MERCHANT_ID,
        approval_ref="approval-1",
    )
    return issued["payment_token"]


async def test_charge_in_test_mode_records_the_processor_payment(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    seen: list[httpx.Request] = []
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", _processor(seen=seen))
    token = _wallet_token()
    async with Client(mcp) as client:
        first = (await client.call_tool("charge", _args(payment_token=token))).data
        # A resumed approval is answered from the ledger, even with the spent token.
        again = (await client.call_tool("charge", _args(payment_token=token))).data
    assert first["payment_id"] == again["payment_id"] == "pi_test_123"
    assert len(seen) == 1
    assert LEDGER["run-1"]["provider"] == "stripe_test"


async def test_charge_in_test_mode_without_a_wallet_token_captures_nothing(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", _never())
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="payment token required"):
            await client.call_tool("charge", _args())
    assert LEDGER == {}


async def test_charge_in_test_mode_without_a_test_key_captures_nothing(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.setenv(processor.KEY_ENV, "sk_live_" + "x" * 24)
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", _never())
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="payments provider not configured"):
            await client.call_tool("charge", _args(payment_token=_wallet_token()))
    assert LEDGER == {}


async def test_declined_test_payment_captures_nothing(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "stripe_test")
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", _processor(402, {"error": {}}))
    async with Client(mcp) as client:
        result = (await client.call_tool("charge", _args(payment_token=_wallet_token()))).data
    assert result == {"status": "declined", "amount_cents": 11200, "currency": "CHF"}
    assert LEDGER == {}


async def test_unknown_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PROVIDER", "live")
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="payments provider not configured"):
            await client.call_tool("charge", _args())
    assert LEDGER == {}
