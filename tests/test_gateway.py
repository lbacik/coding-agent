"""Gateway pins through the fake Model Gateway (contract §2.1).

The Worker runs a gateway pin by speaking OpenAI Responses to the LiteLLM
Proxy through its existing LangChain adapter; the gateway changes only the
transport, and the Worker still owns the tool loop (ADR 0023). These tests
drive the real adapter over the fake gateway
(``tests/support/fake_gateway.py``), which replays the redacted recordings
in ``tests/fixtures/gateway/`` plus mutated variants:

- the request on the wire: ``store: False``,
  ``include: ["reasoning.encrypted_content"]``, the forced ``tool_choice``
  on the probe, the output cap and the effort;
- the Provider Capability Assertion refusing Meta's forced-tool 400
  (recorded) and the accepted invalid effort (recorded 200);
- the assertion passing on the mutated happy variant, where the forced
  tool is honoured and an invalid effort is refused by the gateway;
- a two-turn tool loop carrying the encrypted reasoning back byte-equal.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from coding_agent.provider.capability import (
    UNHONOURED_EFFORT,
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
from coding_agent.provider.price_table import TokenPrices
from support.fake_gateway import (
    FIXTURE_DIR,
    FakeGatewayTransport,
    queue_responder,
    recording_response,
)

GATEWAY_KEY = "test-gateway-key"
GATEWAY_URL = "http://gateway.test:4000"


def _gateway_pin(**overrides: object) -> PinnedModel:
    fields: dict[str, object] = {
        "provider": "litellm",
        "model": "muse-spark-1.3-contributor",
        "endpoint": "responses",
        "effort": "medium",
        "gateway_url": GATEWAY_URL,
        "output_cap_tokens": 32000,
        "context_threshold_tokens": 100000,
    }
    fields.update(overrides)
    return PinnedModel(**fields)  # type: ignore[arg-type]


def _price_table(pin: PinnedModel) -> dict[str, TokenPrices]:
    return {
        pin.key: TokenPrices(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50)
    }


def _gateway_model(pin: PinnedModel, transport: httpx.BaseTransport) -> Any:
    """The real adapter pointed at the fake gateway."""
    return build_chat_model(pin, api_key=GATEWAY_KEY, transport=transport)


@tool
def get_weather(city: str) -> str:
    """A harmless work tool for the two-turn loop."""
    return f"sunny in {city}"


def test_gateway_pin_passes_capability_with_self_contained_requests() -> None:
    """The mutated happy variant: the gateway honours the forced tool and
    refuses the invalid effort, and every request is self-contained.

    Each request goes to the gateway's ``/v1`` Responses endpoint with the
    gateway key, the effort, the output cap, ``store: False`` and
    ``include: ["reasoning.encrypted_content"]``; the probe forces the
    ``tool_choice``. Response headers land on the message, for the gateway
    identity ticket.
    """
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_probe_ok_200.json"),
                recording_response("litellm_effort_refused_400.json"),
            ]
        )
    )

    result = assert_provider_capability(
        pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
    )

    assert result.pin is pin
    assert result.response.tool_calls[0]["name"] == "capability_probe"
    assert "effort" in result.effort_refusal
    headers = result.response.response_metadata.get("headers", {})
    assert headers.get("x-request-id") == "req-probe-ok"

    assert len(transport.requests) == 2
    for recorded in transport.requests:
        assert recorded.method == "POST"
        assert recorded.authorization == f"Bearer {GATEWAY_KEY}"
        assert recorded.body["model"] == "muse-spark-1.3-contributor"
        assert recorded.body["store"] is False
        assert recorded.body["include"] == ["reasoning.encrypted_content"]
        assert recorded.body["max_output_tokens"] == 32000
    probe, effort_probe = (recorded.body for recorded in transport.requests)
    assert probe["reasoning"] == {"effort": "medium"}
    assert probe["tool_choice"] == {"type": "function", "name": "capability_probe"}
    assert effort_probe["reasoning"] == {"effort": UNHONOURED_EFFORT}
    assert effort_probe["max_output_tokens"] == 32000
    transport.assert_only_responses_api()


def test_capability_refuses_the_recorded_meta_forced_tool_400() -> None:
    """Recorded: a Meta model behind the gateway refuses the forced tool
    choice with a 400 — the pin is refused, naming the probe."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder([recording_response("litellm_meta_forced_tool_400.json")])
    )

    with pytest.raises(ProviderCapabilityRefused, match="refused the capability probe"):
        assert_provider_capability(
            pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
        )

    # One attempt, not a retried transient: a 400 is not retryable.
    assert len(transport.requests) == 1
    # The probe itself was well-formed: it forced the choice the gateway
    # refused, so the refusal is about honouring, not about the request.
    assert transport.requests[0].body["tool_choice"] == {
        "type": "function",
        "name": "capability_probe",
    }
    transport.assert_only_responses_api()


