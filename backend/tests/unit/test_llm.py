from datetime import date

import pytest
from pydantic import ValidationError

from regulated_agent_lab.agents.config import LLMConfig
from regulated_agent_lab.agents.llm import (
    ChatModelLLM,
    LLMConfigError,
    LLMUnavailable,
    MockLLM,
    build_llm,
    unbounded_schema,
)
from regulated_agent_lab.agents.state import Intent, TripRequest

INTENT_FIXTURE = {
    "Intent": {
        "kind": "book_trip",
        "trip": {
            "origin": "Zurich HB",
            "destination": "Bern",
            "depart_date": "2026-11-22",
            "return_date": "2026-11-23",
            "passengers": 1,
        },
    }
}


def test_trip_request_rejects_return_before_depart():
    with pytest.raises(ValidationError):
        TripRequest(
            origin="Zurich HB",
            destination="Bern",
            depart_date=date(2026, 11, 23),
            return_date=date(2026, 11, 22),
        )


async def test_mock_llm_returns_typed_intent():
    llm = MockLLM(INTENT_FIXTURE)
    intent = await llm.extract(Intent, system="sys", user="Train from Zurich to Bern")
    assert isinstance(intent, Intent)
    assert intent.kind == "book_trip"
    assert intent.trip is not None
    assert intent.trip.depart_date == date(2026, 11, 22)
    assert intent.trip.return_date == date(2026, 11, 23)


def test_build_llm_mock_from_config():
    cfg = LLMConfig(provider="mock", model="mock", base_url=None)
    llm = build_llm(cfg, fixtures=INTENT_FIXTURE)
    assert isinstance(llm, MockLLM)


