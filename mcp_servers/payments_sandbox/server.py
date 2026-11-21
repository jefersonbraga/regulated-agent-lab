"""payments_sandbox: a simulated, separate payments institution exposed over MCP.

It trusts nothing from the caller except a scoped agent token: only the `booking`
agent, with a token issued for server `payments`, may charge. Every charge needs a
non-empty approval reference, and the `idempotency_key` (the run id) guarantees a
single capture even if the approval is resumed twice. No real money moves.

``PAYMENTS_PROVIDER=stripe_test`` sends each capture to a real card processor in test mode
(see ``processor.py``); the default ``simulated`` records the capture in the ledger only.

Wallet payments (``PAYMENTS_FLOW=wallet`` or ``both``, test mode only): ``charge`` takes the
run's scoped ``payment_token`` (issued by the wallet institution after the human approval) and
asks the wallet to redeem it, over MCP with this institution's own identity (``payments`` for
server ``wallet``). The wallet answers with the processor Customer and PaymentMethod ids only
when the token is unused, unexpired and bound to exactly this amount, currency, merchant and
run; then an off-session PaymentIntent is created and confirmed with the run id as idempotency
key. Without a token, ``stripe_test`` charges nothing (the old fixed test card is gone). A card
that asks for strong customer authentication is answered ``authentication_required`` with
nothing stored, so the booking agent can open the hosted Checkout instead.

The ledger is the institution's own SQLite file, ``PAYMENTS_DB_PATH`` (default
``var/payments.db``), never the agent platform's database. ``idempotency_key`` is its PRIMARY
KEY, so a key is captured at most once across server restarts, subprocesses and concurrent
connections: a second insert for the key is a no-op and every caller gets the stored payment.
In ``stripe_test`` mode the processor is called first (with the same idempotency key) and the
row is written only after it succeeded, so a failed processor call records nothing.
``LEDGER`` is a read-only mapping view of that table (idempotency_key -> payment record).

Hosted Checkout (``PAYMENTS_FLOW=checkout``, test mode only): ``create_checkout`` creates a
Checkout Session for the approved total and records it as ``checkout_pending`` under the same
PRIMARY KEY (``payment_id`` holds the session id until the processor reports the PaymentIntent),
so a run still has at most one payment. The row only leaves ``checkout_pending`` through
``Ledger.transition`` (a conditional UPDATE: one winner) when the processor's own record says
so (``checkout.py``): ``captured``, ``checkout_expired``, ``checkout_canceled`` or
``amount_mismatch``. ``checkout_status`` is the read-only view the booking agent resumes with.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from controls.identity import issue_token, verify_token
from mcp_servers.payments_sandbox import processor

SERVER_NAME = "payments"
ALLOWED_AGENT = "booking"

mcp = FastMCP(SERVER_NAME)

PROVIDERS = ("simulated", "stripe_test")
# Tests swap in an httpx.MockTransport; None means the real network.
PROCESSOR_TRANSPORT: Any = None

# The merchant this institution charges for: a wallet token must name it to be redeemed.
MERCHANT_ID = "demo-rail-lab"
WALLET_SERVER = "wallet"
WALLET_CALL_TIMEOUT_S = 8.0
# Builds the MCP client to the wallet institution. None: in-process (the in-memory servers of
# tests); the stdio server (``__main__``) talks to a wallet subprocess of its own.
WALLET_CLIENT_FACTORY: Callable[[], Client] | None = None
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _stdio_wallet_client() -> Client:
    env = {**os.environ, "FASTMCP_SHOW_CLI_BANNER": "false", "FASTMCP_CHECK_FOR_UPDATES": "off"}
    return Client(
        StdioTransport(
            command=sys.executable,
            args=["-m", "mcp_servers.wallet.server"],
            env=env,
            cwd=str(_REPO_ROOT),
            keep_alive=False,
        )
    )


def _wallet_client() -> Client:
    if WALLET_CLIENT_FACTORY is not None:
        return WALLET_CLIENT_FACTORY()
    from mcp_servers.wallet.server import mcp as wallet_mcp

    return Client(wallet_mcp)


def _provider() -> str:
    provider = os.environ.get("PAYMENTS_PROVIDER", "simulated")
    if provider not in PROVIDERS:
        raise ToolError("payments provider not configured")
    return provider


PAYMENTS_DB_ENV = "PAYMENTS_DB_PATH"
DEFAULT_PAYMENTS_DB = "var/payments.db"
BUSY_TIMEOUT_S = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    idempotency_key TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
        CHECK (typeof(amount_cents) = 'integer' AND amount_cents > 0),
    currency TEXT NOT NULL,
    hold_id TEXT NOT NULL,
    approval_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    checkout_session_id TEXT,
    checkout_url TEXT,
    checkout_expires_at TEXT,
    receipt_url TEXT
)
"""
_COLUMNS = (
    "idempotency_key",
    "payment_id",
    "provider",
    "status",
    "amount_cents",
    "currency",
    "hold_id",
    "approval_ref",
    "created_at",
    "checkout_session_id",
    "checkout_url",
    "checkout_expires_at",
    "receipt_url",
)
# Added after the first release: a payments.db from before gets them in place (ALTER TABLE).
_LATER_COLUMNS = ("checkout_session_id", "checkout_url", "checkout_expires_at", "receipt_url")

