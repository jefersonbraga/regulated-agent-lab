"""The wallet institution's records: saved-card references, spending mandates, scoped tokens.

The wallet is a third institution, next to the agents (the merchant) and the payments
institution (the merchant's payment service provider in front of the card processor). It has
its own SQLite file, ``WALLET_DB_PATH`` (default ``var/wallet.db``), and its own trust boundary:
agents reach it only through the gated client (``mcp_servers.wallet.server``), the payments
institution redeems tokens over MCP with its own service identity, and the signed-in user
manages their card and limit through the API, which calls the functions here.

**No card data here.** The card is typed by the customer on the processor's hosted page
(``mode=setup``) and vaulted by the processor. The wallet stores only what it needs to have the
card charged later (processor Customer and PaymentMethod ids) and what the user is shown
(brand, last four digits, expiry). Nothing here is ever returned to an agent except an opaque
token and its bounds.

**Scoped payment token.** After the human approved a quote and chose "agent pays from my
wallet", the booking agent asks for a token (``issue_payment_token``): single use, bound to
the exact approved amount, the currency, the merchant, the run and a 15-minute expiry, and
refused when there is no saved card or the amount is above the user's mandate (a spending limit
per booking, in integer cents). Only the SHA-256 of the token is stored. The payments
institution redeems it (``redeem_payment_token``) for the Customer and PaymentMethod ids, once:
a replay, another amount, currency, merchant or run, or an expired token is refused and nothing
is charged. This mirrors the scoped, single-use tokens of agentic-commerce schemes.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_servers.wallet import processor

WALLET_DB_ENV = "WALLET_DB_PATH"
DEFAULT_WALLET_DB = "var/wallet.db"
BUSY_TIMEOUT_S = 30.0

# The one merchant this lab's wallet issues tokens for (the agent platform selling Demo Rail
# trips). The payments institution redeems with the same id; a token for another merchant fails.
MERCHANT_ID = "demo-rail-lab"
KNOWN_MERCHANTS = frozenset({MERCHANT_ID})
CURRENCY = "CHF"
DEFAULT_MANDATE_CENTS = 20000  # CHF 200 per booking
MIN_MANDATE_CENTS = 100
MAX_MANDATE_CENTS = 1_000_000
TOKEN_TTL_S = 15 * 60
# Tests swap in an httpx.MockTransport; None means the real network.
PROCESSOR_TRANSPORT: Any = None

PUBLIC_APP_URL_ENV = "PUBLIC_APP_URL"
DEFAULT_PUBLIC_APP_URL = "http://localhost:5173"

_BRAND_RE = re.compile(r"^[a-z_]{1,20}$")
_LAST4_RE = re.compile(r"^\d{4}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    user TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    user TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    payment_method_id TEXT NOT NULL UNIQUE,
    brand TEXT NOT NULL,
    last4 TEXT NOT NULL CHECK (length(last4) = 4),
    exp_month INTEGER NOT NULL CHECK (exp_month BETWEEN 1 AND 12),
    exp_year INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_sessions (
    session_id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'complete', 'expired')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS setup_sessions_user ON setup_sessions (user, created_at);
CREATE TABLE IF NOT EXISTS mandates (
    user TEXT PRIMARY KEY,
    limit_cents INTEGER NOT NULL CHECK (typeof(limit_cents) = 'integer' AND limit_cents > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user TEXT NOT NULL,
    run_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents > 0),
    currency TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    approval_ref TEXT NOT NULL,
    mandate_cents INTEGER NOT NULL,
    payment_method_id TEXT NOT NULL,
    brand TEXT NOT NULL,
    last4 TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    redeemed_at TEXT
);
CREATE INDEX IF NOT EXISTS payment_tokens_run ON payment_tokens (run_id, issued_at);
"""

