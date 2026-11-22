"""Live evals are opt-in: plain `pytest` never calls the real model, even with VLLM_BASE_URL."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _collect(*extra: str) -> str:
    env = {**os.environ, "VLLM_BASE_URL": "http://127.0.0.1:9/v1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *extra],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout


def test_plain_run_deselects_live_evals_even_with_endpoint_set():
    out = _collect("evals")
    assert "test_scenario_live" not in out
    assert "test_scenario" in out  # the mock evals still run


def test_explicit_live_marker_selects_live_evals():
    out = _collect("-m", "live", "evals")
    assert "test_scenario_live" in out


def _yaml(tmp_path, provider: str, model: str) -> Path:
    path = tmp_path / "llm.yaml"
    path.write_text(f"provider: {provider}\nmodel: {model}\nbase_url: ${{VLLM_BASE_URL}}\n")
    return path


def test_live_config_uses_the_provider_named_in_the_yaml(tmp_path, monkeypatch):
    # The harness forces LLM_PROVIDER=mock for the mock evals; the live rows must still hit
    # the model configured in config/llm.yaml, not a hard-coded vLLM.
    from evals.conftest import _live_config

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    cfg = _live_config(_yaml(tmp_path, "anthropic", "claude-x"), user_provider=None)
    assert (cfg.provider, cfg.model) == ("anthropic", "claude-x")


def test_live_config_honours_an_explicit_provider_override(tmp_path, monkeypatch):
    from evals.conftest import _live_config

    monkeypatch.setenv("LIVE_LLM_PROVIDER", "openai")
    cfg = _live_config(_yaml(tmp_path, "vllm", "m"), user_provider="anthropic")
    assert cfg.provider == "openai"
    monkeypatch.delenv("LIVE_LLM_PROVIDER")
    cfg = _live_config(_yaml(tmp_path, "vllm", "m"), user_provider="anthropic")
    assert cfg.provider == "anthropic"  # the user's own LLM_PROVIDER, as everywhere else


def test_live_config_falls_back_to_vllm_for_a_mock_yaml(tmp_path):
    from evals.conftest import _live_config

    cfg = _live_config(_yaml(tmp_path, "mock", "m"), user_provider="mock")
    assert cfg.provider == "vllm"


def test_live_skip_reason_depends_on_the_provider(monkeypatch):
    from evals.conftest import _live_skip_reason
    from regulated_agent_lab.agents.config import LLMConfig

    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    vllm = LLMConfig(provider="vllm", model="m", base_url=None)
    assert "VLLM_BASE_URL" in _live_skip_reason(vllm)
    assert _live_skip_reason(vllm.model_copy(update={"base_url": "http://h/v1"})) is None
    anthropic = LLMConfig(provider="anthropic", model="m", base_url=None)
    assert "ANTHROPIC_API_KEY" in _live_skip_reason(anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert _live_skip_reason(anthropic) is None  # no VLLM_BASE_URL needed
