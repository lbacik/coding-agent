from __future__ import annotations

import os
from collections.abc import Sequence

import pytest
from langchain_core.exceptions import ModelAPIError, ModelInvalidRequestError
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.config import DEFAULT_PRICE_TABLE, PINNED_MODELS
from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
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

    def bind_tools(self, tools: Sequence[BaseTool]) -> FakeChatModel:
        if self._bind_tools_error is not None:
            raise self._bind_tools_error
        return self

    def invoke(self, input: str) -> BaseMessage:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ai_message(
    *, tool_calls: list[dict[str, object]] | None = None, total_tokens: int | None = 12
) -> AIMessage:
    usage = None if total_tokens is None else {"input_tokens": 8, "output_tokens": 4, "total_tokens": total_tokens}
    return AIMessage(
        content="",
        tool_calls=tool_calls or [],
        usage_metadata=usage,
        response_metadata={"model_name": "fake-model"},
    )


PIN = PinnedModel(provider="anthropic", model="claude-sonnet-5", endpoint="messages", effort="medium")
PRICE_TABLE = {PIN.key: TokenPrices(input=1.0, output=1.0, cache_read=1.0, cache_write=1.0)}


# --- PinnedModel -------------------------------------------------------------


def test_pinned_model_key_names_provider_model_and_endpoint() -> None:
    assert PIN.key == "anthropic:claude-sonnet-5@messages"


def test_pinned_model_rejects_anthropic_on_the_wrong_endpoint() -> None:
    with pytest.raises(ValueError, match="messages"):
        PinnedModel(provider="anthropic", model="claude-sonnet-5", endpoint="responses", effort="medium")


def test_pinned_model_rejects_openai_on_the_default_endpoint() -> None:
    with pytest.raises(ValueError, match="responses"):
        PinnedModel(provider="openai", model="gpt-5.6-terra", endpoint="messages", effort="medium")


def test_configured_pins_are_forced_onto_the_right_endpoint() -> None:
    assert PINNED_MODELS["anthropic"].endpoint == "messages"
    assert PINNED_MODELS["openai"].endpoint == "responses"


# --- Price Table --------------------------------------------------------------


def test_price_for_missing_pin_is_none() -> None:
    other = PinnedModel(provider="openai", model="gpt-5.6-terra", endpoint="responses", effort="medium")
    assert price_for(PRICE_TABLE, other) is None


def test_price_for_configured_pins_in_default_table() -> None:
    for pin in PINNED_MODELS.values():
        assert price_for(DEFAULT_PRICE_TABLE, pin) is not None


# --- assert_provider_capability -----------------------------------------------


def test_capability_assertion_passes_on_a_well_formed_tool_call() -> None:
    response = _ai_message(
        tool_calls=[{"name": "capability_probe", "args": {"city": "paris"}, "id": "call_1", "type": "tool_call"}]
    )
    model = FakeChatModel([response])

    result = assert_provider_capability(PIN, model, PRICE_TABLE)

    # The object returned by the model is exactly what gets asserted on and
    # returned — never rebuilt (ADR 0010).
    assert result.response is response
    assert result.pin is PIN


def test_capability_assertion_refuses_without_a_price_table_entry() -> None:
    model = FakeChatModel([_ai_message(tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}])])

    with pytest.raises(ProviderCapabilityRefused, match="Price Table"):
        assert_provider_capability(PIN, model, {})

    # A missing price is caught before spending on a live call.
    assert model.calls == 0


def test_capability_assertion_refuses_when_no_tool_call_arrives() -> None:
    model = FakeChatModel([_ai_message(tool_calls=[])])

    with pytest.raises(ProviderCapabilityRefused, match="did not call"):
        assert_provider_capability(PIN, model, PRICE_TABLE)


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
    pin = PINNED_MODELS["anthropic"]
    model = build_chat_model(pin)
    result = assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    assert result.response.tool_calls
    assert result.response.usage_metadata is not None
    assert result.response.usage_metadata["total_tokens"] > 0


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_provider_capability_assertion_against_real_openai() -> None:
    pin = PINNED_MODELS["openai"]
    model = build_chat_model(pin)
    result = assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    assert result.response.tool_calls
    assert result.response.usage_metadata is not None
    assert result.response.usage_metadata["total_tokens"] > 0
