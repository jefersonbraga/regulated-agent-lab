import pytest

from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.controls.audit_log import AuditLog
from regulated_agent_lab.controls.injection_guard import REMOVED_MARKER
from regulated_agent_lab.controls.policy import PolicyViolation, ToolPolicy
from regulated_agent_lab.services.payments.server import LEDGER, reset_ledger

SEARCH_ARGS = {"origin": "Zurich HB", "destination": "Bern", "date": "2026-12-01"}


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_ledger()
    yield
    reset_ledger()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def gated(audit):
    return GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)


def _events(audit: AuditLog, run_id: str) -> list[str]:
    return [e.event for e in audit.read(run_id)]


async def test_allowed_call_audited_in_order(gated, audit):
    result = await gated.call("run-1", "planner", "travel_search.search_trains", SEARCH_ARGS)

    assert result.flagged is False
    assert len(result.sanitized) == 16
    assert result.sanitized[0]["operator"] == "Demo Rail"
    assert _events(audit, "run-1") == ["tool_call", "tool_result"]
    call_entry = audit.read("run-1")[0]
    assert call_entry.actor == "planner"
    assert call_entry.data["tool"] == "travel_search.search_trains"
    assert audit.verify().ok


async def test_denied_call_audited_and_raises(gated, audit):
    with pytest.raises(PolicyViolation):
        await gated.call(
            "run-2",
            "pricing",
            "payments.charge",
            _charge_args("run-2"),
            # Even a valid human approval does not let the wrong agent charge.
            approval_token=_approval_token("run-2"),
        )

    assert _events(audit, "run-2") == ["policy_denied"]
    assert LEDGER == {}
    assert audit.verify().ok


async def test_poisoned_result_flagged_and_sanitized(gated, audit, monkeypatch):
    monkeypatch.setenv("TRAVEL_SEARCH_POISONED", "1")

    result = await gated.call("run-3", "planner", "travel_search.search_trains", SEARCH_ARGS)

    assert result.flagged is True
    assert result.findings
    first = result.sanitized[0]
    assert first["notes"] == REMOVED_MARKER
    assert first["train_id"]  # structure kept
    assert _events(audit, "run-3") == ["tool_call", "tool_result", "injection_flagged"]
    flagged_entry = audit.read("run-3")[2]
    assert flagged_entry.data["findings"] == result.findings
    raw = audit.path.read_text(encoding="utf-8")
    assert "Ignore previous instructions" not in raw


async def test_payment_receives_injected_token(gated, audit):
    args = _charge_args("run-4")

    result = await gated.call(
        "run-4", "booking", "payments.charge", args, approval_token=_approval_token("run-4")
    )

    assert "agent_token" not in args  # caller's args are not mutated
    assert result.sanitized["status"] == "captured"
    assert result.sanitized["amount_cents"] == 11200
    assert len(LEDGER) == 1
    assert _events(audit, "run-4") == ["tool_call", "tool_result"]
    # The token is injected by the gate and never written to the audit log.
    assert "agent_token" not in audit.path.read_text(encoding="utf-8")


async def test_token_issue_failure_is_audited_as_tool_error(gated, audit, monkeypatch):
    import regulated_agent_lab.agents.tools

    def no_secret(agent, server):
        raise RuntimeError("AGENT_TOKEN_SECRET is not set")

    monkeypatch.setattr(regulated_agent_lab.agents.tools, "issue_token", no_secret)
    args = _charge_args("run-5")

    with pytest.raises(RuntimeError):
        await gated.call(
            "run-5", "booking", "payments.charge", args, approval_token=_approval_token("run-5")
        )

    # Every audited tool_call has an outcome, even when the token cannot be issued.
    assert _events(audit, "run-5") == ["tool_call", "tool_error"]
    assert LEDGER == {}
    assert audit.verify().ok


def _charge_args(run_id: str, amount_cents: int = 11200) -> dict:
    return {
        "hold_id": "H-1",
        "amount_cents": amount_cents,
        "currency": "CHF",
        "idempotency_key": run_id,
        "approval_ref": "alice:" + "a" * 64,
    }


def _approval_token(run_id: str, amount_cents: int = 11200) -> str:
    from regulated_agent_lab.controls.identity import issue_approval_token

    return issue_approval_token(
        run_id=run_id,
        approver="alice",
        quote_hash="a" * 64,
        amount_cents=amount_cents,
        currency="CHF",
    )


@pytest.mark.parametrize(
    ("token", "approval_ref"),
    [
        (None, None),
        ("forged.token", None),
        ("other-run", None),
        ("other-amount", None),
        # A valid proof, but the approval_ref sent to the server names another approver or
        # another quote hash than the one the human approved.
        ("valid", "mallory:" + "a" * 64),
        ("valid", "alice:" + "b" * 64),
    ],
    ids=[
        "no-proof",
        "forged",
        "token-for-other-run",
        "token-for-other-amount",
        "approval-ref-other-approver",
        "approval-ref-other-quote-hash",
    ],
)
async def test_charge_without_matching_approval_proof_denied(gated, audit, token, approval_ref):
    run_id = "run-proof"
    if token == "other-run":
        token = _approval_token("run-elsewhere")
    elif token == "other-amount":
        token = _approval_token(run_id, amount_cents=100)
    elif token == "valid":
        token = _approval_token(run_id)
    args = _charge_args(run_id)
    if approval_ref is not None:
        args["approval_ref"] = approval_ref

    with pytest.raises(PolicyViolation):
        await gated.call(run_id, "booking", "payments.charge", args, approval_token=token)

    assert _events(audit, run_id) == ["policy_denied"]
    denied = audit.read(run_id)[0].data
    assert denied["approved"] is False
    if approval_ref is not None:
        assert denied["approval_error"] == "approval_ref does not match the approval proof"
    assert LEDGER == {}


async def test_charge_with_matching_approval_proof_allowed(gated, audit):
    run_id = "run-proof-ok"
    result = await gated.call(
        run_id,
        "booking",
        "payments.charge",
        _charge_args(run_id),
        approval_token=_approval_token(run_id),
    )
    assert result.sanitized["status"] == "captured"
    # The proof itself is never written to the audit log.
    assert _approval_token(run_id).split(".")[1] not in audit.path.read_text(encoding="utf-8")