def test_build_llm_vllm_uses_openai_compatible_base_url(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LLMConfig(
        provider="vllm",
        model="Qwen3.6-35B-A3B-NVFP4",
        base_url="http://vllm.example.local:8000/v1",
    )
    llm = build_llm(cfg)
    assert isinstance(llm, ChatModelLLM)
    assert llm.model.openai_api_base == "http://vllm.example.local:8000/v1"
    assert llm.model.model_name == "Qwen3.6-35B-A3B-NVFP4"
    # A stalled endpoint must fail the run quickly, not after the SDK default of minutes.
    assert llm.model.request_timeout == 60.0
    assert llm.model.max_retries == 1
    # Typed extractions do not need the model's thinking mode (same answers, ~20x faster).
    assert llm.model.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("passengers", [0, 10, 10000])
def test_trip_request_bounds_passenger_count(passengers):
    # The LLM-extracted count drives one hold_seat call per passenger per leg, so code caps it.
    with pytest.raises(ValidationError):
        TripRequest(
            origin="Zurich HB",
            destination="Bern",
            depart_date=date(2026, 12, 1),
            passengers=passengers,
        )


def test_trip_request_accepts_max_passengers():
    from regulated_agent_lab.agents.state import MAX_PASSENGERS

    trip = TripRequest(
        origin="Zurich HB", destination="Bern", depart_date=date(2026, 12, 1), passengers=9
    )
    assert trip.passengers == MAX_PASSENGERS == 9


def _schema_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _schema_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_keys(value)


def test_model_sees_intent_schema_without_bounds():
    # Guided decoding enforces the schema it is given: with ``maximum: 9`` a request for
    # 10000 people would come back as an in-range count and book silently.
    keys = set(_schema_keys(unbounded_schema(Intent)))
    assert not keys & {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
    assert "passengers" in keys
    assert "maximum" in set(_schema_keys(Intent.model_json_schema()))


class _FakeStructuredModel:
    """Chat-model stand-in: records the schema it was given, answers with a fixed dict."""

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.schema = None

    def with_structured_output(self, schema, method):
        self.schema = schema
        return self

    async def ainvoke(self, messages):
        return self.answer


async def test_chat_model_out_of_range_answer_fails_closed_in_code():
    trip = {**INTENT_FIXTURE["Intent"]["trip"], "passengers": 10000}
    model = _FakeStructuredModel({"kind": "book_trip", "trip": trip})
    with pytest.raises(ValidationError):
        await ChatModelLLM(model).extract(Intent, "system", "10000 people")
    assert model.schema == unbounded_schema(Intent)


class _DownModel(_FakeStructuredModel):
    def __init__(self, error: Exception) -> None:
        super().__init__({})
        self.error = error

    async def ainvoke(self, messages):
        raise self.error


async def test_chat_model_transport_error_becomes_llm_unavailable():
    import httpx
    import openai

    request = httpx.Request("POST", "http://vllm.example.local:8000/v1/chat/completions")
    with pytest.raises(LLMUnavailable, match="APITimeoutError"):
        await ChatModelLLM(_DownModel(openai.APITimeoutError(request))).extract(
            Intent, "system", "user"
        )


async def test_chat_model_other_errors_are_not_relabelled():
    # Only "no answer" errors are LLMUnavailable; a bug stays visible as itself.
    with pytest.raises(KeyError):
        await ChatModelLLM(_DownModel(KeyError("boom"))).extract(Intent, "system", "user")


VLLM_URL = "http://dgx-spark.local:8000/v1"


def test_openai_switch_does_not_use_vllm_url(monkeypatch, tmp_path):
    # The documented one-line switch: only `provider` (and model) change, VLLM_BASE_URL stays set.
    from regulated_agent_lab.agents.config import load_llm_config

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", VLLM_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-cloud-key")
    cfg_file = tmp_path / "llm.yaml"
    cfg_file.write_text(
        "provider: openai\nmodel: gpt-test\nbase_url: ${VLLM_BASE_URL}\n", encoding="utf-8"
    )
    llm = build_llm(load_llm_config(cfg_file))
    assert isinstance(llm, ChatModelLLM)
    assert llm.model.openai_api_base != VLLM_URL
    assert llm.model.openai_api_key.get_secret_value() == "sk-cloud-key"


def test_anthropic_switch_does_not_use_vllm_url(monkeypatch, tmp_path):
    from regulated_agent_lab.agents.config import load_llm_config

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", VLLM_URL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg_file = tmp_path / "llm.yaml"
    cfg_file.write_text(
        "provider: anthropic\nmodel: claude-test\nbase_url: ${VLLM_BASE_URL}\n", encoding="utf-8"
    )
    llm = build_llm(load_llm_config(cfg_file))
    assert isinstance(llm, ChatModelLLM)
    assert llm.model.anthropic_api_url != VLLM_URL


def test_vllm_does_not_receive_cloud_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-cloud-key")
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    cfg = LLMConfig(provider="vllm", model="Qwen3.6-35B-A3B-NVFP4", base_url=VLLM_URL)
    llm = build_llm(cfg)
    assert llm.model.openai_api_base == VLLM_URL
    assert llm.model.openai_api_key.get_secret_value() == "EMPTY"

    monkeypatch.setenv("VLLM_API_KEY", "vllm-local-key")
    assert build_llm(cfg).model.openai_api_key.get_secret_value() == "vllm-local-key"


@pytest.mark.parametrize("base_url", [None, ""])
def test_vllm_without_base_url_fails_instead_of_falling_back_to_cloud(monkeypatch, base_url):
    # Without a vLLM URL, ChatOpenAI would silently use the cloud default (or OPENAI_BASE_URL)
    # and send the user's message there. Fail at startup instead.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://cloud.example.invalid/v1")
    cfg = LLMConfig(provider="vllm", model="Qwen3.6-35B-A3B-NVFP4", base_url=base_url)
    with pytest.raises(LLMConfigError, match="VLLM_BASE_URL"):
        build_llm(cfg)


def test_vllm_without_base_url_from_yaml_fails(monkeypatch, tmp_path):
    from regulated_agent_lab.agents.config import load_llm_config

    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    path = tmp_path / "llm.yaml"
    path.write_text("provider: vllm\nmodel: m\nbase_url: ${VLLM_BASE_URL}\n", encoding="utf-8")
    with pytest.raises(LLMConfigError):
        build_llm(load_llm_config(path))


def _vllm_cfg(model: str = "demo/model") -> LLMConfig:
    return LLMConfig(provider="vllm", model=model, base_url="http://vllm.example.local:8000/v1")


def _transport(status: int = 200, body: dict | None = None, error: Exception | None = None):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        if error is not None:
            raise error
        return httpx.Response(status, json=body or {})

    return httpx.MockTransport(handler)


async def test_vllm_probe_passes_when_the_model_is_served():
    from regulated_agent_lab.agents.llm import check_vllm_endpoint

    body = {"data": [{"id": "demo/model"}]}
    assert await check_vllm_endpoint(_vllm_cfg(), _transport(body=body)) is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"body": {"data": [{"id": "embedder"}]}}, "not the configured model 'demo/model'"),
        ({"status": 401}, "rejected VLLM_API_KEY (HTTP 401)"),
        ({"status": 503}, "answered HTTP 503"),
    ],
)
async def test_vllm_probe_names_the_problem(kwargs, expected):
    from regulated_agent_lab.agents.llm import check_vllm_endpoint

    problem = await check_vllm_endpoint(_vllm_cfg(), _transport(**kwargs))
    assert problem is not None and expected in problem


