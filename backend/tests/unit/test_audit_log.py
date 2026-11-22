import json

import pytest

from regulated_agent_lab.controls.audit_log import AuditChainError, AuditLog


def _fill(path, n=3):
    log = AuditLog(path)
    for i in range(n):
        log.append("run-1", "planner", f"event_{i}", {"i": i})
    return log


def _lines(path):
    return path.read_text().splitlines()


def test_chain_verifies_after_appends(tmp_path):
    log = _fill(tmp_path / "audit.jsonl")
    result = log.verify()
    assert result.ok is True
    assert result.entries == 3
    assert result.first_bad_seq is None
    assert result.reason is None
    entries = log.read()
    assert [e.seq for e in entries] == [0, 1, 2]
    assert entries[0].prev_hash == "0" * 64
    assert entries[1].prev_hash == entries[0].hash


def test_edit_field_breaks_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    lines = _lines(path)
    row = json.loads(lines[1])
    row["data"] = {"i": 999}
    lines[1] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n")
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 1


def test_deleted_line_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    lines = _lines(path)
    del lines[1]
    path.write_text("\n".join(lines) + "\n")
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 1


def test_reordered_lines_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    lines = _lines(path)
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n")
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 1


def test_reopen_continues_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    log2 = AuditLog(path)
    entry = log2.append("run-2", "booking", "hold_seat", {"seat": "12A"})
    assert entry.seq == 3
    result = log2.verify()
    assert result.ok is True
    assert result.entries == 4
    assert [e.seq for e in log2.read("run-2")] == [3]


def test_pii_redacted_on_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(
        "run-1",
        "booking",
        "passenger",
        {"passenger": {"full_name": "Anna Muster", "email": "a@x.ch"}, "note": "mail a@x.ch"},
    )
    raw = path.read_text()
    assert "a@x.ch" not in raw
    assert "Anna Muster" not in raw
    assert log.verify().ok is True


def _rewrite_line(path, index, mutate):
    lines = _lines(path)
    row = json.loads(lines[index])
    mutate(row)
    lines[index] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")


def test_injected_field_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    _rewrite_line(path, 1, lambda row: row.update(override={"approved": True}))
    result = AuditLog(path).verify()
    assert result.ok is False
    assert result.first_bad_seq == 1


def test_type_coerced_field_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    _rewrite_line(path, 1, lambda row: row.update(seq="1"))
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 1


def test_two_writers_on_one_file_keep_one_chain(tmp_path):
    # e.g. the CLI and the API both appending to var/audit.jsonl
    path = tmp_path / "audit.jsonl"
    a = AuditLog(path)
    a.append("run-a", "planner", "e0", {})
    b = AuditLog(path)
    b.append("run-b", "planner", "e1", {})
    a.append("run-a", "planner", "e2", {})
    b.append("run-b", "planner", "e3", {})

    result = AuditLog(path).verify()
    assert result.ok is True, result.reason
    assert [e.seq for e in AuditLog(path).read()] == [0, 1, 2, 3]


