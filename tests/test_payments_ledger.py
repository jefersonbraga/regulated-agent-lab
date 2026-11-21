"""The payments ledger in its own SQLite file: one capture per idempotency key, whatever happens.

Two copies of the server module stand in for two server processes (or a server before and
after a restart): separate FastMCP instances, separate locks, separate connections, one file.
"""

import asyncio
import importlib.util
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastmcp import Client

import mcp_servers.payments_sandbox.server as payments
from controls.identity import issue_token

N_CONCURRENT = 8


def _load_copy(name: str):
    spec = importlib.util.spec_from_file_location(name, payments.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "payments.db"
    monkeypatch.setenv("PAYMENTS_DB_PATH", str(path))
    yield path
    ledger = payments._LEDGERS.pop(str(path), None)
    if ledger is not None:
        ledger.close()


@pytest.fixture
def servers(db):
    copies = [_load_copy("payments_server_a"), _load_copy("payments_server_b")]
    yield copies
    for module in copies:
        for ledger in module._LEDGERS.values():
            ledger.close()


def _args(**overrides) -> dict:
    return {
        "agent_token": issue_token("booking", "payments"),
        "hold_id": "H-1",
        "amount_cents": 11200,
        "currency": "CHF",
        "idempotency_key": "run-1",
        "approval_ref": "alice:hash",
    } | overrides


def _rows(path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT idempotency_key, payment_id, typeof(amount_cents), amount_cents FROM payments"
        ).fetchall()


async def test_second_server_instance_on_the_same_file_does_not_capture_again(servers, db):
    a, b = servers
    async with Client(a.mcp) as client:
        first = (await client.call_tool("charge", _args())).data
    async with Client(b.mcp) as client:  # another process, or the same one after a restart
        second = (await client.call_tool("charge", _args())).data

    assert first == second
    assert _rows(db) == [("run-1", first["payment_id"], "integer", 11200)]
    # The ledger view of a third instance (the real module) reads the same row.
    assert payments.LEDGER["run-1"]["payment_id"] == first["payment_id"]
    assert payments.LEDGER["run-1"]["approval_ref"] == "alice:hash"
    assert payments.LEDGER["run-1"]["created_at"]


def test_concurrent_charges_with_the_same_key_capture_once(servers, db):
    barrier = threading.Barrier(N_CONCURRENT)

    def charge(i: int) -> dict:
        module = servers[i % 2]
        barrier.wait()
        # Each thread is its own caller with its own event loop (charge is an async tool).
        return asyncio.run(module.charge.fn(**_args()))

    with ThreadPoolExecutor(N_CONCURRENT) as pool:
        results = list(pool.map(charge, range(N_CONCURRENT)))

    assert len({r["payment_id"] for r in results}) == 1
    assert len(_rows(db)) == 1


def test_primary_key_decides_a_lost_race(db):
    first, second = payments.Ledger(str(db)), payments.Ledger(str(db))
    record = {
        "idempotency_key": "run-1",
        "payment_id": "PAY-first",
        "provider": "simulated",
        "status": "captured",
        "amount_cents": 5600,
        "currency": "CHF",
        "hold_id": "H-1",
        "approval_ref": "alice:hash",
    }
    try:
        kept, inserted = first.insert_or_get(record)
        # Another connection looked the key up before the first insert and lost the race.
        again, inserted_again = second.insert_or_get({**record, "payment_id": "PAY-second"})
    finally:
        first.close()
        second.close()
    assert inserted and not inserted_again
    assert kept["payment_id"] == again["payment_id"] == "PAY-first"
    assert _rows(db) == [("run-1", "PAY-first", "integer", 5600)]
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO payments (idempotency_key, payment_id, provider, status, "
            "amount_cents, currency, hold_id, approval_ref, created_at) VALUES "
            "('run-1', 'PAY-x', 's', 'captured', 1, 'CHF', 'h', 'a', 't')"
        )
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO payments (idempotency_key, payment_id, provider, status, "
            "amount_cents, currency, hold_id, approval_ref, created_at) VALUES "
            "('run-2', 'PAY-y', 's', 'captured', 1.5, 'CHF', 'h', 'a', 't')"
        )


def test_ledger_uses_its_own_file_not_the_agents_db(db, monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_DB_PATH", str(tmp_path / "lab.db"))
    payments.ledger()
    assert payments.ledger().path == str(db)
    assert not (tmp_path / "lab.db").exists()