# Ledger statuses. Only ``checkout_pending`` ever changes (see ``Ledger.transition``).
CAPTURED = "captured"
CHECKOUT_PENDING = "checkout_pending"
CHECKOUT_EXPIRED = "checkout_expired"
CHECKOUT_CANCELED = "checkout_canceled"
AMOUNT_MISMATCH = "amount_mismatch"
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM payments"


class Ledger:
    """The payments table in one SQLite file, over one connection (serialised in-process).

    Across processes SQLite's own locking applies: writes run in ``BEGIN IMMEDIATE``
    transactions and the PRIMARY KEY decides which capture of a key is kept.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: transactions are explicit (BEGIN IMMEDIATE ... COMMIT).
        self._conn = sqlite3.connect(
            path, timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False
        )
        self._lock = threading.Lock()
        with self._lock:
            self._init_schema()

    def _init_schema(self) -> None:
        # Switching a fresh file to WAL takes an exclusive lock and does not wait for the busy
        # timeout, so two processes opening it at once retry briefly instead of failing.
        deadline = time.monotonic() + BUSY_TIMEOUT_S
        while True:
            try:
                if self.path != ":memory:":
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute(_SCHEMA)
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(payments)")}
                for column in _LATER_COLUMNS:
                    if column not in columns:
                        self._conn.execute(f"ALTER TABLE payments ADD COLUMN {column} TEXT")
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS payments_checkout_session "
                    "ON payments (checkout_session_id)"
                )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    @staticmethod
    def _record(row: tuple) -> dict:
        return dict(zip(_COLUMNS, row, strict=True))

    def get(self, idempotency_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"{_SELECT} WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def get_by_payment_id(self, payment_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(f"{_SELECT} WHERE payment_id = ?", (payment_id,)).fetchone()
        return self._record(row) if row is not None else None

    def get_by_session_id(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"{_SELECT} WHERE checkout_session_id = ?", (session_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def transition(
        self, idempotency_key: str, from_status: str, to_status: str, **fields: Any
    ) -> tuple[dict | None, bool]:
        """Move a row from ``from_status`` to ``to_status`` (and set ``fields``) atomically.

        (the row afterwards, whether this call made the change). A conditional UPDATE: of two
        racing callers (a webhook and an on-demand refresh, in any process) exactly one wins.
        """
        allowed = {"payment_id", "receipt_url"}
        if not set(fields) <= allowed:
            raise ValueError("only payment_id and receipt_url can change with the status")
        sets = ", ".join(["status = ?", *(f"{k} = ?" for k in fields)])
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    f"UPDATE payments SET {sets} WHERE idempotency_key = ? AND status = ?",
                    (to_status, *fields.values(), idempotency_key, from_status),
                )
                changed = cur.rowcount == 1
                row = self._conn.execute(
                    f"{_SELECT} WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return (self._record(row) if row is not None else None), changed

    def insert_or_get(self, record: dict) -> tuple[dict, bool]:
        """Store ``record`` unless its key exists; (the stored record, whether it was new)."""
        values = {
            **{c: None for c in _LATER_COLUMNS},
            **record,
            "created_at": record.get("created_at") or _now(),
        }
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    f"INSERT INTO payments ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
                    "ON CONFLICT (idempotency_key) DO NOTHING",
                    tuple(values[c] for c in _COLUMNS),
                )
                inserted = cur.rowcount == 1
                row = self._conn.execute(
                    f"{_SELECT} WHERE idempotency_key = ?", (values["idempotency_key"],)
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._record(row), inserted

    def all(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(f"{_SELECT} ORDER BY created_at").fetchall()
        return {r[0]: self._record(r) for r in rows}

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM payments")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def payments_db_path() -> str:
    return os.environ.get(PAYMENTS_DB_ENV) or DEFAULT_PAYMENTS_DB


_LEDGERS: dict[str, Ledger] = {}
_LEDGERS_LOCK = threading.Lock()


def ledger() -> Ledger:
    """The ledger for the current ``PAYMENTS_DB_PATH`` (one connection per path)."""
    path = payments_db_path()
    with _LEDGERS_LOCK:
        if path not in _LEDGERS:
            _LEDGERS[path] = Ledger(path)
        return _LEDGERS[path]


class _LedgerView(Mapping[str, dict]):
    """Read-only ``idempotency_key -> record`` view of the current ledger (tests, debugging)."""

    def __getitem__(self, key: str) -> dict:
        record = ledger().get(key)
        if record is None:
            raise KeyError(key)
        return record

    def __iter__(self) -> Iterator[str]:
        return iter(ledger().all())

    def __len__(self) -> int:
        return len(ledger().all())

    def __repr__(self) -> str:
        return f"LEDGER({ledger().all()!r})"


LEDGER: Mapping[str, dict] = _LedgerView()
# In-process: one capture attempt (and processor call) per server at a time.
_LOCK = threading.Lock()


def reset_ledger() -> None:
    """Empty the current ledger (tests only: point PAYMENTS_DB_PATH at a temporary file)."""
    with _LOCK:
        ledger().clear()


def _public(record: dict) -> dict:
    """What a caller learns about a payment: ids, status, integer cents, public URLs."""
    public = {
        "payment_id": record["payment_id"],
        "status": record["status"],
        "amount_cents": record["amount_cents"],
        "currency": record["currency"],
    }
    if record.get("receipt_url"):
        public["receipt_url"] = record["receipt_url"]
    if record.get("checkout_session_id"):
        public["session_id"] = record["checkout_session_id"]
        if record["status"] == CHECKOUT_PENDING:
            public["checkout_url"] = record["checkout_url"]
            public["expires_at"] = record["checkout_expires_at"]
    return public


def _authorize(agent_token: str) -> None:
    try:
        agent = verify_token(agent_token, SERVER_NAME)
    except (ValueError, RuntimeError) as exc:
        raise ToolError("unauthorized") from exc
    if agent != ALLOWED_AGENT:
        raise ToolError("unauthorized")


def _check_payment_args(amount_cents: Any, idempotency_key: str, approval_ref: str) -> None:
    if not approval_ref or not approval_ref.strip():
        raise ToolError("approval required")
    if not idempotency_key or not idempotency_key.strip():
        raise ToolError("idempotency_key required")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ToolError("amount_cents must be a positive integer")


async def _redeem(payment_token: str, amount_cents: int, currency: str, run_id: str) -> dict:
    """Ask the wallet institution to spend the run's token; its processor ids, or ToolError."""
    args = {
        "agent_token": issue_token(SERVER_NAME, WALLET_SERVER),
        "payment_token": payment_token,
        "amount_cents": amount_cents,
        "currency": currency,
        "merchant_id": MERCHANT_ID,
        "run_id": run_id,
    }
    try:
        async with _wallet_client() as client:
            result = await client.call_tool(
                "redeem_payment_token", args, timeout=WALLET_CALL_TIMEOUT_S
            )
    except ToolError:
        raise  # the wallet answered: replay, mismatch, expiry, other run (its own words)
    except Exception as exc:
        raise ToolError("wallet unavailable") from exc
    data = result.structured_content if isinstance(result.structured_content, dict) else {}
    customer, method = data.get("customer_id"), data.get("payment_method_id")
    if not isinstance(customer, str) or not customer.startswith("cus_"):
        raise ToolError("wallet answered without a payment method")
    if not isinstance(method, str) or not method.startswith("pm_"):
        raise ToolError("wallet answered without a payment method")
    return {"customer": customer, "payment_method": method, "token_id": data.get("token_id")}


