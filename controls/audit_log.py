"""Append-only, SHA-256 hash-chained audit log (one JSON object per line).

Each entry stores the hash of the previous entry, so editing, deleting or reordering any
line breaks the chain and ``verify`` reports the first bad ``seq``. The head of the chain
(last ``seq`` and ``hash``) is also written to a ``<file>.head`` anchor after every append,
so removing lines from the end, or emptying the file, is detected too. ``append`` refuses to
extend a log whose tail does not match that anchor, so an ordinary write never re-anchors the
head to a tampered chain. The one exception is the state a crash can leave behind: a writer
that died after fsyncing its line but before replacing the head leaves exactly one valid,
correctly chained entry past the anchored head. The next append rolls the head forward over
that entry and audits ``head_recovered`` (``from_seq``/``to_seq``); anything else (two or more
entries past the head, a different hash at the head ``seq``, a shorter log) is refused.
``data`` is passed through
``redact`` before hashing, so no raw PII ever reaches the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

try:  # POSIX advisory file locks; the lab runs on macOS/Linux/containers.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback: in-process lock only
    fcntl = None  # type: ignore[assignment]

from pydantic import BaseModel, ConfigDict, ValidationError

from controls.redaction import redact

GENESIS_HASH = "0" * 64


class AuditChainError(ValueError):
    """The on-disk log does not end at the anchored head: refuse to extend (or re-anchor) it."""


class AuditEntry(BaseModel):
    # Unknown fields are rejected: a forged key on disk must not be silently dropped.
    model_config = ConfigDict(extra="forbid")

    seq: int
    ts: str
    run_id: str
    actor: str
    event: str
    data: dict
    prev_hash: str
    hash: str


class VerifyResult(BaseModel):
    ok: bool
    entries: int
    first_bad_seq: int | None
    reason: str | None


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _compute_hash(entry_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(entry_without_hash).encode("utf-8")).hexdigest()


@contextmanager
def _file_lock(fh: IO[str], *, shared: bool = False) -> Iterator[None]:
    """Advisory lock on ``fh``: exclusive for appends, shared for reads."""
    if fcntl is None:
        yield
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    """Make a rename or a new file in ``path`` durable (POSIX); best effort elsewhere."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - e.g. platforms without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _check_chain(lines: list[str]) -> tuple[str, tuple[int, str] | None]:
    """Walk the chain: (hash of the last entry, None) or (prev hash, (first bad seq, reason))."""
    prev_hash = GENESIS_HASH
    for expected_seq, line in enumerate(lines):
        try:
            entry = AuditEntry.model_validate_json(line)
        except ValidationError:
            return prev_hash, (expected_seq, "unparseable entry")
        # The hash covers the parsed model; the raw line must be exactly its canonical
        # form, so extra keys or values the parser would coerce ("1" -> 1) are caught.
        if line.rstrip("\n") != _canonical(entry.model_dump()):
            return prev_hash, (expected_seq, "entry is not in canonical form: line was modified")
        if entry.seq != expected_seq:
            return prev_hash, (expected_seq, f"seq gap: expected {expected_seq}, found {entry.seq}")
        if entry.prev_hash != prev_hash:
            return prev_hash, (expected_seq, "prev_hash does not match previous entry hash")
        body = entry.model_dump(exclude={"hash"})
        if _compute_hash(body) != entry.hash:
            return prev_hash, (expected_seq, "hash mismatch: entry content was modified")
        prev_hash = entry.hash
    return prev_hash, None


def _last_line(path: Path) -> str | None:
    """Last non-empty line of ``path``, read from the end (no full scan)."""
    if not path.exists():
        return None
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        buf = b""
        pos = end
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            stripped = buf.rstrip(b"\r\n")
            if b"\n" in stripped:
                return stripped.rsplit(b"\n", 1)[1].decode("utf-8")
        stripped = buf.rstrip(b"\r\n")
        return stripped.decode("utf-8") if stripped.strip() else None


