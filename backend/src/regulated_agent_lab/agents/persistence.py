"""Agent-side persistence: LangGraph checkpoints and run history in one SQLite file.

``LAB_DB_PATH`` (default ``backend/var/lab.db``) holds two things:

- the LangGraph checkpoints (``checkpoints``/``writes`` tables of the official
  ``AsyncSqliteSaver``), so a run paused at the approval interrupt survives an API restart.
  The saver uses ``agents.graph.checkpoint_serde``: the same restricted msgpack allowlist as
  the in-memory saver, so a checkpoint row can only revive our own state models.
- ``runs``: one summary row per run for ``GET /runs``. The request is stored only after
  ``controls.redaction.redact``; amounts are integer cents. ``settlement`` mirrors what the
  card processor reported after the capture (``confirmed``, ``refunded``, ``disputed``,
  ``failed``; ``pending`` until it reports; NULL without a processor payment). It is a copy:
  the processor events themselves live in the payments database. ``requester`` is the
  authenticated user who created the run (NULL with API auth disabled), for segregation of
  duties (``REQUIRE_SEPARATE_APPROVER``) and the history.

The payments institution keeps its ledger in its own file (``PAYMENTS_DB_PATH``), never here.

Swapping in Postgres later means replacing the two factories in this module: the official
``AsyncPostgresSaver`` (``langgraph-checkpoint-postgres``) takes the same ``serde=`` argument,
and ``RunStore`` is a small class whose SQL is portable (``ON CONFLICT``, ``ORDER BY``).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from regulated_agent_lab import paths
from regulated_agent_lab.agents.graph import checkpoint_serde
from regulated_agent_lab.controls.redaction import redact

Settlement = Literal["confirmed", "refunded", "disputed", "failed", "pending"]
SETTLEMENTS: tuple[str, ...] = ("confirmed", "refunded", "disputed", "failed", "pending")

LAB_DB_ENV = "LAB_DB_PATH"
DEFAULT_LAB_DB = str(paths.VAR_DIR / "lab.db")
# The request is user text: bound what is kept even after redaction (the API caps it at 2000).
REQUEST_MAX_CHARS = 500
# Wait this long for another connection's write lock instead of failing at once.
BUSY_TIMEOUT_S = 30.0


def lab_db_path() -> Path:
    return Path(os.environ.get(LAB_DB_ENV) or DEFAULT_LAB_DB)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


async def _connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path), timeout=BUSY_TIMEOUT_S)
    # WAL: readers never block the writer; the saver and the run store share the file.
    await conn.execute("PRAGMA journal_mode=WAL")
    return conn


@asynccontextmanager
async def open_checkpointer(path: Path | str) -> AsyncIterator[AsyncSqliteSaver]:
    """A persistent checkpointer with the restricted serializer; closed on exit."""
    conn = await _connect(Path(path))
    try:
        saver = AsyncSqliteSaver(conn, serde=checkpoint_serde())
        await saver.setup()
        yield saver
    finally:
        await conn.close()


class RunSummary(BaseModel):
    """One row of ``GET /runs``: no raw user text, integer cents."""

    run_id: str
    created_at: str
    updated_at: str
    request_redacted: str
    status: str
    total_cents: int | None = None
    currency: str | None = None
    payment_id: str | None = None
    flags: list[str] = []
    settlement: Settlement | None = None
    requester: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    request_redacted TEXT NOT NULL,
    status TEXT NOT NULL,
    total_cents INTEGER CHECK (total_cents IS NULL OR typeof(total_cents) = 'integer'),
    currency TEXT,
    payment_id TEXT,
    flags TEXT NOT NULL DEFAULT '[]',
    settlement TEXT,
    requester TEXT
);
CREATE INDEX IF NOT EXISTS runs_created_at ON runs (created_at DESC);
"""

_COLUMNS = (
    "run_id, created_at, updated_at, request_redacted, status, total_cents, currency, "
    "payment_id, flags, settlement, requester"
)