def _store_capture(
    store: Ledger,
    *,
    idempotency_key: str,
    payment_id: str,
    provider: str,
    amount_cents: int,
    currency: str,
    hold_id: str,
    approval_ref: str,
    receipt: str | None,
) -> dict:
    # Another process may have captured this key since the lookup: the PRIMARY KEY keeps the
    # first row and this call answers with it (a processor capture used the same key).
    stored, _ = store.insert_or_get(
        {
            "idempotency_key": idempotency_key,
            "payment_id": payment_id,
            "provider": provider,
            "status": CAPTURED,
            "amount_cents": amount_cents,
            "currency": currency,
            "hold_id": hold_id,
            "approval_ref": approval_ref,
            "receipt_url": receipt,
        }
    )
    return _public(stored)


def _charge_simulated(
    hold_id: str, amount_cents: int, currency: str, idempotency_key: str, approval_ref: str
) -> dict:
    with _LOCK:
        store = ledger()
        existing = store.get(idempotency_key)
        if existing is not None:
            return _public(existing)
        return _store_capture(
            store,
            idempotency_key=idempotency_key,
            payment_id=f"PAY-{uuid.uuid4().hex[:12]}",
            provider="simulated",
            amount_cents=amount_cents,
            currency=currency,
            hold_id=hold_id,
            approval_ref=approval_ref,
            receipt=None,
        )