class AuditLog:
    """Hash-chained JSONL log.

    Several ``AuditLog`` instances (or processes, e.g. the CLI and the API) may append to the
    same file: each append takes an exclusive OS file lock and re-reads the tail under it, so
    ``seq`` and ``prev_hash`` always continue the chain that is actually on disk. ``read`` and
    ``verify`` take a shared lock, so they never see a line without its head anchor.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")
        self._lock = threading.Lock()
        self._next_seq = 0
        self._prev_hash = GENESIS_HASH
        self._restore_tail()

    def _restore_tail(self) -> None:
        last = _last_line(self.path)
        if last is None or not last.strip():
            self._next_seq, self._prev_hash = 0, GENESIS_HASH
            return
        try:
            tail = AuditEntry.model_validate_json(last)
        except ValidationError as exc:
            raise ValueError(f"audit log tail is corrupt, refusing to extend: {self.path}") from exc
        self._next_seq = tail.seq + 1
        self._prev_hash = tail.hash

    def append(self, run_id: str, actor: str, event: str, data: dict) -> AuditEntry:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh, _file_lock(fh):
                # Another writer may have appended since we last looked: continue its tail.
                self._restore_tail()
                # ...but only if that tail is the anchored head. Otherwise the file was cut,
                # emptied or its last entries rewritten, and appending would re-anchor the head
                # to the tampered chain and make ``verify`` pass again. The one state a crash
                # can leave (one valid entry past the head) is rolled forward and audited.
                recovered_from = self._check_tail_against_head()
                if recovered_from is not None:
                    self._write_entry(
                        fh,
                        "audit",
                        "audit_log",
                        "head_recovered",
                        {"from_seq": recovered_from, "to_seq": self._next_seq - 1},
                    )
                entry = self._write_entry(fh, run_id, actor, event, data)
            return entry

    def _write_entry(
        self, fh: IO[str], run_id: str, actor: str, event: str, data: dict
    ) -> AuditEntry:
        """Write, fsync and anchor one entry; called under the file lock."""
        body: dict[str, Any] = {
            "seq": self._next_seq,
            "ts": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "actor": actor,
            "event": event,
            "data": redact(data),
            "prev_hash": self._prev_hash,
        }
        entry = AuditEntry(**body, hash=_compute_hash(body))
        fh.write(_canonical(entry.model_dump()) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        self._write_head(entry)
        self._next_seq = entry.seq + 1
        self._prev_hash = entry.hash
        return entry

    def _check_tail_against_head(self) -> int | None:
        """Raise ``AuditChainError`` unless the restored tail equals the head anchor.

        Called under the file lock, after ``_restore_tail``. An empty file is a valid start only
        when no head exists; a non-empty file needs a head with the same ``seq`` and ``hash``.
        Returns None when they match. When the log holds exactly one entry past the head, the
        whole chain verifies and the entry at the head ``seq`` carries the anchored hash, that
        is the trace of a writer that crashed between fsyncing its line and replacing the head:
        the head is rolled forward to the tail and the old head ``seq`` is returned.
        """
        head = self._read_head()
        tail_seq = self._next_seq - 1
        if head is None:
            if tail_seq == -1:
                return None
            reason = "head anchor missing for a non-empty log"
        elif head.get("seq") == tail_seq and head.get("hash") == self._prev_hash and tail_seq >= 0:
            return None
        else:
            head_seq = head.get("seq")
            if self._crash_recoverable(head, tail_seq):
                assert isinstance(head_seq, int)
                self._write_head_raw(tail_seq, self._prev_hash)
                return head_seq
            reason = f"log tail (seq {tail_seq}) does not match the anchored head (seq {head_seq})"
        raise AuditChainError(f"{reason}; refusing to extend {self.path}")

    def _crash_recoverable(self, head: dict[str, Any], tail_seq: int) -> bool:
        """True only for one valid, chained entry right after an intact anchored head."""
        head_seq, head_hash = head.get("seq"), head.get("hash")
        if not isinstance(head_seq, int) or isinstance(head_seq, bool):
            return False
        if head_seq < 0 or head_seq != tail_seq - 1:
            return False
        with self.path.open("r", encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        last_hash, bad = _check_chain(lines)
        if bad is not None or len(lines) != tail_seq + 1 or last_hash != self._prev_hash:
            return False
        anchored = AuditEntry.model_validate_json(lines[head_seq])
        return anchored.hash == head_hash

    def _write_head(self, entry: AuditEntry) -> None:
        self._write_head_raw(entry.seq, entry.hash)

    def _write_head_raw(self, seq: int, entry_hash: str) -> None:
        """Atomically and durably record the chain head; called under the file lock.

        The tmp file is fsynced before the rename and the directory after it, so a crash
        leaves either the old head or the new one on disk, never an empty or partial file.
        """
        tmp = self.head_path.with_name(self.head_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(_canonical({"seq": seq, "hash": entry_hash}))
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(self.head_path.parent)
        os.replace(tmp, self.head_path)
        _fsync_dir(self.head_path.parent)

    def _read_head(self) -> dict[str, Any] | None:
        if not self.head_path.exists():
            return None
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return head if isinstance(head, dict) else {}

    def _snapshot(self) -> tuple[list[str], dict[str, Any] | None]:
        """Lines and head anchor read together under a shared file lock.

        ``append`` writes the line and then the head while holding the exclusive lock, so a
        reader that takes the shared lock never sees a new line without its head (or the
        reverse) and an intact log is never reported as tampered mid-append.
        """
        try:
            fh = self.path.open("r", encoding="utf-8")
        except FileNotFoundError:
            # No file means no append in progress (append creates it before locking).
            return [], self._read_head()
        with fh, _file_lock(fh, shared=True):
            lines = [line for line in fh if line.strip()]
            return lines, self._read_head()

    def read(self, run_id: str | None = None) -> list[AuditEntry]:
        lines, _ = self._snapshot()
        entries = [AuditEntry.model_validate_json(line) for line in lines]
        if run_id is not None:
            entries = [e for e in entries if e.run_id == run_id]
        return entries

    def verify(self) -> VerifyResult:
        lines, head = self._snapshot()
        prev_hash, bad = _check_chain(lines)
        if bad is not None:
            seq, reason = bad
            return VerifyResult(ok=False, entries=len(lines), first_bad_seq=seq, reason=reason)
        # The chain only proves internal consistency; the head anchor proves nothing is
        # missing from the end (any prefix of a valid chain is itself a valid chain).
        n = len(lines)
        if head is None:
            if n == 0:
                return VerifyResult(ok=True, entries=0, first_bad_seq=None, reason=None)
            return VerifyResult(ok=False, entries=n, first_bad_seq=n, reason="head anchor missing")
        head_seq = head.get("seq")
        if not isinstance(head_seq, int) or isinstance(head_seq, bool):
            return VerifyResult(ok=False, entries=n, first_bad_seq=n, reason="head anchor corrupt")
        if head_seq != n - 1 or head.get("hash") != prev_hash:
            if head_seq >= n:  # lines missing from the end: the first missing seq
                first_bad = n
            elif head_seq < n - 1:  # entries after the anchored head
                first_bad = head_seq + 1
            else:  # same length, different last entry
                first_bad = n - 1
            reason = f"log does not end at the anchored head (seq {head_seq})"
            if head_seq == n - 2 and AuditEntry.model_validate_json(
                lines[head_seq]
            ).hash == head.get("hash"):
                # Read-only: verify never re-anchors. The next append rolls the head forward.
                reason += (
                    "; one unanchored entry, e.g. an append interrupted before the head update"
                )
            return VerifyResult(ok=False, entries=n, first_bad_seq=first_bad, reason=reason)
        return VerifyResult(ok=True, entries=n, first_bad_seq=None, reason=None)
