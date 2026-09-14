from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.provider.retry import invoke_with_retry

_PROBE_PROMPT = "Call the capability_probe tool with city='paris'."


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


def assert_provider_capability(
    pin: PinnedModel,
    model: InvokableToolModel,
    price_table: PriceTable,
) -> CapabilityAssertionResult:
    """ADR 0009's Required Capability list, established by attempting rather
    than by reading a declaration: tool calling, the pinned effort level,
    non-zero usage reporting, and a Price Table entry (`L1-4`, `L1-4a`).

    The Price Table entry is checked first and costs no call: no provider
    exposes a rate through any API, so its absence is caught before the
    (paid) live call runs.
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

    return CapabilityAssertionResult(pin=pin, response=response)