def _charge_off_session(
    instrument: dict,
    hold_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    approval_ref: str,
    quote_hash: str | None,
) -> dict:
    """The off-session capture with the wallet's ids; SCA and declines store nothing."""
    with _LOCK:
        store = ledger()
        existing = store.get(idempotency_key)
        if existing is not None:
            return _public(existing)
        metadata = {"run_id": idempotency_key, "hold_id": hold_id, "approval_ref": approval_ref}
        if quote_hash:
            metadata["quote_hash"] = quote_hash
        metadata["payment_model"] = "wallet"
        try:
            captured = processor.capture(
                amount_cents,
                currency,
                idempotency_key,
                metadata,
                customer=instrument["customer"],
                payment_method=instrument["payment_method"],
                transport=PROCESSOR_TRANSPORT,
            )
        except processor.ProcessorNotConfigured as exc:
            raise ToolError("payments provider not configured") from exc
        except processor.AuthenticationRequired:
            # Nothing captured, nothing stored: the run can open the hosted Checkout, where the
            # card holder authenticates with their bank.
            return {
                "status": "authentication_required",
                "amount_cents": amount_cents,
                "currency": currency,
            }
        except processor.PaymentDeclined:
            return {"status": "declined", "amount_cents": amount_cents, "currency": currency}
        except processor.ProcessorUnavailable as exc:
            raise ToolError("payment processor unavailable") from exc
        return _store_capture(
            store,
            idempotency_key=idempotency_key,
            payment_id=captured["payment_id"],
            provider="stripe_test",
            amount_cents=amount_cents,
            currency=currency,
            hold_id=hold_id,
            approval_ref=approval_ref,
            receipt=captured.get("receipt_url"),
        )


@mcp.tool
async def charge(
    agent_token: str,
    hold_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    approval_ref: str,
    payment_token: str | None = None,
    quote_hash: str | None = None,
) -> dict:
    """Capture the payment for a seat hold (requires a booking-agent token).

    Simulated: recorded in the ledger. Test-mode processor: only with the run's scoped wallet
    ``payment_token``, redeemed once at the wallet institution, then charged off-session.
    Idempotent per ``idempotency_key`` (the run id).
    """
    _authorize(agent_token)
    _check_payment_args(amount_cents, idempotency_key, approval_ref)
    provider = _provider()
    existing = ledger().get(idempotency_key)
    if existing is not None:
        return _public(existing)
    if provider != "stripe_test":
        return await asyncio.to_thread(
            _charge_simulated, hold_id, amount_cents, currency, idempotency_key, approval_ref
        )
    if not payment_token:
        raise ToolError("payment token required")
    if quote_hash is not None and not approval_ref.endswith(f":{quote_hash}"):
        raise ToolError("approval required")
    instrument = await _redeem(payment_token, amount_cents, currency, idempotency_key)
    return await asyncio.to_thread(
        _charge_off_session,
        instrument,
        hold_id,
        amount_cents,
        currency,
        idempotency_key,
        approval_ref,
        quote_hash,
    )


