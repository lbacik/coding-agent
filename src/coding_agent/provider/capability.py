"""The Provider Capability Assertion: one small live check at startup.

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 ("Preflight and
startup checks") and §2.1 (the Pinned Model and its prices come from the
Effective Configuration), and
[ADR 0009](../../../docs/adr/0009-provider-capability-is-established-by-attempting.md)
(capability is established by attempting, never by reading a declaration).

MVP row ids: L1-4, L1-4a.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool

from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.provider.retry import invoke_with_retry

_PROBE_PROMPT = "Call the capability_probe tool with city='paris'."

UNHONOURED_EFFORT = "not-an-effort-level"
"""The effort value the reach probe sends: no provider accepts it.

ADR 0009's cheapest probe: a deliberately invalid effort proves the
parameter reaches the provider at all, because a provider that receives it
refuses the request naming the parameter. An effort pin silently dropped on
the way to the wire is otherwise invisible — the run looks correct and is
not the run the Attempt recorded.
"""


@tool
def capability_probe(city: str) -> str:
    """A harmless tool the Provider Capability Assertion asks the Pinned Model to call."""
    return f"probe received {city!r}"


class ProviderCapabilityRefused(Exception):
    """The Provider Capability Assertion failed.

    No Attempt is opened, and nothing GitHub-facing is touched (ADR 0009
    part 4) — the caller's job is to turn this into a nonzero process exit.
    """


@dataclass(frozen=True)
class CapabilityAssertionResult:
    pin: PinnedModel
    response: AIMessage
    effort_refusal: str
    """The provider's own refusal of the invalid effort: the evidence that
    the effort parameter reaches the wire (L1-4a)."""


class _EffortOverride:
    """``bound``, invoked with a call-time ``reasoning_effort`` override.

    Both provider adapters accept ``reasoning_effort`` at call time and put
    it on the wire where the constructor's value would go (Anthropic's
    ``output_config.effort``, OpenAI Responses' ``reasoning.effort``).
    """

    def __init__(self, bound: InvokableToolModel, effort: str) -> None:
        self._bound = bound
        self._effort = effort

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        invoke = cast(Callable[..., BaseMessage], self._bound.invoke)
        return invoke(input, reasoning_effort=self._effort)


def assert_provider_capability(
    pin: PinnedModel,
    model: InvokableToolModel,
    price_table: PriceTable,
) -> CapabilityAssertionResult:
    """ADR 0009's Required Capability list, established by attempting rather
    than by reading a declaration (`L1-4`, `L1-4a`):

    - **a price** for the pin in ``price_table`` — on the configured path,
      the Effective Configuration's ``prices_usd_per_mtok``. No provider
      exposes a rate through any API, so this is checked first and costs no
      call;
    - **tool calling** and **non-zero usage** — one live call under the
      pinned effort must answer with a tool call and report its usage;
    - **the pinned effort reaches the provider** — the same request, sent
      with an invalid effort, must be refused as an invalid request. The
      live call above already proved the identical request otherwise
      succeeds, so the refusal is attributable to the effort value alone.
      A provider that accepts the invalid value is ignoring the parameter,
      and the pin is refused.
    """
    if price_for(price_table, pin) is None:
        raise ProviderCapabilityRefused(f"no Price Table entry for pinned model {pin.key!r}")

    try:
        bound = model.bind_tools([capability_probe])
        response = invoke_with_retry(bound, _PROBE_PROMPT)
    except Exception as exc:
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r} refused the capability probe: {exc}"
        ) from exc

    if not isinstance(response, AIMessage):
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r} returned {type(response).__name__}, not an AIMessage"
        )
    if not response.tool_calls:
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r} did not call the probe tool under the pinned "
            f"{pin.effort!r} effort — tool calling is not usable"
        )
    usage = response.usage_metadata
    if not usage or not usage.get("total_tokens"):
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r} reported zero usage on a successful response"
        )

    effort_refusal = _assert_effort_reaches_provider(pin, bound)
    return CapabilityAssertionResult(pin=pin, response=response, effort_refusal=effort_refusal)


def _assert_effort_reaches_provider(pin: PinnedModel, bound: InvokableToolModel) -> str:
    try:
        invoke_with_retry(_EffortOverride(bound, UNHONOURED_EFFORT), _PROBE_PROMPT)
    except ModelInvalidRequestError as exc:
        # Both providers refuse naming the parameter or the value (ADR 0009);
        # an invalid request for any other reason establishes nothing.
        if "effort" not in str(exc).lower():
            raise ProviderCapabilityRefused(
                f"pinned model {pin.key!r}: the invalid-effort probe was refused for a "
                f"reason that does not name the effort setting: {exc}"
            ) from exc
        return str(exc)
    except Exception as exc:
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r}: could not establish that the effort setting "
            f"reaches the provider: {exc}"
        ) from exc
    raise ProviderCapabilityRefused(
        f"pinned model {pin.key!r} accepted the invalid effort {UNHONOURED_EFFORT!r} — "
        f"the effort setting does not reach the provider, so the pinned "
        f"{pin.effort!r} effort is not honoured"
    )
