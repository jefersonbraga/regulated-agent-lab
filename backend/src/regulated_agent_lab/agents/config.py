"""LLM provider configuration loaded from ``backend/config/llm.yaml``.

Switching provider is a one-line change in the YAML file. Tests force the mock
provider through the ``LLM_PROVIDER`` environment variable; ``LLM_MODEL`` overrides the model
the same way (a hosted deploy picks a cloud provider and its model without editing the file).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from regulated_agent_lab import paths

_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LLMConfig(BaseModel):
    provider: Literal["vllm", "anthropic", "openai", "mock"]
    model: str
    base_url: str | None
    temperature: float = 0.0
    # Per-request deadline and retries for real providers: a stalled endpoint ends the run as
    # failed (llm_unavailable) instead of holding it open for the SDK default of minutes.
    timeout_s: float = Field(60.0, gt=0)
    max_retries: int = Field(1, ge=0, le=5)
    # vLLM only: let a reasoning model think before its structured answer. Off by default: the
    # answers are typed extractions, and thinking made each call ~20x slower with the same
    # results on the eval scenarios.
    thinking: bool = False


def _expand_env(value: str | None) -> str | None:
    """Expand ``${VAR}`` from the environment; unset variables become empty.

    Returns ``None`` when the expanded value is empty.
    """
    if value is None:
        return None
    expanded = _ENV_VAR.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return expanded or None


def load_llm_config(path: Path | None = None) -> LLMConfig:
    path = paths.llm_config_path() if path is None else path
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    provider_override = os.environ.get("LLM_PROVIDER")
    if provider_override:
        raw["provider"] = provider_override
    model_override = (os.environ.get("LLM_MODEL") or "").strip()
    if model_override:
        raw["model"] = model_override
    raw["base_url"] = _expand_env(raw.get("base_url"))
    return LLMConfig.model_validate(raw)