def test_concurrent_processes_keep_one_chain(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "audit.jsonl"
    repo = Path(__file__).resolve().parents[2]  # backend/
    code = (
        "import sys; from pathlib import Path\n"
        "from regulated_agent_lab.controls.audit_log import AuditLog\n"
        "log = AuditLog(Path(sys.argv[1]))\n"
        "for i in range(25): log.append(sys.argv[2], 'w', f'e{i}', {'i': i})\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(path), f"run-{n}"], cwd=repo)
        for n in range(3)
    ]
    assert all(p.wait(timeout=60) == 0 for p in procs)

    result = AuditLog(path).verify()
    assert result.ok is True, result.reason
    assert result.entries == 75


def test_truncated_tail_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    lines = _lines(path)
    path.write_text("\n".join(lines[:-1]) + "\n")
    result = AuditLog(path).verify()
    assert result.ok is False
    assert result.first_bad_seq == 2


def test_emptied_file_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    path.write_text("")
    result = AuditLog(path).verify()
    assert result.ok is False
    assert result.first_bad_seq == 0


def test_removed_head_anchor_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    log.head_path.unlink()
    result = log.verify()
    assert result.ok is False


def _assert_append_refused_and_tamper_still_detected(path):
    with pytest.raises(AuditChainError):
        AuditLog(path).append("run-2", "booking", "after_tamper", {})
    result = AuditLog(path).verify()
    assert result.ok is False


def test_append_after_truncation_does_not_reanchor(tmp_path):
    # Attacker can write audit.jsonl but not audit.jsonl.head: the next ordinary write must not
    # launder the truncation by re-anchoring the head to the shortened chain.
    path = tmp_path / "audit.jsonl"
    _fill(path, n=5)
    path.write_text("\n".join(_lines(path)[:-2]) + "\n")
    _assert_append_refused_and_tamper_still_detected(path)


def test_append_after_emptying_does_not_reanchor(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    path.write_text("")
    _assert_append_refused_and_tamper_still_detected(path)


def test_append_after_rewritten_tail_does_not_reanchor(tmp_path):
    # Last entry replaced with a self-consistent one (recomputed SHA-256).
    import hashlib

    path = tmp_path / "audit.jsonl"
    _fill(path)
    lines = _lines(path)
    row = json.loads(lines[-1])
    row["data"] = {"i": 999}
    body = {k: v for k, v in row.items() if k != "hash"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    row["hash"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    lines[-1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    _assert_append_refused_and_tamper_still_detected(path)


def test_append_without_head_on_nonempty_log_refused(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    log.head_path.unlink()
    _assert_append_refused_and_tamper_still_detected(path)


def test_verify_during_append_sees_a_consistent_snapshot(tmp_path, monkeypatch):
    # An append writes the line, then the head anchor. A verify that runs in between must
    # wait for the append (file lock), not report tampering on an intact log.
    import threading

    path = tmp_path / "audit.jsonl"
    writer = AuditLog(path)
    writer.append("r", "a", "e", {"i": 0})
    reader = AuditLog(path)

    line_written = threading.Event()
    release = threading.Event()
    real_write_head = AuditLog._write_head

    def slow_write_head(self, entry):
        line_written.set()
        release.wait(5)
        real_write_head(self, entry)

    monkeypatch.setattr(AuditLog, "_write_head", slow_write_head)
    t = threading.Thread(target=writer.append, args=("r", "a", "e", {"i": 1}))
    t.start()
    assert line_written.wait(5)

    results = []
    v = threading.Thread(target=lambda: results.append((reader.verify(), len(reader.read()))))
    v.start()
    v.join(0.3)
    release.set()
    t.join(5)
    v.join(5)

    ((verified, read_count),) = results
    assert verified.ok, verified
    assert verified.entries == 2
    assert read_count == 2


_CRASH_BEFORE_HEAD = (
    "import os, sys; from pathlib import Path\n"
    "from regulated_agent_lab.controls.audit_log import AuditLog\n"
    "log = AuditLog(Path(sys.argv[1]))\n"
    "log.append('run-1', 'w', 'e0', {})\n"
    "def die(*a, **k): os._exit(9)\n"
    "os.replace = die  # crash after the line is fsynced, before the head is replaced\n"
    "log.append('run-1', 'w', 'e1', {})\n"
)


def test_crash_between_line_and_head_recovers_on_next_append(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "audit.jsonl"
    repo = Path(__file__).resolve().parents[2]  # backend/
    proc = subprocess.run([sys.executable, "-c", _CRASH_BEFORE_HEAD, str(path)], cwd=repo)
    assert proc.returncode == 9
    assert len(_lines(path)) == 2  # the line reached the disk, the head did not move
    assert json.loads(AuditLog(path).head_path.read_text())["seq"] == 0

    entry = AuditLog(path).append("run-2", "w", "after_crash", {})

    result = AuditLog(path).verify()
    assert result.ok is True, result.reason
    events = [(e.seq, e.event) for e in AuditLog(path).read()]
    assert events == [(0, "e0"), (1, "e1"), (2, "head_recovered"), (3, "after_crash")]
    recovered = AuditLog(path).read()[2]
    assert recovered.data == {"from_seq": 0, "to_seq": 1}
    assert entry.seq == 3


def _restore_head(path, head_text):
    AuditLog(path).head_path.write_text(head_text)


def test_head_more_than_one_entry_behind_is_refused(tmp_path):
    # A crash leaves at most one unanchored line: two valid lines past the head are not a crash.
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    head = log.head_path.read_text()
    log.append("run-1", "planner", "forged_1", {})
    log.append("run-1", "planner", "forged_2", {})
    _restore_head(path, head)
    _assert_append_refused_and_tamper_still_detected(path)


def test_unanchored_line_that_does_not_chain_is_refused(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    head = log.head_path.read_text()
    log.append("run-1", "planner", "extra", {})
    _restore_head(path, head)
    lines = _lines(path)
    row = json.loads(lines[-1])
    row["data"] = {"forged": True}  # hash no longer matches the content
    lines[-1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    _assert_append_refused_and_tamper_still_detected(path)


def test_unanchored_line_after_a_rewritten_head_entry_is_refused(tmp_path):
    # The head seq is one behind, but the entry at that seq is not the anchored one.
    import hashlib

    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    head = log.head_path.read_text()
    lines = _lines(path)
    row = json.loads(lines[-1])
    row["data"] = {"i": 999}
    body = {k: v for k, v in row.items() if k != "hash"}
    row["hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    lines[-1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    # Re-anchor a fresh log state to the rewritten entry, then add one valid line after it.
    AuditLog(path).head_path.write_text(json.dumps({"seq": row["seq"], "hash": row["hash"]}))
    AuditLog(path).append("run-1", "planner", "extra", {})
    _restore_head(path, head)
    _assert_append_refused_and_tamper_still_detected(path)


def test_verify_names_an_interrupted_append_but_does_not_reanchor(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _fill(path)
    head = log.head_path.read_text()
    log.append("run-1", "planner", "unanchored", {})
    _restore_head(path, head)

    result = AuditLog(path).verify()
    assert result.ok is False
    assert result.first_bad_seq == 3
    assert "interrupted" in result.reason
    assert AuditLog(path).head_path.read_text() == head