def redact_request(message: str) -> str:
    text = redact(message or "")
    return text if len(text) <= REQUEST_MAX_CHARS else text[: REQUEST_MAX_CHARS - 1] + "…"


class RunStore:
    """Run history (``runs`` table). One connection; aiosqlite serialises its statements."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path | str) -> AsyncIterator[RunStore]:
        conn = await _connect(Path(path))
        try:
            await conn.executescript(_SCHEMA)
            # A lab.db from before settlements existed: add the column in place.
            async with conn.execute("PRAGMA table_info(runs)") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            for column in ("settlement", "requester"):
                if column not in columns:
                    await conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
            await conn.commit()
            yield cls(conn)
        finally:
            await conn.close()

    async def start(self, run_id: str, message: str, requester: str | None = None) -> None:
        now = _now()
        await self._conn.execute(
            "INSERT INTO runs (run_id, created_at, updated_at, request_redacted, status, "
            "requester) VALUES (?, ?, ?, ?, 'running', ?) ON CONFLICT (run_id) DO NOTHING",
            (run_id, now, now, redact_request(message), requester),
        )
        await self._conn.commit()

    async def update(
        self,
        run_id: str,
        *,
        status: str,
        total_cents: int | None = None,
        currency: str | None = None,
        payment_id: str | None = None,
        flags: list[str] | None = None,
        initial_settlement: Settlement | None = None,
    ) -> None:
        """Mirror the run's state. ``initial_settlement`` only fills an empty settlement: a
        processor event that arrived first (webhooks can beat the graph) is never overwritten."""
        if initial_settlement is not None and initial_settlement not in SETTLEMENTS:
            raise ValueError("unknown settlement")
        if total_cents is not None and (
            isinstance(total_cents, bool) or not isinstance(total_cents, int)
        ):
            raise TypeError("total_cents must be an integer number of cents")
        await self._conn.execute(
            "UPDATE runs SET updated_at = ?, status = ?, total_cents = ?, currency = ?, "
            "payment_id = ?, flags = ?, settlement = COALESCE(settlement, ?) WHERE run_id = ?",
            (
                _now(),
                status,
                total_cents,
                currency,
                payment_id,
                json.dumps(list(flags or [])),
                initial_settlement,
                run_id,
            ),
        )
        await self._conn.commit()

    async def set_settlement(self, run_id: str, settlement: Settlement) -> str | None:
        """Record what the processor reported; returns the previous settlement."""
        if settlement not in SETTLEMENTS:
            raise ValueError("unknown settlement")
        async with self._conn.execute(
            "SELECT settlement FROM runs WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        await self._conn.execute(
            "UPDATE runs SET updated_at = ?, settlement = ? WHERE run_id = ?",
            (_now(), settlement, run_id),
        )
        await self._conn.commit()
        return row[0] if row is not None else None

    async def confirm_settlement(self, run_id: str) -> bool:
        """``confirmed`` unless the processor already reported something stronger (a refund or a
        dispute that arrived first is never overwritten). True when it changed."""
        cur = await self._conn.execute(
            "UPDATE runs SET updated_at = ?, settlement = 'confirmed' WHERE run_id = ? "
            "AND (settlement IS NULL OR settlement = 'pending')",
            (_now(), run_id),
        )
        await self._conn.commit()
        return cur.rowcount == 1

    @staticmethod
    def _row(row: Any) -> RunSummary:
        values = dict(zip([c.strip() for c in _COLUMNS.split(",")], row, strict=True))
        values["flags"] = json.loads(values["flags"] or "[]")
        return RunSummary.model_validate(values)

    async def recent(self, limit: int = 20) -> list[RunSummary]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [self._row(r) for r in await cur.fetchall()]

    async def get(self, run_id: str) -> RunSummary | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row(row) if row is not None else None


__all__ = [
    "DEFAULT_LAB_DB",
    "LAB_DB_ENV",
    "SETTLEMENTS",
    "RunStore",
    "RunSummary",
    "Settlement",
    "lab_db_path",
    "open_checkpointer",
    "redact_request",
]
