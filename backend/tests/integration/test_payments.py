import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from regulated_agent_lab.controls.identity import issue_token
from regulated_agent_lab.services.payments.server import LEDGER, mcp, reset_ledger


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_ledger()
    yield
    reset_ledger()


def _args(**overrides):
    args = {
        "agent_token": issue_token("booking", "payments"),
        "hold_id": "H-1",
        "amount_cents": 11200,
        "currency": "CHF",
        "idempotency_key": "run-1",
        "approval_ref": "approval-1",
    }
    args.update(overrides)
    return args


async def test_charge_with_booking_token_captures():
    async with Client(mcp) as client:
        result = await client.call_tool("charge", _args())
    payment = result.data
    assert payment["status"] == "captured"
    assert payment["amount_cents"] == 11200
    assert isinstance(payment["amount_cents"], int)
    assert payment["currency"] == "CHF"
    assert payment["payment_id"]
    assert len(LEDGER) == 1


async def test_pricing_token_rejected():
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="unauthorized"):
            await client.call_tool("charge", _args(agent_token=issue_token("pricing", "payments")))
        # A booking token scoped to another server is also refused.
        with pytest.raises(ToolError, match="unauthorized"):
            await client.call_tool(
                "charge", _args(agent_token=issue_token("booking", "travel_search"))
            )
    assert LEDGER == {}


async def test_missing_approval_rejected():
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="approval required"):
            await client.call_tool("charge", _args(approval_ref=""))
    assert LEDGER == {}


async def test_idempotent_double_charge_single_capture():
    async with Client(mcp) as client:
        first = (await client.call_tool("charge", _args())).data
        second = (await client.call_tool("charge", _args())).data
    assert first["payment_id"] == second["payment_id"]
    assert len(LEDGER) == 1
