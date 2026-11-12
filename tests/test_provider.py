"""The provider layer: the Pinned Model, prices, retry and the Provider
Capability Assertion.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the Pinned Model
and ``api_key_env``), §18 (preflight and startup checks) and
[ADR 0009](../docs/adr/0009-provider-capability-is-established-by-attempting.md).

MVP row ids: L1-4, L1-4a, L1-5. Step-workflow row id: L1-4b.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.exceptions import ModelAPIError, ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.capability import (
    UNHONOURED_EFFORT,
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.config import (
    DEFAULT_CONTEXT_WINDOWS,
    DEFAULT_OUTPUT_CAPS,
    DEFAULT_PRICE_TABLE,
    PINNED_MODELS,
    policy_for_pin,
)
from coding_agent.provider.pinned_model import (
    DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP,
    DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS,
    DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS,
    PinnedModel,
    build_chat_model,
    canonical_gateway_url,
    gateway_base_url,
)
from coding_agent.provider.price_table import TokenPrices, price_for
from coding_agent.provider.retry import invoke_with_retry


class FakeChatModel:
    """A scripted chat model: each `invoke` pops the next queued outcome —
    an `AIMessage` to return, or an exception to raise. Structurally
    satisfies `InvokableToolModel`/`InvokableModel` without inheriting them.
    """

    def __init__(
        self, outcomes: list[BaseMessage | Exception], *, bind_tools_error: Exception | None = None
    ) -> None:
        self._outcomes = list(outcomes)
        self._bind_tools_error = bind_tools_error
        self.calls = 0
        self.call_kwargs: list[dict[str, Any]] = []
        self.tool_choices: list[str | None] = []

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> FakeChatModel:
        self.tool_choices.append(tool_choice)
        if self._bind_tools_error is not None:
            raise self._bind_tools_error
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        self.calls += 1
        self.call_kwargs.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DroppingToolChoiceFake(FakeChatModel):
    """A fake standing in for a path that silently downgrades a forced choice.

    It records whatever ``tool_choice`` the probe binds (so the test can
    prove the probe sent the forced choice) and then discards it: the queued
    outcomes model the ``auto`` behaviour — faced with the probe's contrary
    prompt it answers with text, so no probe tool call ever arrives.
    """


def _ai_message(
    *, tool_calls: list[dict[str, object]] | None = None, total_tokens: int | None = 12
) -> AIMessage:
    usage = None if total_tokens is None else {"input_tokens": 8, "output_tokens": 4, "total_tokens": total_tokens}
    return AIMessage(
        content="",
        tool_calls=tool_calls or [],
        usage_metadata=usage,
        response_metadata={"model": PIN.model},
    )


PIN = PinnedModel(provider="anthropic", model="claude-sonnet-5", endpoint="messages", effort="medium")
PRICE_TABLE = {PIN.key: TokenPrices(input=1.0, output=1.0, cache_read=1.0, cache_write=1.0)}

_PROBE_CALL: dict[str, object] = {"name": "capability_probe", "args": {"city": "paris"}, "id": "call_1", "type": "tool_call"}


def _effort_refused() -> ModelInvalidRequestError:
    """What both providers answer to an invalid effort value: a 400."""
    return ModelInvalidRequestError(
        "Error code: 400 - output_config.effort: Input should be 'low', 'medium' or 'high'"
    )


# --- PinnedModel -------------------------------------------------------------


def test_pinned_model_key_names_provider_model_and_endpoint() -> None:
    assert PIN.key == "anthropic:claude-sonnet-5@messages"


def test_pinned_model_rejects_anthropic_on_the_wrong_endpoint() -> None:
    with pytest.raises(ValueError, match="messages"):
        PinnedModel(provider="anthropic", model="claude-sonnet-5", endpoint="responses", effort="medium")


def test_pinned_model_rejects_openai_on_the_default_endpoint() -> None:
    with pytest.raises(ValueError, match="responses"):
        PinnedModel(provider="openai", model="gpt-5.6-terra", endpoint="messages", effort="medium")


def test_build_chat_model_uses_the_given_api_key_for_either_provider() -> None:
    """``api_key_env`` names the credential (§2.1): its value is passed to
    the adapter explicitly, so a name other than the SDK default works."""
    anthropic = build_chat_model(PIN, api_key="configured-anthropic-key")
    openai = build_chat_model(
        PinnedModel(provider="openai", model="gpt-5.6-luna", endpoint="responses", effort="medium"),
        api_key="configured-openai-key",
    )

    assert getattr(anthropic, "anthropic_api_key").get_secret_value() == "configured-anthropic-key"
    assert getattr(openai, "openai_api_key").get_secret_value() == "configured-openai-key"


def test_configured_pins_are_forced_onto_the_right_endpoint() -> None:
    assert PINNED_MODELS["anthropic"].endpoint == "messages"
    assert PINNED_MODELS["openai"].endpoint == "responses"


# --- Gateway pins -------------------------------------------------------------


def _gateway_pin(**overrides: object) -> PinnedModel:
    fields: dict[str, object] = {
        "provider": "litellm",
        "model": "muse-spark-1.3-contributor",
        "endpoint": "responses",
        "effort": "medium",
        "gateway_url": "http://pi.lan:4000",
        "output_cap_tokens": 32000,
        "context_threshold_tokens": 100000,
    }
    fields.update(overrides)
    return PinnedModel(**fields)  # type: ignore[arg-type]


def test_gateway_pin_key_is_the_kind_model_and_endpoint_label() -> None:
    pin = _gateway_pin()

    assert pin.is_gateway
    assert pin.key == "litellm:muse-spark-1.3-contributor@responses"
    assert not PIN.is_gateway


def test_gateway_pin_rejects_the_wrong_endpoint() -> None:
    with pytest.raises(ValueError, match="responses"):
        _gateway_pin(endpoint="messages")


def test_gateway_pin_rejects_a_direct_pin_carrying_token_keys() -> None:
    with pytest.raises(ValueError, match="output-cap-tokens"):
        PinnedModel(
            provider="anthropic",
            model="claude-sonnet-5",
            endpoint="messages",
            effort="medium",
            output_cap_tokens=32000,
        )
    with pytest.raises(ValueError, match="context-threshold-tokens"):
        PinnedModel(
            provider="anthropic",
            model="claude-sonnet-5",
            endpoint="messages",
            effort="medium",
            context_threshold_tokens=100000,
        )


def test_gateway_pin_rejects_a_threshold_at_the_safety_plus_overhead_floor() -> None:
    floor = DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS + DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS
    with pytest.raises(ValueError, match="context-threshold-tokens"):
        _gateway_pin(context_threshold_tokens=floor)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("http://pi.lan:4000", "http://pi.lan:4000"),
        ("HTTP://PI.LAN:4000/", "http://pi.lan:4000"),
        ("http://pi.lan:4000/v1/", "http://pi.lan:4000/v1"),
        ("https://PI.LAN:4000/V1", "https://pi.lan:4000/V1"),
    ],
)
def test_canonical_gateway_url_lowercases_scheme_and_host(
    raw: str, canonical: str
) -> None:
    assert canonical_gateway_url(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://pi.lan:4000",
        "http://user:pass@pi.lan:4000",
        "http://pi.lan:4000?key=value",
        "http://pi.lan:4000#section",
        "http:///no-host",
        "//pi.lan:4000",
        "http://pi.lan:4000:badport",
    ],
)
def test_canonical_gateway_url_refuses_non_literal_urls(raw: str) -> None:
    with pytest.raises(ValueError, match="gateway URL"):
        canonical_gateway_url(raw)


def test_build_chat_model_serves_a_gateway_pin_from_the_gateway_v1() -> None:
    """A gateway pin builds the OpenAI chat model on the Responses API
    against the gateway's ``/v1``: the gateway key, the pinned effort, the
    pin's output cap, ``store: False`` and the encrypted-reasoning include —
    with response headers exposed on each message, for the identity ticket."""
    model = build_chat_model(_gateway_pin(), api_key="test-gateway-key")

    assert getattr(model, "openai_api_base") == "http://pi.lan:4000/v1"
    assert getattr(model, "reasoning_effort") == "medium"
    assert getattr(model, "max_tokens") == 32000
    assert getattr(model, "store") is False
    assert getattr(model, "include") == ["reasoning.encrypted_content"]
    assert getattr(model, "include_response_headers") is True
    assert getattr(model, "use_responses_api") is True


def test_gateway_base_url_keeps_an_explicit_v1_suffix() -> None:
    pin = _gateway_pin(gateway_url="http://pi.lan:4000/v1")

    assert gateway_base_url(pin) == "http://pi.lan:4000/v1"


def test_gateway_base_url_appends_v1_to_a_bare_gateway_url() -> None:
    assert gateway_base_url(_gateway_pin()) == "http://pi.lan:4000/v1"


def test_policy_for_pin_resolves_a_gateway_pin_from_the_pin_itself() -> None:
    policy = policy_for_pin(_gateway_pin())

    assert policy.output_cap == 32000
    assert policy.context_window is not None
    assert policy.context_window.threshold_tokens == 100000
    assert policy.context_window.safety_margin_tokens == DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS
    assert policy.context_window.request_overhead_tokens == DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS
    assert policy.context_window.handoff_token_cap == DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP


def test_code_owned_per_pin_tables_never_hold_gateway_keys() -> None:
    assert "litellm:muse-spark-1.3-contributor@responses" not in DEFAULT_OUTPUT_CAPS
    assert "litellm:muse-spark-1.3-contributor@responses" not in DEFAULT_CONTEXT_WINDOWS


# --- Price Table --------------------------------------------------------------


def test_price_for_missing_pin_is_none() -> None:
    other = PinnedModel(provider="openai", model="gpt-5.6-terra", endpoint="responses", effort="medium")
    assert price_for(PRICE_TABLE, other) is None


def test_price_for_configured_pins_in_default_table() -> None:
    for pin in PINNED_MODELS.values():
        assert price_for(DEFAULT_PRICE_TABLE, pin) is not None


# --- assert_provider_capability -----------------------------------------------


def test_capability_assertion_passes_on_a_well_formed_tool_call() -> None:
    response = _ai_message(tool_calls=[_PROBE_CALL])
    model = FakeChatModel([response, _effort_refused()])

    result = assert_provider_capability(PIN, model, PRICE_TABLE)

    # The object returned by the model is exactly what gets asserted on and
    # returned — never rebuilt (ADR 0010).
    assert result.response is response
    assert result.pin is PIN
    assert "effort" in result.effort_refusal
    # The probe binds its tool with a forced choice naming it (L1-4b).
    assert model.tool_choices == ["capability_probe"]


def test_l1_4a_effort_is_established_by_attempting_an_invalid_value() -> None:
    """L1-4a: the effort pin is established by a live call, not a lookup.

    The first call runs under the pinned effort (no override); the second is
    the same request with an invalid effort, which the provider must refuse.
    """
    model = FakeChatModel([_ai_message(tool_calls=[_PROBE_CALL]), _effort_refused()])

    assert_provider_capability(PIN, model, PRICE_TABLE)

    assert model.calls == 2
    assert model.call_kwargs[0] == {}
    assert model.call_kwargs[1] == {"reasoning_effort": UNHONOURED_EFFORT}


def test_l1_4_refuses_when_the_provider_accepts_an_invalid_effort() -> None:
    """L1-4: an effort setting that never reaches the provider is refused —
    the pinned effort would be silently dropped from every Attempt."""
    model = FakeChatModel(
        [_ai_message(tool_calls=[_PROBE_CALL]), _ai_message(tool_calls=[_PROBE_CALL])]
    )

    with pytest.raises(ProviderCapabilityRefused, match="effort is not honoured"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


def test_l1_4_an_invalid_request_that_does_not_name_effort_establishes_nothing() -> None:
    """ADR 0009: the provider's refusal names the effort parameter or value;
    a 400 for some other reason does not prove the effort reaches the wire."""
    model = FakeChatModel(
        [_ai_message(tool_calls=[_PROBE_CALL]), ModelInvalidRequestError("max_tokens too large")]
    )

    with pytest.raises(ProviderCapabilityRefused, match="does not name the effort"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


def test_l1_4_refuses_when_the_effort_probe_fails_for_another_reason() -> None:
    """Only an invalid-request refusal counts as the effort reaching the
    provider; any other failure leaves it unestablished, and that refuses."""
    # The effort probe retries a transient failure like any call, and the
    # third exhausts it.
    outcomes: list[BaseMessage | Exception] = [_ai_message(tool_calls=[_PROBE_CALL])]
    outcomes += [ModelAPIError("connection reset")] * 3
    model = FakeChatModel(outcomes)

    with pytest.raises(ProviderCapabilityRefused, match="could not establish"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


def test_capability_assertion_refuses_without_a_price_table_entry() -> None:
    model = FakeChatModel([_ai_message(tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}])])

    with pytest.raises(ProviderCapabilityRefused, match="Price Table"):
        assert_provider_capability(PIN, model, {})

    # A missing price is caught before spending on a live call.
    assert model.calls == 0


def test_capability_assertion_refuses_when_no_tool_call_arrives() -> None:
    model = FakeChatModel([_ai_message(tool_calls=[])])

    with pytest.raises(ProviderCapabilityRefused, match="forced tool choice"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


def test_l1_4b_forced_choice_silently_downgraded_to_auto_is_refused() -> None:
    """L1-4b: the probe binds its tool with a forced ``tool_choice`` naming
    it and sends a prompt asking for no tool call. A path that drops the
    forced choice behaves as ``auto`` and answers the contrary prompt with
    text — and the pin is refused, naming the unhonoured forced choice."""
    model = DroppingToolChoiceFake([_ai_message(tool_calls=[])])

    with pytest.raises(ProviderCapabilityRefused, match="forced tool choice"):
        assert_provider_capability(PIN, model, PRICE_TABLE)

    # The probe did send the forced choice; the fake dropped it.
    assert model.tool_choices == ["capability_probe"]


def test_capability_assertion_refuses_on_zero_usage() -> None:
    model = FakeChatModel(
        [_ai_message(
            tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}], total_tokens=0
        )]
    )

    with pytest.raises(ProviderCapabilityRefused, match="zero usage"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


def test_capability_assertion_wraps_a_provider_refusal_cleanly() -> None:
    model = FakeChatModel([ModelInvalidRequestError("effort value not supported")])

    with pytest.raises(ProviderCapabilityRefused, match="refused the capability probe"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


def test_capability_assertion_wraps_a_bind_tools_failure_cleanly() -> None:
    """A model/endpoint pair that cannot even bind a tool must refuse
    cleanly too, not propagate a raw exception (issue #31: "never a stack
    trace")."""
    model = FakeChatModel([], bind_tools_error=ModelInvalidRequestError("tools not supported here"))

    with pytest.raises(ProviderCapabilityRefused, match="refused the capability probe"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


# --- invoke_with_retry (L1-5, unit-level: 529/503 cannot be summoned for real) ---


def test_invoke_with_retry_retries_a_transient_overload_and_returns_it_verbatim() -> None:
    response = _ai_message()
    model = FakeChatModel([ModelAPIError("synthetic 529 overload"), ModelAPIError("synthetic 529 overload"), response])

    result = invoke_with_retry(model, "hi")

    assert result is response
    assert model.calls == 3


def test_invoke_with_retry_never_swaps_to_a_different_model() -> None:
    """No fallback wiring exists anywhere in this adapter: the retry only
    ever calls the one `model` object it was given (structural evidence
    for "no cross-model fallback")."""
    response = _ai_message()
    model = FakeChatModel([ModelAPIError("synthetic overload"), response])

    invoke_with_retry(model, "hi")

    assert model.calls == 2  # both attempts landed on the same `model` instance


def test_invoke_with_retry_does_not_retry_a_non_retryable_error() -> None:
    model = FakeChatModel([ModelInvalidRequestError("bad request"), _ai_message()])

    with pytest.raises(ModelInvalidRequestError):
        invoke_with_retry(model, "hi")

    assert model.calls == 1


def test_invoke_with_retry_exhausts_attempts_and_raises_the_last_error() -> None:
    model = FakeChatModel([ModelAPIError("1"), ModelAPIError("2"), ModelAPIError("3")])

    with pytest.raises(ModelAPIError):
        invoke_with_retry(model, "hi", max_attempts=3)

    assert model.calls == 3


# --- Real-provider smoke tests (issue #31: confirmed for real by whoever picks this up) ---


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set")
def test_provider_capability_assertion_against_real_anthropic() -> None:
    """Direct-provider L1: the probe binds the forced choice and sends the
    contrary prompt — only a call to the probe tool passes."""
    pin = PINNED_MODELS["anthropic"]
    model = build_chat_model(pin)
    result = assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    assert "effort" in result.effort_refusal
    assert result.response.tool_calls
    assert result.response.tool_calls[0]["name"] == "capability_probe"
    assert result.response.usage_metadata is not None
    assert result.response.usage_metadata["total_tokens"] > 0


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_provider_capability_assertion_against_real_openai() -> None:
    """Direct-provider L1: the probe binds the forced choice and sends the
    contrary prompt — only a call to the probe tool passes."""
    pin = PINNED_MODELS["openai"]
    model = build_chat_model(pin)
    result = assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    assert UNHONOURED_EFFORT in result.effort_refusal
    assert result.response.tool_calls
    assert result.response.tool_calls[0]["name"] == "capability_probe"
    assert result.response.usage_metadata is not None
    assert result.response.usage_metadata["total_tokens"] > 0