PUBLIC_APP_URL_ENV = "PUBLIC_APP_URL"
DEFAULT_PUBLIC_APP_URL = "http://localhost:5173"


def public_app_url() -> str:
    """Where the processor sends the customer back (the UI), without a trailing slash."""
    url = (os.environ.get(PUBLIC_APP_URL_ENV) or DEFAULT_PUBLIC_APP_URL).strip().rstrip("/")
    if not url.startswith(("https://", "http://")) or any(c in url for c in "?#\"' <>"):
        raise ToolError("PUBLIC_APP_URL must be an http(s) URL without query or fragment")
    return url


def return_urls(run_id: str) -> tuple[str, str]:
    """(success_url, cancel_url): the UI reopens the run from ``?run=<id>&checkout=...``."""
    base = public_app_url()
    return (
        f"{base}/?run={run_id}&checkout=success",
        f"{base}/?run={run_id}&checkout=cancel",
    )


@mcp.tool
def create_checkout(
    agent_token: str,
    hold_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    approval_ref: str,
    quote_hash: str,
) -> dict:
    """Open a hosted Checkout Session for the approved total (test mode, booking agent only).

    Nothing is captured here: the customer confirms on the processor's page. Idempotent per
    ``idempotency_key`` (the run id): a second call answers with the stored checkout.
    """
    _authorize(agent_token)
    _check_payment_args(amount_cents, idempotency_key, approval_ref)
    if not quote_hash or not approval_ref.endswith(f":{quote_hash}"):
        raise ToolError("approval required")
    if _provider() != "stripe_test":
        raise ToolError("checkout needs the test-mode card processor")
    with _LOCK:
        store = ledger()
        existing = store.get(idempotency_key)
        if existing is not None:
            return _public(existing)
        success_url, cancel_url = return_urls(idempotency_key)
        expires_at = int(time.time()) + processor.CHECKOUT_MIN_EXPIRY_S + 60
        try:
            session = processor.create_checkout_session(
                amount_cents=amount_cents,
                currency=currency,
                run_id=idempotency_key,
                quote_hash=quote_hash,
                approval_ref=approval_ref,
                success_url=success_url,
                cancel_url=cancel_url,
                expires_at=expires_at,
                transport=PROCESSOR_TRANSPORT,
            )
        except processor.ProcessorNotConfigured as exc:
            raise ToolError("payments provider not configured") from exc
        except (processor.ProcessorUnavailable, processor.PaymentDeclined) as exc:
            raise ToolError("payment processor unavailable") from exc
        expiry = session.get("expires_at")
        stored, _ = store.insert_or_get(
            {
                "idempotency_key": idempotency_key,
                "payment_id": session["id"],
                "provider": "stripe_test",
                "status": CHECKOUT_PENDING,
                "amount_cents": amount_cents,
                "currency": currency,
                "hold_id": hold_id,
                "approval_ref": approval_ref,
                "checkout_session_id": session["id"],
                "checkout_url": session["url"],
                "checkout_expires_at": datetime.fromtimestamp(
                    expiry if isinstance(expiry, int) else expires_at, UTC
                ).isoformat(),
            }
        )
    return _public(stored)


@mcp.tool
def checkout_status(agent_token: str, idempotency_key: str) -> dict:
    """The ledger's record of this run's payment (read-only; booking agent only)."""
    _authorize(agent_token)
    record = ledger().get(idempotency_key)
    if record is None:
        raise ToolError("no payment for this run")
    return _public(record)


if __name__ == "__main__":
    # A separate process: it reaches the wallet institution over its own stdio subprocess.
    WALLET_CLIENT_FACTORY = _stdio_wallet_client
    mcp.run(show_banner=False)
