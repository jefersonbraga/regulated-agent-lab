from pathlib import Path

from agents.config import LLMConfig, load_llm_config

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "llm.yaml"


def test_loads_default_provider_vllm(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = load_llm_config(DEFAULT_CONFIG)
    assert isinstance(cfg, LLMConfig)
    assert cfg.provider == "vllm"
    assert cfg.model == "nvidia/Qwen3.6-35B-A3B-NVFP4"
    assert cfg.temperature == 0.0
    assert cfg.timeout_s == 60
    assert cfg.max_retries == 1
    assert cfg.thinking is False


def test_env_overrides_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    cfg = load_llm_config(DEFAULT_CONFIG)
    assert cfg.provider == "mock"


def test_env_overrides_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    cfg = load_llm_config(DEFAULT_CONFIG)
    assert (cfg.provider, cfg.model) == ("anthropic", "claude-sonnet-5-5")
    monkeypatch.setenv("LLM_MODEL", "  ")
    assert load_llm_config(DEFAULT_CONFIG).model == "nvidia/Qwen3.6-35B-A3B-NVFP4"


def test_base_url_env_expanded(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "http://dgx-spark.local:8000/v1")
    cfg = load_llm_config(DEFAULT_CONFIG)
    assert cfg.base_url == "http://dgx-spark.local:8000/v1"
