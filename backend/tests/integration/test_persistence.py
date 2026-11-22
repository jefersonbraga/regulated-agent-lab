"""Agent-side persistence: checkpoints survive an API restart, run history, restricted serde."""

import sqlite3

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from regulated_agent_lab.agents.graph import checkpoint_serde
from regulated_agent_lab.agents.llm import MockLLM
from regulated_agent_lab.agents.persistence import RunStore, open_checkpointer
from regulated_agent_lab.agents.state import Quote
from regulated_agent_lab.agents.tools import GatedToolClient, make_clients
from regulated_agent_lab.api.server import create_app
from regulated_agent_lab.controls.audit_log import AuditLog, VerifyResult
from regulated_agent_lab.controls.policy import ToolPolicy
from regulated_agent_lab.services.payments.server import LEDGER
from tests.integration.test_api import CHARGE, FIXTURES, MESSAGE, _approval, _clean_ledger, serve  # noqa: F401

PII_EMAIL = "anna.muster@example.org"
PII_PASSPORT = "X1234567"


def _app(tmp_path, audit):
    # A fresh gate and fresh MCP clients each time: nothing survives but the files.
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    return create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "lab.db")


async def test_paused_run_is_approved_after_an_api_restart(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    async with serve(_app(tmp_path, audit)) as first:
        run = (await first.post("/runs", json={"message": MESSAGE})).json()
    assert run["status"] == "awaiting_approval"
    assert LEDGER == {}

    # "Restart": a new app, graph, checkpointer connection and lock table on the same file.
    async with serve(_app(tmp_path, audit)) as second:
        paused = (await second.get(f"/runs/{run['run_id']}")).json()
        assert paused["status"] == "awaiting_approval"
        assert paused["interrupt"]["quote_hash"] == run["interrupt"]["quote_hash"]
        # The offer is revived as our own model: the quote endpoint can still price from it.
        recommended = [leg["recommended_fare_id"] for leg in paused["interrupt"]["offer"]["legs"]]
        quote = await second.post(f"/runs/{run['run_id']}/quote", json={"fare_ids": recommended})
        assert quote.status_code == 200 and quote.json()["quote_hash"] == run["quote"]["quote_hash"]

        url = f"/runs/{run['run_id']}/approval"
        done = await second.post(url, json=_approval(run))
        again = await second.post(url, json=_approval(run))

        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"
        assert done.json()["trajectory"].count(CHARGE) == 1
        assert again.status_code == 409
        assert len(LEDGER) == 1
        assert LEDGER[run["run_id"]]["amount_cents"] == 5600
        (summary,) = (await second.get("/runs")).json()
        assert summary["run_id"] == run["run_id"]
        assert summary["status"] == "completed"
        assert summary["total_cents"] == 5600 and summary["currency"] == "CHF"
        assert summary["payment_id"] == done.json()["payment"]["payment_id"]
    assert audit.verify().ok


async def test_unknown_run_is_404_after_restart(tmp_path):
    async with serve(_app(tmp_path, AuditLog(tmp_path / "audit.jsonl"))) as client:
        body = {"approved": True, "approver": "alice", "quote_hash": "x"}
        assert (await client.post("/runs/nope/approval", json=body)).status_code == 404
        assert (await client.get("/runs/nope")).status_code == 404


async def test_runs_list_is_newest_first_limited_and_redacted(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    pii_message = f"{MESSAGE} Contact {PII_EMAIL}, passport {PII_PASSPORT}."
    async with serve(_app(tmp_path, audit)) as client:
        ids = []
        for message in (MESSAGE, "", pii_message):
            ids.append((await client.post("/runs", json={"message": message})).json()["run_id"])

        listed = (await client.get("/runs")).json()
        limited = (await client.get("/runs", params={"limit": 2})).json()
        assert (await client.get("/runs", params={"limit": 0})).status_code == 422

    assert [r["run_id"] for r in listed] == ids[::-1]
    assert [r["run_id"] for r in limited] == ids[:0:-1]
    newest = listed[0]
    assert newest["status"] == "awaiting_approval"
    assert newest["total_cents"] == 5600 and isinstance(newest["total_cents"], int)
    assert PII_EMAIL not in newest["request_redacted"]
    assert PII_PASSPORT not in newest["request_redacted"]
    assert "[REDACTED:email]" in newest["request_redacted"]
    assert listed[1]["status"] == "needs_clarification" and listed[1]["total_cents"] is None

    # The stored row itself is redacted, not just the API answer.
    with sqlite3.connect(tmp_path / "lab.db") as conn:
        stored = [r[0] for r in conn.execute("SELECT request_redacted FROM runs")]
        kinds = {r[0] for r in conn.execute("SELECT typeof(total_cents) FROM runs")}
    assert len(stored) == 3
    assert not any(PII_EMAIL in s or PII_PASSPORT in s for s in stored)
    assert kinds <= {"integer", "null"}


def _quote() -> Quote:
    return Quote.build([], 0, "test")


async def test_persistent_checkpoints_keep_the_restricted_deserialisation(tmp_path):
    db = tmp_path / "lab.db"
    config = {"configurable": {"thread_id": "t-1", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    # A type outside STATE_TYPES (here a pydantic model of the audit module) next to an allowed
    # one, as a tampered row or a foreign writer could leave in the file.
    foreign = VerifyResult(ok=True, entries=1, first_bad_seq=None, reason=None)
    checkpoint["channel_values"] = {"quote": _quote(), "foreign": foreign}
    async with open_checkpointer(db) as saver:
        await saver.aput(config, checkpoint, {}, {})

    async with open_checkpointer(db) as reopened:
        loaded = await reopened.aget_tuple({"configurable": {"thread_id": "t-1"}})
    values = loaded.checkpoint["channel_values"]
    assert isinstance(values["quote"], Quote)
    assert values["quote"] == _quote()
    # Not constructed as that type: only its raw data comes back.
    assert not isinstance(values["foreign"], VerifyResult)
    assert values["foreign"] == foreign.model_dump()


async def test_persistent_saver_uses_the_restricted_serializer(tmp_path):
    async with open_checkpointer(tmp_path / "lab.db") as saver:
        serde = saver.serde
    expected = checkpoint_serde()
    foreign = VerifyResult(ok=False, entries=0, first_bad_seq=None, reason=None)
    blob = expected.dumps_typed({"x": foreign})
    assert not isinstance(serde.loads_typed(blob)["x"], VerifyResult)


async def test_checkpoints_never_hold_emails_or_document_numbers(tmp_path):
    # Data minimisation: the message is redacted before it enters the graph, so neither the
    # model nor the on-disk checkpoints ever see the e-mail or passport number.
    from regulated_agent_lab.agents.graph import initial_state

    state = initial_state("r1", "Zurich to Bern for Anna, anna@example.ch, passport X1234567.")
    assert "anna@example.ch" not in state["user_message"]
    assert "X1234567" not in state["user_message"]
    assert "[REDACTED:email]" in state["user_message"]


async def test_run_store_adds_the_settlement_column_to_an_older_file(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, request_redacted TEXT NOT NULL, status TEXT NOT NULL, "
        "total_cents INTEGER, currency TEXT, payment_id TEXT, flags TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute(
        "INSERT INTO runs VALUES ('r-old', '2026-11-20T10:00:00+00:00', "
        "'2026-11-20T10:00:00+00:00', 'old run', 'completed', 5600, 'CHF', 'PAY-1', '[]')"
    )
    conn.commit()
    conn.close()
    async with RunStore.open(path) as store:
        old = await store.get("r-old")
        assert old is not None and old.settlement is None
        await store.start("r-new", "new run")
        await store.update(
            "r-new", status="completed", payment_id="pi_1", initial_settlement="pending"
        )
        assert (await store.get("r-new")).settlement == "pending"
        # A processor event recorded first is never overwritten by the run's own record.
        assert await store.set_settlement("r-new", "refunded") == "pending"
        await store.update(
            "r-new", status="completed", payment_id="pi_1", initial_settlement="pending"
        )
        assert (await store.get("r-new")).settlement == "refunded"
        with pytest.raises(ValueError):
            await store.set_settlement("r-new", "paid")  # type: ignore[arg-type]
