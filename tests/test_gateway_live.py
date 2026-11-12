"""L1 gateway rows: the same suite against a real gateway (matrix GW-1…GW-5).

Each row runs the same assertions as the fake-gateway unit tests in
``tests/test_gateway.py`` — the Provider Capability Assertion, the
``x-litellm-model-group`` identity check, the verbatim two-turn tool loop,
and the provoked retry / fallback rows — but over a real operator-provided
gateway instead of ``tests/support/fake_gateway.py``.

These rows are marked ``l1_gateway``, gated on the gateway environment, and
never run by default or in CI: without ``TEST_GATEWAY_URL``,
``TEST_GATEWAY_API_KEY`` and ``TEST_GATEWAY_MODEL`` every row skips, and CI
additionally deselects the mark. Run them manually before each release tag
and after each LiteLLM upgrade, and record the upstream and the observed
``x-litellm-version`` in the matrix row as a fact about that run — never as
a support claim. L4 stays direct-only: no gateway row runs there, and there
is no supported-upstream matrix.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from coding_agent.provider.capability import assert_provider_capability
from coding_agent.provider.observed_identity import check_gateway_response
from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
from coding_agent.provider.price_table import TokenPrices
from support.gateway_assertions import (
    assert_gateway_identity_ok,
    assert_probe_request,
    assert_reasoning_carried_verbatim,
    assert_self_contained_request,
    assert_suppression_on_wire,
    gateway_headers_of,
    summarize_gateway_evidence,
)

pytestmark = pytest.mark.l1_gateway


@dataclass(frozen=True)
class LiveGatewayConfig:
    """The real gateway under test: URL, key and the pinned L1 alias."""

    url: str
    api_key: str
    model: str
    effort: str
    output_cap: int
    context_threshold: int


def _live_config() -> LiveGatewayConfig:
    """The operator-provided gateway, or skip without it.

    ``TEST_GATEWAY_MODEL`` is the gateway's own L1 alias: the name the
    gateway reports in ``x-litellm-model-group``. Effort, output cap and
    context threshold default to the fake suite's values and are overridable
    for a gateway whose L1 alias needs different ones.
    """
    url = os.environ.get("TEST_GATEWAY_URL")
    api_key = os.environ.get("TEST_GATEWAY_API_KEY")
    model = os.environ.get("TEST_GATEWAY_MODEL")
    if not url or not api_key or not model:
        pytest.skip(
            "real-gateway L1 rows need TEST_GATEWAY_URL, "
            "TEST_GATEWAY_API_KEY and TEST_GATEWAY_MODEL"
        )
    return LiveGatewayConfig(
        url=url,
        api_key=api_key,
        model=model,
        effort=os.environ.get("TEST_GATEWAY_EFFORT", "medium"),
        output_cap=int(os.environ.get("TEST_GATEWAY_OUTPUT_CAP_TOKENS", "32000")),
        context_threshold=int(
            os.environ.get("TEST_GATEWAY_CONTEXT_THRESHOLD_TOKENS", "100000")
        ),
    )


class _ForwardingRecorder(httpx.HTTPTransport):
    """An ``httpx`` transport that records request bodies, then performs
    real I/O. The live rows assert on the recorded wire bodies that the
    per-request suppression was sent, while the responses come from the real
    gateway."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.bodies.append(dict(json.loads(request.content.decode("utf-8"))))
        return super().handle_request(request)


def _live_pin(config: LiveGatewayConfig) -> PinnedModel:
    return PinnedModel(
        provider="litellm",
        model=config.model,
        endpoint="responses",
        effort=config.effort,  # type: ignore[arg-type]
        gateway_url=config.url,
        output_cap_tokens=config.output_cap,
        context_threshold_tokens=config.context_threshold,
    )


def _live_prices(pin: PinnedModel) -> dict[str, TokenPrices]:
    return {
        pin.key: TokenPrices(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50)
    }


def _note_evidence(tag: str, message: AIMessage) -> None:
    """Record the run's fact line: what answered and which gateway version."""
    print(f"[{tag}] {summarize_gateway_evidence(gateway_headers_of(message))}")


@tool
def get_weather(city: str) -> str:
    """A harmless work tool for the live two-turn loop."""
    return f"sunny in {city}"