_CARD_COLUMNS = (
    "user",
    "customer_id",
    "payment_method_id",
    "brand",
    "last4",
    "exp_month",
    "exp_year",
    "created_at",
)
_TOKEN_COLUMNS = (
    "token_id",
    "token_hash",
    "user",
    "run_id",
    "amount_cents",
    "currency",
    "merchant_id",
    "approval_ref",
    "mandate_cents",
    "payment_method_id",
    "brand",
    "last4",
    "issued_at",
    "expires_at",
    "redeemed_at",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def wallet_db_path() -> str:
    return os.environ.get(WALLET_DB_ENV) or DEFAULT_WALLET_DB


class WalletStore:
    """The wallet's tables in one SQLite file, over one connection (serialised in-process).

    Across processes (the wallet MCP server, the API) SQLite's locking applies: writes run in
    ``BEGIN IMMEDIATE`` transactions, and a token is redeemed by a conditional UPDATE, so of two
    racing redemptions exactly one wins.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False
        )
        self._lock = threading.Lock()
        deadline = time.monotonic() + BUSY_TIMEOUT_S
        with self._lock:
            while True:
                try:
                    if path != ":memory:":
                        self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.executescript(_SCHEMA)
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(0.01)

    def _write(self, sql: str, params: tuple) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(sql, params)
                count = cur.rowcount
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return count

    def _one(self, sql: str, params: tuple) -> tuple | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # --- customers and cards -------------------------------------------------------------

    def customer_id(self, user: str) -> str | None:
        row = self._one("SELECT customer_id FROM customers WHERE user = ?", (user,))
        return row[0] if row else None

    def set_customer(self, user: str, customer_id: str) -> str:
        """Record the user's processor Customer unless one exists; returns the stored one."""
        self._write(
            "INSERT INTO customers (user, customer_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (user) DO NOTHING",
            (user, customer_id, _now()),
        )
        stored = self.customer_id(user)
        assert stored is not None
        return stored

    def card(self, user: str) -> dict | None:
        row = self._one(f"SELECT {', '.join(_CARD_COLUMNS)} FROM cards WHERE user = ?", (user,))
        return dict(zip(_CARD_COLUMNS, row, strict=True)) if row else None

    def save_card(self, user: str, card: dict) -> tuple[dict, bool]:
        """Store (or replace) the user's card reference; (stored, whether it changed)."""
        before = self.card(user)
        if before is not None and before["payment_method_id"] == card["payment_method_id"]:
            return before, False
        values = {**card, "user": user, "created_at": _now()}
        self._write(
            f"INSERT INTO cards ({', '.join(_CARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _CARD_COLUMNS)}) "
            "ON CONFLICT (user) DO UPDATE SET customer_id = excluded.customer_id, "
            "payment_method_id = excluded.payment_method_id, brand = excluded.brand, "
            "last4 = excluded.last4, exp_month = excluded.exp_month, "
            "exp_year = excluded.exp_year, created_at = excluded.created_at",
            tuple(values[c] for c in _CARD_COLUMNS),
        )
        stored = self.card(user)
        assert stored is not None
        return stored, True

    def delete_card(self, user: str) -> dict | None:
        card = self.card(user)
        if card is not None:
            self._write("DELETE FROM cards WHERE user = ?", (user,))
        return card

    # --- setup sessions ------------------------------------------------------------------

    def record_setup(self, session_id: str, user: str, customer_id: str) -> None:
        self._write(
            "INSERT INTO setup_sessions (session_id, user, customer_id, status, created_at) "
            "VALUES (?, ?, ?, 'open', ?) ON CONFLICT (session_id) DO NOTHING",
            (session_id, user, customer_id, _now()),
        )

    def setup_session(self, session_id: str) -> dict | None:
        row = self._one(
            "SELECT session_id, user, customer_id, status FROM setup_sessions WHERE session_id = ?",
            (session_id,),
        )
        keys = ("session_id", "user", "customer_id", "status")
        return dict(zip(keys, row, strict=True)) if row else None

    def latest_open_setup(self, user: str) -> dict | None:
        row = self._one(
            "SELECT session_id FROM setup_sessions WHERE user = ? AND status = 'open' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (user,),
        )
        return self.setup_session(row[0]) if row else None

    def close_setup(self, session_id: str, status: str) -> bool:
        """``open`` -> ``complete``/``expired``; True for the call that made the change."""
        return (
            self._write(
                "UPDATE setup_sessions SET status = ? WHERE session_id = ? AND status = 'open'",
                (status, session_id),
            )
            == 1
        )

    # --- mandates ------------------------------------------------------------------------

    def mandate(self, user: str) -> int:
        row = self._one("SELECT limit_cents FROM mandates WHERE user = ?", (user,))
        return int(row[0]) if row else DEFAULT_MANDATE_CENTS

    def set_mandate(self, user: str, limit_cents: int) -> int:
        previous = self.mandate(user)
        self._write(
            "INSERT INTO mandates (user, limit_cents, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (user) DO UPDATE SET limit_cents = excluded.limit_cents, "
            "updated_at = excluded.updated_at",
            (user, limit_cents, _now()),
        )
        return previous

    # --- payment tokens ------------------------------------------------------------------

    def insert_token(self, row: dict) -> None:
        values = {**row, "redeemed_at": None}
        self._write(
            f"INSERT INTO payment_tokens ({', '.join(_TOKEN_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _TOKEN_COLUMNS)})",
            tuple(values[c] for c in _TOKEN_COLUMNS),
        )

    def _token(self, where: str, params: tuple) -> dict | None:
        row = self._one(
            f"SELECT {', '.join(_TOKEN_COLUMNS)} FROM payment_tokens WHERE {where}", params
        )
        return dict(zip(_TOKEN_COLUMNS, row, strict=True)) if row else None

    def token_by_hash(self, token_hash: str) -> dict | None:
        return self._token("token_hash = ?", (token_hash,))

    def token_for_run(self, run_id: str) -> dict | None:
        """The run's redeemed token if any, else its latest issued one."""
        return self._token(
            "run_id = ? ORDER BY redeemed_at IS NULL, issued_at DESC, rowid DESC LIMIT 1",
            (run_id,),
        )

    def redeem(self, token_hash: str) -> bool:
        """Mark the token used, once; True only for the call that did (conditional UPDATE)."""
        return (
            self._write(
                "UPDATE payment_tokens SET redeemed_at = ? "
                "WHERE token_hash = ? AND redeemed_at IS NULL",
                (_now(), token_hash),
            )
            == 1
        )

    def tokens(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_TOKEN_COLUMNS)} FROM payment_tokens ORDER BY issued_at"
            ).fetchall()
        return [dict(zip(_TOKEN_COLUMNS, r, strict=True)) for r in rows]

    def clear(self) -> None:
        with self._lock:
            for table in ("customers", "cards", "setup_sessions", "mandates", "payment_tokens"):
                self._conn.execute(f"DELETE FROM {table}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORES: dict[str, WalletStore] = {}
_STORES_LOCK = threading.Lock()


def store() -> WalletStore:
    """The wallet store for the current ``WALLET_DB_PATH`` (one connection per path)."""
    path = wallet_db_path()
    with _STORES_LOCK:
        if path not in _STORES:
            _STORES[path] = WalletStore(path)
        return _STORES[path]


def reset_wallet() -> None:
    """Empty the current wallet (tests only: point WALLET_DB_PATH at a temporary file)."""
    store().clear()


# --- what the user sees ------------------------------------------------------------------


def card_summary(user: str) -> dict | None:
    """Brand, last four digits, expiry and when it was saved; never the processor ids."""
    card = store().card(user)
    if card is None:
        return None
    return {k: card[k] for k in ("brand", "last4", "exp_month", "exp_year", "created_at")}


def _card_expired(card: dict, now: datetime | None = None) -> bool:
    at = now or datetime.now(UTC)
    return (card["exp_year"], card["exp_month"]) < (at.year, at.month)


def set_mandate(user: str, limit_cents: Any) -> int:
    """Set the user's spending limit per booking (integer cents); returns the previous one."""
    if (
        isinstance(limit_cents, bool)
        or not isinstance(limit_cents, int)
        or not MIN_MANDATE_CENTS <= limit_cents <= MAX_MANDATE_CENTS
    ):
        raise ValueError(
            f"limit_cents must be an integer from {MIN_MANDATE_CENTS} to {MAX_MANDATE_CENTS}"
        )
    return store().set_mandate(user, limit_cents)


def wallet_unavailable(user: str, amount_cents: int, currency: str) -> str | None:
    """Why the wallet could not pay this amount now (None: it can). No side effects."""
    card = store().card(user)
    if card is None:
        return "no_saved_card"
    if _card_expired(card):
        return "card_expired"
    if currency.upper() != CURRENCY:
        return "currency_not_supported"
    if amount_cents > store().mandate(user):
        return "over_mandate"
    return None


def public_app_url() -> str:
    url = (os.environ.get(PUBLIC_APP_URL_ENV) or DEFAULT_PUBLIC_APP_URL).strip().rstrip("/")
    if not url.startswith(("https://", "http://")) or any(c in url for c in "?#\"' <>"):
        raise ValueError("PUBLIC_APP_URL must be an http(s) URL without query or fragment")
    return url


def start_setup(user: str, *, transport: Any = None) -> dict:
    """Open the processor's hosted card form (``mode=setup``) for ``user``.

    The processor Customer is created once per user and reused. Returns the page URL; the
    processor sends the browser back to ``PUBLIC_APP_URL/?wallet=saved`` (or ``=canceled``).
    """
    base = public_app_url()
    wallet = store()
    customer_id = wallet.customer_id(user)
    if customer_id is None:
        customer_id = wallet.set_customer(user, processor.create_customer(user, transport))
    session = processor.create_setup_session(
        customer_id=customer_id,
        user=user,
        success_url=f"{base}/?wallet=saved",
        cancel_url=f"{base}/?wallet=canceled",
        transport=transport,
    )
    wallet.record_setup(session["id"], user, customer_id)
    return {"session_id": session["id"], "setup_url": session["url"]}


@dataclass(frozen=True)
class SetupOutcome:
    """What reading a setup session back did. ``state``: saved, pending, expired, refused,
    not_found. ``changed`` is True only for the call that stored the card (audit once)."""

    state: str
    user: str | None = None
    changed: bool = False
    session_id: str | None = None
    reason: str | None = None
    card: dict | None = field(default=None)


def _card_from_session(session: dict) -> tuple[dict | None, str | None]:
    """(card reference, None) from a completed setup session, or (None, why not)."""
    intent = session.get("setup_intent")
    if not isinstance(intent, dict):
        return None, "pending"
    if intent.get("status") != "succeeded":
        return None, "pending"
    method = intent.get("payment_method")
    if not isinstance(method, dict) or method.get("type") != "card":
        return None, "not a card"
    card = method.get("card") if isinstance(method.get("card"), dict) else {}
    pm_id, brand, last4 = method.get("id"), card.get("brand"), card.get("last4")
    month, year = card.get("exp_month"), card.get("exp_year")
    if (
        not isinstance(pm_id, str)
        or not processor.PAYMENT_METHOD_RE.fullmatch(pm_id)
        or not isinstance(brand, str)
        or not _BRAND_RE.fullmatch(brand)
        or not isinstance(last4, str)
        or not _LAST4_RE.fullmatch(last4)
        or isinstance(month, bool)
        or not isinstance(month, int)
        or not 1 <= month <= 12
        or isinstance(year, bool)
        or not isinstance(year, int)
        or not 2000 <= year <= 2100
    ):
        return None, "unexpected card details"
    return {
        "payment_method_id": pm_id,
        "brand": brand,
        "last4": last4,
        "exp_month": month,
        "exp_year": year,
    }, None


def complete_setup_session(session_id: str, *, transport: Any = None) -> SetupOutcome:
    """Read a recorded setup session back from the processor and save its card.

    The session must be one this wallet opened, for the same user (``client_reference_id``)
    and Customer, in ``mode=setup``; only its SetupIntent's PaymentMethod is read, and only the
    ids, brand, last four digits and expiry are kept.
    """
    wallet = store()
    row = wallet.setup_session(session_id)
    if row is None:
        return SetupOutcome(state="not_found", session_id=session_id)
    user = row["user"]
    if row["status"] == "complete":
        return SetupOutcome(
            state="saved", user=user, session_id=session_id, card=card_summary(user)
        )
    if row["status"] == "expired":
        return SetupOutcome(state="expired", user=user, session_id=session_id)
    session = processor.retrieve_setup_session(session_id, transport=transport)
    if (
        session.get("mode") != "setup"
        or session.get("client_reference_id") != user
        or session.get("customer") != row["customer_id"]
    ):
        return SetupOutcome(
            state="refused", user=user, session_id=session_id, reason="reference_mismatch"
        )
    if session.get("status") == "expired":
        wallet.close_setup(session_id, "expired")
        return SetupOutcome(state="expired", user=user, session_id=session_id)
    if session.get("status") != "complete":
        return SetupOutcome(state="pending", user=user, session_id=session_id)
    card, why = _card_from_session(session)
    if card is None:
        state = "pending" if why == "pending" else "refused"
        return SetupOutcome(state=state, user=user, session_id=session_id, reason=why)
    _, changed = wallet.save_card(user, {**card, "customer_id": row["customer_id"]})
    closed = wallet.close_setup(session_id, "complete")
    return SetupOutcome(
        state="saved",
        user=user,
        changed=changed and closed,
        session_id=session_id,
        card=card_summary(user),
    )


def complete_setup(user: str, *, transport: Any = None) -> SetupOutcome:
    """The browser came back (``?wallet=saved``): complete the user's latest open setup."""
    row = store().latest_open_setup(user)
    if row is None:
        card = card_summary(user)
        return SetupOutcome(state="saved" if card else "not_found", user=user, card=card)
    return complete_setup_session(row["session_id"], transport=transport)


def remove_card(user: str, *, transport: Any = None) -> bool:
    """Forget the user's card here and detach it at the processor (best effort there)."""
    card = store().delete_card(user)
    if card is None:
        return False
    try:
        processor.detach_payment_method(card["payment_method_id"], transport)
    except processor.ProcessorNotConfigured:
        pass  # the reference is gone here either way; nothing can be charged through us
    return True


def run_payment(run_id: str) -> dict | None:
    """The card and limit a run's wallet token was issued under (for the outcome card)."""
    row = store().token_for_run(run_id)
    if row is None:
        return None
    return {
        "brand": row["brand"],
        "last4": row["last4"],
        "mandate_cents": row["mandate_cents"],
        "redeemed": row["redeemed_at"] is not None,
    }


# --- scoped payment tokens -----------------------------------------------------------------


class WalletRefused(ValueError):
    """The wallet refused a token request or a redemption; the message says why."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def issue_payment_token(
    *,
    user: str,
    amount_cents: Any,
    currency: str,
    run_id: str,
    merchant_id: str,
    approval_ref: str,
    now: float | None = None,
) -> dict:
    """A single-use token for exactly this amount, merchant and run, or why not.

    ``{"issued": False, "reason": ...}`` for a reason the run can fall back from (no saved card,
    expired card, above the mandate); ``WalletRefused`` for a malformed request.
    """
    if not _positive_int(amount_cents):
        raise WalletRefused("amount_cents must be a positive integer")
    if not user or not run_id or not approval_ref or not approval_ref.strip():
        raise WalletRefused("user, run_id and approval_ref are required")
    if merchant_id not in KNOWN_MERCHANTS:
        raise WalletRefused("unknown merchant")
    reason = wallet_unavailable(user, amount_cents, currency)
    wallet = store()
    limit = wallet.mandate(user)
    if reason is not None:
        refusal: dict[str, Any] = {"issued": False, "reason": reason}
        if reason == "over_mandate":
            refusal["mandate_cents"] = limit
        return refusal
    card = wallet.card(user)
    assert card is not None
    token = f"wpt_{secrets.token_urlsafe(32)}"
    issued = time.time() if now is None else now
    expires = int(issued) + TOKEN_TTL_S
    token_id = f"wtk_{uuid.uuid4().hex[:16]}"
    wallet.insert_token(
        {
            "token_id": token_id,
            "token_hash": _hash(token),
            "user": user,
            "run_id": run_id,
            "amount_cents": amount_cents,
            "currency": currency.upper(),
            "merchant_id": merchant_id,
            "approval_ref": approval_ref,
            "mandate_cents": limit,
            "payment_method_id": card["payment_method_id"],
            "brand": card["brand"],
            "last4": card["last4"],
            "issued_at": _now(),
            "expires_at": expires,
        }
    )
    return {
        "issued": True,
        "payment_token": token,
        "token_id": token_id,
        "amount_cents": amount_cents,
        "currency": currency.upper(),
        "merchant_id": merchant_id,
        "run_id": run_id,
        "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
        "mandate_cents": limit,
    }


def redeem_payment_token(
    *,
    payment_token: str,
    amount_cents: Any,
    currency: str,
    merchant_id: str,
    run_id: str,
    now: float | None = None,
) -> dict:
    """Spend a token once: the processor ids to charge, or ``WalletRefused`` (nothing spent)."""
    if not isinstance(payment_token, str) or not payment_token.startswith("wpt_"):
        raise WalletRefused("unknown payment token")
    wallet = store()
    token_hash = _hash(payment_token)
    row = wallet.token_by_hash(token_hash)
    if row is None:
        raise WalletRefused("unknown payment token")
    if row["redeemed_at"] is not None:
        raise WalletRefused("payment token already used")
    if (time.time() if now is None else now) >= row["expires_at"]:
        raise WalletRefused("payment token expired")
    if (
        isinstance(amount_cents, bool)
        or amount_cents != row["amount_cents"]
        or not isinstance(currency, str)
        or currency.upper() != row["currency"]
    ):
        raise WalletRefused("amount or currency does not match the payment token")
    if merchant_id != row["merchant_id"]:
        raise WalletRefused("merchant does not match the payment token")
    if run_id != row["run_id"]:
        raise WalletRefused("payment token is for another run")
    card = wallet.card(row["user"])
    if card is None or card["payment_method_id"] != row["payment_method_id"]:
        raise WalletRefused("card no longer in the wallet")
    if not wallet.redeem(token_hash):
        raise WalletRefused("payment token already used")
    return {
        "token_id": row["token_id"],
        "customer_id": card["customer_id"],
        "payment_method_id": card["payment_method_id"],
    }


__all__ = [
    "CURRENCY",
    "DEFAULT_MANDATE_CENTS",
    "MERCHANT_ID",
    "TOKEN_TTL_S",
    "WALLET_DB_ENV",
    "SetupOutcome",
    "WalletRefused",
    "WalletStore",
    "card_summary",
    "complete_setup",
    "complete_setup_session",
    "issue_payment_token",
    "redeem_payment_token",
    "remove_card",
    "reset_wallet",
    "run_payment",
    "set_mandate",
    "start_setup",
    "store",
    "wallet_unavailable",
]
