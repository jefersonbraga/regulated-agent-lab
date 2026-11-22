import json

import pytest

import regulated_agent_lab.agents.cli
from regulated_agent_lab import paths

FIXTURES = paths.MOCK_FIXTURES


def _capture_run(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_run(message, fixtures, approver, audit_path):
        seen["fixtures"] = fixtures
        return 0

    monkeypatch.setattr(regulated_agent_lab.agents.cli, "run", fake_run)
    monkeypatch.setattr(regulated_agent_lab.agents.cli, "load_dotenv", lambda: None)
    return seen


def test_mock_cli_defaults_to_roundtrip_fixtures(monkeypatch):
    # Same fallback as the API: the mock answers from agents/fixtures/mock_roundtrip.json.
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("MOCK_FIXTURES", raising=False)
    seen = _capture_run(monkeypatch)

    assert regulated_agent_lab.agents.cli.main(["Train from Zurich to Bern tomorrow"]) == 0

    assert seen["fixtures"] == json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert "Intent" in seen["fixtures"]


def test_cli_explicit_fixtures_win(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    custom = tmp_path / "f.json"
    custom.write_text(json.dumps({"Intent": {"kind": "unsupported"}}), encoding="utf-8")
    seen = _capture_run(monkeypatch)

    regulated_agent_lab.agents.cli.main(["hi", "--mock-fixtures", str(custom)])

    assert seen["fixtures"] == {"Intent": {"kind": "unsupported"}}


def test_cli_real_provider_gets_no_fixtures(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    monkeypatch.delenv("MOCK_FIXTURES", raising=False)
    seen = _capture_run(monkeypatch)

    regulated_agent_lab.agents.cli.main(["hi"])

    assert seen["fixtures"] is None


def test_cli_without_token_secret_prints_hint_and_fails_closed(monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("AGENT_TOKEN_SECRET", raising=False)
    called = {"run": False}

    async def fake_run(*args):
        called["run"] = True
        return 0

    monkeypatch.setattr(regulated_agent_lab.agents.cli, "run", fake_run)
    monkeypatch.setattr(regulated_agent_lab.agents.cli, "load_dotenv", lambda: None)

    assert regulated_agent_lab.agents.cli.main(["hi"]) == 2

    assert called["run"] is False
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "AGENT_TOKEN_SECRET is not set" in err[0]
    assert "cp .env.example .env" in err[0]


def test_cli_vllm_without_base_url_fails_before_any_request(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(regulated_agent_lab.agents.cli, "load_dotenv", lambda: None)

    assert regulated_agent_lab.agents.cli.main(["Train from Zurich to Bern"]) == 2

    err = capsys.readouterr().err
    assert "VLLM_BASE_URL" in err
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_cli_no_answer_at_prompt_rejects(monkeypatch, capfd, tmp_path, exc):
    # stdin closed or not interactive: the [y/N] default applies, the run ends 'rejected'.
    # capfd, not capsys: the stdio MCP subprocesses need a real stderr file descriptor.
    from regulated_agent_lab.controls.audit_log import AuditLog

    def no_answer(prompt=""):
        raise exc

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("MOCK_FIXTURES", raising=False)
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(regulated_agent_lab.agents.cli, "load_dotenv", lambda: None)
    monkeypatch.setattr("builtins.input", no_answer)

    assert regulated_agent_lab.agents.cli.main(["Train from Zurich to Bern"]) == 0

    out = capfd.readouterr().out
    assert "status   rejected" in out
    assert "verify=" in out
    log = AuditLog(audit_path)
    (decision,) = [e for e in log.read() if e.event == "approval_decision"]
    assert decision.data["approved"] is False
    assert log.verify().ok