def test_live_gateway_capability_assertion() -> None:
    """GW-1: the Provider Capability Assertion passes against the real gateway.

    The forced probe tool is honoured under the pinned effort, the invalid
    effort is refused, and every request on the wire is self-contained with
    the per-request suppression sent.
    """
    config = _live_config()
    pin = _live_pin(config)
    recorder = _ForwardingRecorder()
    model: Any = build_chat_model(pin, api_key=config.api_key, transport=recorder)

    result = assert_provider_capability(
        pin, model, _live_prices(pin), max_tokens=config.output_cap
    )

    assert result.pin is pin
    assert result.response.tool_calls
    assert result.response.tool_calls[0]["name"] == "capability_probe"
    assert "effort" in result.effort_refusal
    assert len(recorder.bodies) == 2
    probe, effort_probe = recorder.bodies
    assert_probe_request(
        probe, model=config.model, effort=config.effort, output_cap=config.output_cap
    )
    assert_self_contained_request(
        effort_probe,
        model=config.model,
        effort=config.effort,
        output_cap=config.output_cap,
    )
    assert all(path == "/v1/responses" for path in recorder.paths)
    _note_evidence("GW-1", result.response)


def test_live_gateway_model_group_equals_pinned_name() -> None:
    """GW-2: the real gateway answers under the pinned L1 alias.

    The response's ``x-litellm-model-group`` equals the pinned name with no
    fallback or retry evidence — the same identity check the fake suite runs.
    """
    config = _live_config()
    pin = _live_pin(config)
    model: Any = build_chat_model(pin, api_key=config.api_key)

    message = model.invoke("Reply with the single word OK.")
    assert isinstance(message, AIMessage)

    check = check_gateway_response(pin, message)
    assert check.violated is False
    assert check.observed == config.model
    assert_gateway_identity_ok(gateway_headers_of(message), pinned_model=config.model)
    _note_evidence("GW-2", message)


def test_live_gateway_two_turn_tool_loop_verbatim() -> None:
    """GW-3: a cheap two-turn tool loop carries the turn back verbatim.

    The assistant turn — including encrypted reasoning — travels inside the
    second request byte-equal, never by server-side reference, with the
    per-request suppression sent on both legs.
    """
    config = _live_config()
    pin = _live_pin(config)
    recorder = _ForwardingRecorder()
    model: Any = build_chat_model(pin, api_key=config.api_key, transport=recorder)
    bound: Any = model.bind_tools([get_weather])

    first = bound.invoke([HumanMessage(content="what is the weather in paris?")])
    assert isinstance(first, AIMessage)
    assert first.tool_calls and first.tool_calls[0]["name"] == "get_weather"

    history = [
        HumanMessage(content="what is the weather in paris?"),
        first,
        ToolMessage(content="sunny, 21C", tool_call_id=first.tool_calls[0]["id"]),
    ]
    second = bound.invoke(history)
    assert isinstance(second, AIMessage)

    assert len(recorder.bodies) == 2
    for body in recorder.bodies:
        assert_suppression_on_wire(body)
    assert_reasoning_carried_verbatim(first, recorder.bodies[1])
    for message in (first, second):
        check = check_gateway_response(pin, message)
        assert check.violated is False
    _note_evidence("GW-3", second)


def test_live_gateway_provoked_retry_shows_none() -> None:
    """GW-4: a provoked gateway retry shows none happened.

    The probe pair exercises the gateway path where a configured retry would
    surface; the row asserts the identity evidence shows none while
    ``num_retries: 0`` was sent on every request. Provoked, not simulated:
    a gateway that ignored the suppression would be caught by the evidence.
    """
    config = _live_config()
    pin = _live_pin(config)
    recorder = _ForwardingRecorder()
    model: Any = build_chat_model(pin, api_key=config.api_key, transport=recorder)

    result = assert_provider_capability(
        pin, model, _live_prices(pin), max_tokens=config.output_cap
    )

    assert len(recorder.bodies) == 2
    for body in recorder.bodies:
        assert body.get("num_retries") == 0
    check = check_gateway_response(pin, result.response)
    assert check.violated is False
    assert check.gateway is not None
    assert check.gateway.attempted_retries == 0
    _note_evidence("GW-4", result.response)


def test_live_gateway_provoked_fallback_shows_none() -> None:
    """GW-5: a provoked gateway fallback shows none happened.

    A tool-calling turn exercises the gateway path where a configured
    fallback would surface; the row asserts the identity evidence shows
    none while ``disable_fallbacks: true`` was sent. A gateway that ignored
    the suppression would be caught by the evidence.
    """
    config = _live_config()
    pin = _live_pin(config)
    recorder = _ForwardingRecorder()
    model: Any = build_chat_model(pin, api_key=config.api_key, transport=recorder)
    bound: Any = model.bind_tools([get_weather])

    first = bound.invoke([HumanMessage(content="what is the weather in paris?")])
    assert isinstance(first, AIMessage)
    assert first.tool_calls and first.tool_calls[0]["name"] == "get_weather"

    assert len(recorder.bodies) == 1
    assert recorder.bodies[0].get("disable_fallbacks") is True
    check = check_gateway_response(pin, first)
    assert check.violated is False
    assert check.gateway is not None
    assert check.gateway.attempted_fallbacks == 0
    _note_evidence("GW-5", first)
