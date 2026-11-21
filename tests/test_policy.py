import time

import pytest

from controls.identity import issue_token, verify_token
from controls.policy import PolicyViolation, ToolPolicy


@pytest.fixture
def policy() -> ToolPolicy:
    return ToolPolicy.from_yaml()


def test_pricing_cannot_call_payment(policy: ToolPolicy) -> None:
    with pytest.raises(PolicyViolation) as exc:
        policy.check("pricing", "payments.charge", approved=True)
    assert exc.value.agent == "pricing"
    assert exc.value.tool == "payments.charge"
    assert exc.value.reason


def test_unknown_agent_denied(policy: ToolPolicy) -> None:
    with pytest.raises(PolicyViolation):
        policy.check("intruder", "travel_search.search_trains")


def test_unlisted_tool_denied(policy: ToolPolicy) -> None:
    policy.check("planner", "travel_search.search_trains")
    with pytest.raises(PolicyViolation):
        policy.check("planner", "travel_search.hold_seat")


def test_charge_requires_approval(policy: ToolPolicy) -> None:
    with pytest.raises(PolicyViolation) as exc:
        policy.check("booking", "payments.charge", approved=False)
    assert "approval" in exc.value.reason
    policy.check("booking", "payments.charge", approved=True)


def test_token_roundtrip() -> None:
    token = issue_token("booking", "payments")
    assert verify_token(token, "payments") == "booking"


def test_token_wrong_server_rejected() -> None:
    token = issue_token("booking", "payments")
    with pytest.raises(ValueError):
        verify_token(token, "travel_search")


def test_token_expired_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = issue_token("booking", "payments", ttl_s=10)
    real_time = time.time()
    monkeypatch.setattr("controls.identity.time.time", lambda: real_time + 11)
    with pytest.raises(ValueError):
        verify_token(token, "payments")
