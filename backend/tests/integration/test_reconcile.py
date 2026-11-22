"""Reconciliation of the ledger against the processor's record (MockTransport, no network)."""

import json
import time

import httpx
import pytest

import regulated_agent_lab.services.payments.server as payments
from regulated_agent_lab.controls.audit_log import AuditLog
from regulated_agent_lab.services.payments import events, processor, reconcile
from regulated_agent_lab.services.payments.server import ledger, reset_ledger
from tests.integration.test_processor_events import (
    TEST_KEY,
    FakeProcessor,
    _event,
    _paid_run,
    _signed,
    app_client,
    install_fake_processor,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_ledger()
    events.reset_events()
    yield
    reset_ledger()
    events.reset_events()


def _capture(run_id: str, payment_id: str, amount: int = 5600, provider: str = "stripe_test"):
    ledger().insert_or_get(
        {
            "idempotency_key": run_id,
            "payment_id": payment_id,
            "provider": provider,
            "status": "captured",
            "amount_cents": amount,
            "currency": "CHF",
            "hold_id": f"H-{run_id}",
            "approval_ref": f"approval-{run_id}",
        }
    )


def _intent(pid: str, amount=5600, status="succeeded", refunded=0, disputed=False, currency="chf"):
    return {
        "id": pid,
        "object": "payment_intent",
        "amount": amount,
        "currency": currency,
        "status": status,
        "livemode": False,
        "latest_charge": {"id": f"ch_{pid}", "amount_refunded": refunded, "disputed": disputed},
    }


def _seen(pid: str, *types: str) -> None:
    for i, etype in enumerate(types):
        events.event_store().insert_or_get(
            {
                "event_id": f"evt_{pid}_{i}",
                "type": etype,
                "payment_id": pid,
                "amount_cents": 5600,
                "currency": "CHF",
                "livemode": False,
                "created": int(time.time()) + i,
                "disposition": "matched",
                "idempotency_key": None,
            }
        )


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
async def client(audit, tmp_path):
    async with app_client(audit, tmp_path) as c:
        yield c


@pytest.fixture
def fake_processor(monkeypatch):
    return install_fake_processor(monkeypatch)


@pytest.fixture
def proc(monkeypatch):
    fake = FakeProcessor()
    monkeypatch.setenv(processor.KEY_ENV, TEST_KEY)
    return fake


def _by_run(report: reconcile.ReconciliationReport) -> dict[str, reconcile.ReconciledPayment]:
    return {p.run_id: p for p in report.payments}


def test_every_discrepancy_kind_is_reported(proc):
    cases = {
        "ok": (_intent("pi_ok"), ["payment_intent.succeeded"], []),
        "missing": (None, ["payment_intent.succeeded"], ["missing_at_processor"]),
        "amount": (
            _intent("pi_amount", amount=9900),
            ["payment_intent.succeeded"],
            ["amount_mismatch"],
        ),
        "currency": (
            _intent("pi_currency", currency="eur"),
            ["payment_intent.succeeded"],
            ["amount_mismatch"],
        ),
        "refunded": (
            _intent("pi_refunded", refunded=5600),
            ["payment_intent.succeeded", "charge.refunded"],
            ["refunded"],
        ),
        "disputed": (
            _intent("pi_disputed", disputed=True),
            ["payment_intent.succeeded", "charge.dispute.created"],
            ["disputed"],
        ),
        "not_succeeded": (
            _intent("pi_not_succeeded", status="requires_payment_method"),
            ["payment_intent.succeeded"],
            ["not_succeeded", "settlement_mismatch"],
        ),
        "unconfirmed": (_intent("pi_unconfirmed"), [], ["unconfirmed"]),
        # A refund only the events claim (a forged or synthetic event): the processor disagrees.
        "event_only": (
            _intent("pi_event_only"),
            ["payment_intent.succeeded", "charge.refunded"],
            ["settlement_mismatch"],
        ),
        # Refunded at the processor, no webhook ever seen: both are reported.
        "silent_refund": (
            _intent("pi_silent_refund", refunded=1000),
            [],
            ["refunded", "unconfirmed"],
        ),
    }
    for run_id, (intent, seen, _) in cases.items():
        pid = f"pi_{run_id}"
        _capture(run_id, pid)
        if intent is not None:
            proc.intents[pid] = intent
        _seen(pid, *seen)
    _capture("sim", "PAY-abc123", provider="simulated")

    report = reconcile.reconcile(transport=httpx.MockTransport(proc.handler))

    by_run = _by_run(report)
    for run_id, (_, _, expected) in cases.items():
        assert sorted(by_run[run_id].discrepancies) == sorted(expected), run_id
    assert report.checked == len(cases)
    assert report.ok == 1
    assert report.skipped_simulated == 1
    assert report.skipped[0].run_id == "sim" and report.skipped[0].provider == "simulated"
    assert "sim" not in by_run
    for kind in reconcile.DISCREPANCIES:
        if kind != "processor_unavailable":
            assert report.counts[kind] >= 1, kind
    # Amounts stay integer cents end to end.
    for p in report.payments:
        assert isinstance(p.ledger_amount_cents, int)
        assert p.processor_amount_cents is None or isinstance(p.processor_amount_cents, int)
    assert by_run["silent_refund"].refunded_cents == 1000
    assert by_run["refunded"].processor_settlement == "refunded"
    assert by_run["disputed"].processor_settlement == "disputed"
    assert by_run["ok"].processor_settlement == "confirmed"
    assert by_run["not_succeeded"].processor_settlement == "failed"


def test_processor_unreachable_is_reported_per_payment(proc):
    _capture("r1", "pi_down")

    def down(request):
        raise httpx.ConnectError("down")

    report = reconcile.reconcile(transport=httpx.MockTransport(down))
    assert _by_run(report)["r1"].discrepancies == ["processor_unavailable", "unconfirmed"]


def test_only_simulated_captures_need_no_key(monkeypatch):
    monkeypatch.delenv(processor.KEY_ENV, raising=False)
    _capture("sim", "PAY-1", provider="simulated")
    report = reconcile.reconcile(transport=None)
    assert report.checked == 0 and report.skipped_simulated == 1


@pytest.mark.parametrize("key", [None, "sk_live_" + "x" * 24, "pk_test_abc"])
def test_reconcile_refuses_non_test_keys_before_any_request(monkeypatch, key):
    if key is None:
        monkeypatch.delenv(processor.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(processor.KEY_ENV, key)
    _capture("r1", "pi_x")

    def never(request):
        raise AssertionError("no request may be sent")

    with pytest.raises(processor.ProcessorNotConfigured):
        reconcile.reconcile(transport=httpx.MockTransport(never))


def test_live_mode_answer_is_refused(proc):
    _capture("r1", "pi_live")
    proc.intents["pi_live"] = {**_intent("pi_live"), "livemode": True}
    with pytest.raises(processor.ProcessorNotConfigured):
        reconcile.reconcile(transport=httpx.MockTransport(proc.handler))


def test_odd_payment_ids_never_reach_the_url(proc):
    _capture("r1", "pi_../../v1/charges")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(500)

    report = reconcile.reconcile(transport=httpx.MockTransport(handler))
    assert seen == []
    assert "missing_at_processor" in _by_run(report)["r1"].discrepancies


def test_cli_prints_report_and_appends_one_audit_entry(proc, monkeypatch, tmp_path, capsys):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(payments, "PROCESSOR_TRANSPORT", httpx.MockTransport(proc.handler))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    _capture("r1", "pi_cli")
    proc.intents["pi_cli"] = _intent("pi_cli", refunded=5600)
    _capture("sim", "PAY-2", provider="simulated")

    code = reconcile.main(["--json"])

    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checked"] == 1 and report["skipped_simulated"] == 1
    (entry,) = AuditLog(audit_path).read()
    assert entry.event == "reconciliation_run" and entry.run_id == "reconcile"
    assert entry.data["counts"]["refunded"] == 1 and entry.data["skipped_simulated"] == 1
    assert "pi_cli" not in audit_path.read_text()  # counts only


def test_cli_without_a_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv(processor.KEY_ENV, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    _capture("r1", "pi_x")
    assert reconcile.main(["--no-audit"]) == 2
    assert "test-mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------
# POST /reconcile: the report, settlement mirrored from the processor, one audit entry per call
# ---------------------------------------------------------------------------------------------


async def test_reconcile_endpoint_applies_a_dashboard_refund(client, fake_processor, audit):
    view = await _paid_run(client)
    run_id, pid = view["run_id"], view["payment"]["payment_id"]
    # Confirmed by webhook, then refunded in the processor dashboard (the webhook was missed).
    body, headers = _signed(_event("payment_intent.succeeded", pid))
    assert (await client.post("/webhooks/stripe", content=body, headers=headers)).status_code == 200
    fake_processor.intents[pid]["latest_charge"]["amount_refunded"] = 5600

    resp = await client.post("/reconcile")

    assert resp.status_code == 200, resp.text
    result = resp.json()
    item = result["report"]["payments"][0]
    assert item["discrepancies"] == ["refunded", "settlement_mismatch"]
    assert result["settlement_updates"] == [
        {"run_id": run_id, "payment_id": pid, "previous": "confirmed", "settlement": "refunded"}
    ]
    assert (await client.get(f"/runs/{run_id}")).json()["settlement"] == "refunded"
    run_entries = [e for e in audit.read(run_id) if e.actor == "processor"]
    assert [e.event for e in run_entries] == ["payment_confirmed", "settlement_reconciled"]
    (summary,) = audit.read("reconcile")
    assert summary.event == "reconciliation_run"
    assert summary.data["checked"] == 1 and summary.data["settlement_updates"] == 1

    # A second run of reconciliation changes nothing more, but is audited again.
    again = (await client.post("/reconcile")).json()
    assert again["settlement_updates"] == []
    assert len(audit.read("reconcile")) == 2
    assert audit.verify().ok


async def test_reconcile_endpoint_corrects_a_settlement_the_processor_does_not_show(
    client, fake_processor
):
    view = await _paid_run(client)
    run_id, pid = view["run_id"], view["payment"]["payment_id"]
    # A correctly signed but untrue refund event (e.g. the synthetic demo event).
    body, headers = _signed(_event("charge.refunded", pid))
    await client.post("/webhooks/stripe", content=body, headers=headers)
    assert (await client.get(f"/runs/{run_id}")).json()["settlement"] == "refunded"

    result = (await client.post("/reconcile")).json()

    assert "settlement_mismatch" in result["report"]["payments"][0]["discrepancies"]
    assert (await client.get(f"/runs/{run_id}")).json()["settlement"] == "confirmed"


async def test_reconcile_endpoint_without_a_key_is_503(client, monkeypatch):
    monkeypatch.delenv(processor.KEY_ENV, raising=False)
    _capture("r1", "pi_x")
    resp = await client.post("/reconcile")
    assert resp.status_code == 503


async def test_reconcile_endpoint_skips_simulated_captures(client, monkeypatch):
    monkeypatch.delenv("PAYMENTS_PROVIDER", raising=False)
    await _paid_run(client)
    result = (await client.post("/reconcile")).json()
    assert result["report"]["checked"] == 0
    assert result["report"]["skipped_simulated"] == 1
