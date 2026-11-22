"""Pluggable LLM behind a tiny protocol: typed structured output only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from regulated_agent_lab import paths
from regulated_agent_lab.agents.config import LLMConfig

T = TypeVar("T", bound=BaseModel)

DEFAULT_MOCK_FIXTURES = paths.MOCK_FIXTURES


def load_mock_fixtures(provider: str, path: str | Path | None = None) -> dict | None:
    """MockLLM fixtures for ``provider == "mock"`` (``path`` or the round-trip default).

    Shared by the CLI and the API so both answer the same way on the mock path.
    """
    if provider != "mock":
        return None
    source = Path(path) if path else DEFAULT_MOCK_FIXTURES
    return json.loads(source.read_text(encoding="utf-8"))


class LLMConfigError(ValueError):
    """The LLM configuration is unsafe or incomplete: refuse to start."""


class LLMUnavailable(RuntimeError):
    """The model endpoint did not answer: unreachable, timed out, overloaded or 5xx.

    Raised instead of the SDK's own error so the graph can name the cause (``llm_unavailable``)
    without importing every provider SDK. The message never carries the prompt.
    """


def _transport_errors() -> tuple[type[BaseException], ...]:
    """Provider SDK errors that mean "no usable answer", not a bad answer."""
    errors: list[type[BaseException]] = []
    for module in ("openai", "anthropic"):
        try:
            sdk = __import__(module)
        except ImportError:
            continue
        for name in (
            "APIConnectionError",
            "APITimeoutError",
            "InternalServerError",
            "RateLimitError",
        ):
            if hasattr(sdk, name):
                errors.append(getattr(sdk, name))
    return tuple(errors)


class LLM(Protocol):
    async def extract(self, schema: type[T], system: str, user: str) -> T: ...


class MockLLM:
    """Returns fixtures keyed by schema class name. Bad fixtures raise ValidationError."""

    def __init__(self, fixtures: dict[str, dict]) -> None:
        self.fixtures = fixtures

    async def extract(self, schema: type[T], system: str, user: str) -> T:
        return schema.model_validate(self.fixtures[schema.__name__])


# JSON Schema keywords that bound a value. They are stripped from the schema the model sees.
_BOUND_KEYWORDS = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength"}
)


def unbounded_schema(schema: type[BaseModel]) -> dict:
    """``schema``'s JSON Schema without value bounds, for the model to fill.

    Servers with guided decoding (vLLM) enforce the schema token by token, so a bound such as
    ``passengers <= 9`` would make the model write an in-range value instead of what the user
    asked for (10000 people silently becomes 1). The model gets the shape only; the bounds are
    enforced afterwards by ``schema.model_validate``, in code, where a violation fails closed.
    """

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in _BOUND_KEYWORDS}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(schema.model_json_schema())


class ChatModelLLM:
    """Wraps a LangChain chat model; output is always validated against ``schema``."""

    def __init__(self, model: BaseChatModel) -> None:
        self.model = model
        self._transport_errors = _transport_errors()

    async def extract(self, schema: type[T], system: str, user: str) -> T:
        runnable = self.model.with_structured_output(unbounded_schema(schema), method="json_schema")
        try:
            result: Any = await runnable.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
        except self._transport_errors as exc:
            raise LLMUnavailable(type(exc).__name__) from exc
        return schema.model_validate(result)


def build_llm(cfg: LLMConfig, fixtures: dict | None = None) -> LLM:
    if cfg.provider == "mock":
        return MockLLM(fixtures or {})
    # ``base_url`` (``${VLLM_BASE_URL}`` in backend/config/llm.yaml) belongs to the on-prem vLLM
    # endpoint only. Cloud providers always use their SDK default (or their own
    # OPENAI_BASE_URL / ANTHROPIC_BASE_URL env), so switching `provider` is one line and never
    # sends a cloud API key to the vLLM host, nor a cloud key to vLLM.
    if cfg.provider == "vllm":
        # Fail closed: without a URL, ChatOpenAI falls back to OPENAI_BASE_URL or the public
        # cloud endpoint and would send the user's message (and VLLM_API_KEY) off-prem.
        if not cfg.base_url:
            raise LLMConfigError(
                "provider 'vllm' needs base_url: set VLLM_BASE_URL to the on-prem "
                "OpenAI-compatible endpoint (e.g. in .env), or choose another provider"
            )
        from langchain_openai import ChatOpenAI

        return ChatModelLLM(
            ChatOpenAI(
                base_url=cfg.base_url,
                model=cfg.model,
                api_key=os.environ.get("VLLM_API_KEY") or "EMPTY",
                temperature=cfg.temperature,
                timeout=cfg.timeout_s,
                max_retries=cfg.max_retries,
                # Chat-template switch understood by reasoning models served by vLLM; models
                # without a thinking mode ignore it.
                extra_body={"chat_template_kwargs": {"enable_thinking": cfg.thinking}},
            )
        )
    if cfg.provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatModelLLM(
            ChatOpenAI(
                model=cfg.model,
                temperature=cfg.temperature,
                timeout=cfg.timeout_s,
                max_retries=cfg.max_retries,
            )
        )
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatModelLLM(
            ChatAnthropic(
                model=cfg.model,
                temperature=cfg.temperature,
                timeout=cfg.timeout_s,
                max_retries=cfg.max_retries,
            )
        )
    raise ValueError(f"unknown provider: {cfg.provider}")


async def check_vllm_endpoint(cfg: LLMConfig, transport: Any = None) -> str | None:
    """Startup probe of the on-prem endpoint: ``None`` when it serves ``cfg.model``.

    Otherwise a one-line problem (unreachable, rejected key, model not served) so a wrong
    address, key or model name shows up when the API starts, not as a failed first run.
    Never raises, never sends a prompt; other providers are not probed.
    """
    if cfg.provider != "vllm" or not cfg.base_url:
        return None
    import httpx

    url = cfg.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {os.environ.get('VLLM_API_KEY') or 'EMPTY'}"}
    try:
        async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return f"vLLM endpoint {url} is unreachable ({type(exc).__name__})"
    if response.status_code in (401, 403):
        return f"vLLM endpoint {url} rejected VLLM_API_KEY (HTTP {response.status_code})"
    if response.status_code != 200:
        return f"vLLM endpoint {url} answered HTTP {response.status_code}"
    try:
        served = [m.get("id") for m in response.json().get("data", [])]
    except (ValueError, AttributeError):
        return f"vLLM endpoint {url} did not return a model list"
    if cfg.model not in served:
        return f"vLLM endpoint serves {served}, not the configured model {cfg.model!r}"
    return None