async def test_vllm_probe_reports_an_unreachable_endpoint():
    import httpx

    from regulated_agent_lab.agents.llm import check_vllm_endpoint

    down = _transport(error=httpx.ConnectError("refused"))
    assert "unreachable (ConnectError)" in await check_vllm_endpoint(_vllm_cfg(), down)


async def test_probe_skips_other_providers():
    from regulated_agent_lab.agents.llm import check_vllm_endpoint

    cfg = LLMConfig(provider="mock", model="m", base_url=None)
    assert await check_vllm_endpoint(cfg) is None


def test_time_window_rejects_earliest_after_latest():
    from regulated_agent_lab.agents.state import TimeWindow

    with pytest.raises(ValidationError):
        TimeWindow(earliest="14:00", latest="09:00")
    with pytest.raises(ValidationError):
        TripRequest(
            origin="Zurich HB",
            destination="Bern",
            depart_date="2026-12-01",
            outbound_window={"earliest": "18:00", "latest": "06:00"},
        )
    window = TimeWindow(earliest="06:00", latest="12:00")
    assert window.contains(window.earliest) and window.contains(window.latest)
    assert TimeWindow().is_open()


def test_trip_windows_are_optional_and_described_to_the_model():
    from datetime import date

    from regulated_agent_lab.agents.graph import intent_system_prompt

    trip = TripRequest(origin="Zurich HB", destination="Bern", depart_date="2026-12-01")
    assert trip.outbound_window is None and trip.return_window is None
    schema = unbounded_schema(Intent)
    assert "outbound_window" in set(_schema_keys(schema))
    assert "return_window" in set(_schema_keys(schema))
    prompt = intent_system_prompt(date(2026, 11, 21))
    for phrase in ("06:00-12:00", "12:00-18:00", "18:00-23:59", "earliest 09:00", "latest 14:00"):
        assert phrase in prompt
    assert "Omit a window when no time is stated" in prompt


def test_time_window_drops_a_model_supplied_offset():
    # Live models answer "06:00+01:00"; trains depart in naive local time, so the window must
    # compare as wall-clock time instead of raising TypeError on aware vs naive.
    from datetime import time

    from regulated_agent_lab.agents.state import TimeWindow

    window = TimeWindow.model_validate({"earliest": "06:00:00+01:00", "latest": "12:00:00+01:00"})
    assert window.earliest == time(6, 0) and window.earliest.tzinfo is None
    assert window.contains(time(9, 2))