def test_capability_refuses_the_recorded_accepted_invalid_effort() -> None:
    """Recorded: the gateway answers the invalid-effort probe with a 200 —
    the effort setting does not reach the provider, so the pin is refused."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_probe_ok_200.json"),
                recording_response("litellm_effort_accepted_200.json"),
            ]
        )
    )

    with pytest.raises(ProviderCapabilityRefused, match="effort is not honoured"):
        assert_provider_capability(
            pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
        )

    assert len(transport.requests) == 2
    assert transport.requests[1].body["reasoning"] == {"effort": UNHONOURED_EFFORT}
    transport.assert_only_responses_api()


def test_two_turn_tool_loop_carries_encrypted_reasoning_byte_equal() -> None:
    """The first turn's ``reasoning.encrypted_content`` block travels inside
    the second request byte-equal — verbatim means a self-contained turn,
    never a reference to server-side state (ADR 0010)."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_turn1_200.json"),
                recording_response("litellm_turn2_200.json"),
            ]
        )
    )
    model: Any = _gateway_model(pin, transport)
    bound: Any = model.bind_tools([get_weather])

    first = bound.invoke([HumanMessage(content="what is the weather in paris?")])
    assert isinstance(first, AIMessage)
    assert first.tool_calls and first.tool_calls[0]["name"] == "get_weather"
    encrypted = [
        block.get("encrypted_content")
        for block in first.content
        if isinstance(block, dict) and block.get("type") == "reasoning"
    ]
    assert encrypted == ["cmVkYWN0ZWQtdHVybi0xLWVuY3J5cHRlZC1ieXRlcw=="]

    history = [
        HumanMessage(content="what is the weather in paris?"),
        first,
        ToolMessage(content="sunny, 21C", tool_call_id=first.tool_calls[0]["id"]),
    ]
    second = bound.invoke(history)
    assert "Sunny in Paris." in str(second.content)

    assert len(transport.requests) == 2
    turn_two_input = transport.requests[1].body["input"]
    reasoning_blocks = [
        block
        for block in turn_two_input
        if isinstance(block, dict) and block.get("type") == "reasoning"
    ]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0]["encrypted_content"] == encrypted[0]
    assert reasoning_blocks[0]["id"] == "rs_turn_1"
    assert transport.requests[1].body["max_output_tokens"] == 32000
    transport.assert_only_responses_api()


def test_no_gateway_metadata_endpoint_is_ever_called() -> None:
    """The adapter speaks only the Responses API: ``/model/info`` and
    ``/v1/models`` are gateway declarations, never read (ADR 0009)."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_turn1_200.json"),
                recording_response("litellm_turn2_200.json"),
            ]
        )
    )
    model: Any = _gateway_model(pin, transport)

    model.invoke("hi")
    model.invoke("hi again")

    assert [recorded.path for recorded in transport.requests] == [
        "/v1/responses",
        "/v1/responses",
    ]
    transport.assert_only_responses_api()


def test_vendored_recordings_are_redacted_of_keys_and_addresses() -> None:
    """The fixtures carry no credential values and no real addresses."""
    forbidden = [
        r"sk-[A-Za-z0-9]",
        r"ghp_",
        r"github_pat",
        r"BEGIN [A-Z ]*PRIVATE KEY",
        r"192\.168\.\d",
        r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        r"pi\.lan",
        r"[Bb]earer [A-Za-z0-9._-]{8,}",
    ]
    recordings = sorted(FIXTURE_DIR.glob("litellm_*.json"))
    assert len(recordings) >= 6
    for path in recordings:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not re.search(pattern, text), f"{path.name} matches {pattern!r}"


def test_direct_pins_take_no_injected_transport() -> None:
    """The transport seam is gateway-only: direct pins are unaffected."""
    direct = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
    )
    with pytest.raises(ValueError, match="gateway-only"):
        build_chat_model(direct, api_key="key", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
