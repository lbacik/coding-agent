"""The Provider Capability Assertion: one small live check at startup.

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 ("Preflight and
startup checks") and §2.1 (the Pinned Model and its prices come from the
Effective Configuration), and
[ADR 0009](../../../docs/adr/0009-provider-capability-is-established-by-attempting.md)
(capability is established by attempting, never by reading a declaration).

MVP row ids: L1-4, L1-4a. Step-workflow row id: L1-4b.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool, tool

from coding_agent.provider.observed_identity import check_direct_pin
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.provider.retry import invoke_with_retry

_PROBE_PROMPT = "Reply with the single word OK. Do not call any tool."

_FORCED_TOOL_CHOICE = "capability_probe"
"""The probe tool, named as the forced ``tool_choice`` of the probe request.

Only a call to this tool passes: a text answer means the forced choice was
dropped somewhere on the way (silently downgraded to ``auto``), and the pin
is refused.
"""

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

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> InvokableToolModel:
        return _EffortOverride(
            self._bound.bind_tools(tools, tool_choice=tool_choice), self._effort
        )

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        invoke = cast(Callable[..., BaseMessage], self._bound.invoke)
        return invoke(input, reasoning_effort=self._effort, **kwargs)


def assert_provider_capability(
    pin: PinnedModel,
    model: InvokableToolModel,
    price_table: PriceTable,
    *,
    max_tokens: int | None = None,
) -> CapabilityAssertionResult:
    """ADR 0009's Required Capability list, established by attempting rather
    than by reading a declaration (`L1-4`, `L1-4a`, `L1-4b`):

    - **a price** for the pin in ``price_table`` — on the configured path,
      the Effective Configuration's ``prices_usd_per_mtok``. No provider
      exposes a rate through any API, so this is checked first and costs no
      call;
    - **a forced call of a named tool, proven against a contrary prompt**,
      and **non-zero usage** — one live call under the pinned effort binds
      the probe tool with a forced ``tool_choice`` naming it and sends a
      prompt asking for no tool call; only a call to the probe tool passes,
      and that same response must report its usage. A text answer means the
      forced choice was dropped on the way, and the pin is refused;
    - **the pinned effort reaches the provider** — the same request, sent
      with an invalid effort, must be refused as an invalid request. The
      live call above already proved the identical request otherwise
      succeeds, so the refusal is attributable to the effort value alone.
      A provider that accepts the invalid value is ignoring the parameter,
      and the pin is refused.

    Step-workflow contract §2.10, §11.4 (matrix row STEP-17): where
    ``max_tokens`` — the Pinned Model's code-owned output cap — is given,
    both probe requests carry it, so the assertion proves the cap reaches
    the provider. ``None`` keeps the MVP call shape working.
    """
    if price_for(price_table, pin) is None:
        raise ProviderCapabilityRefused(f"no Price Table entry for pinned model {pin.key!r}")

    try:
        bound = model.bind_tools([capability_probe], tool_choice=_FORCED_TOOL_CHOICE)
        response = _invoke_probe(bound, _PROBE_PROMPT, max_tokens=max_tokens)
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
            f"pinned model {pin.key!r} did not honour the forced tool choice "
            f"{_FORCED_TOOL_CHOICE!r} under the pinned {pin.effort!r} effort — "
            f"answered with text instead of calling the probe tool"
        )
    usage = response.usage_metadata
    if not usage or not usage.get("total_tokens"):
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r} reported zero usage on a successful response"
        )
    # Gateway pins run without the answering-model assertion until the
    # gateway identity ticket lands (contract §3): the gateway reports its
    # own model name, never the pin key.
    check = check_direct_pin(pin, response)
    if not pin.is_gateway and check.violated:
        raise ProviderCapabilityRefused(
            f"pinned model {pin.key!r} resolved to {check.observed!r}: a direct "
            f"pin's probe response must report the pinned model id "
            f"{pin.model!r} exactly — pin the model the vendor reports"
        )

    effort_refusal = _assert_effort_reaches_provider(pin, bound, max_tokens=max_tokens)
    return CapabilityAssertionResult(pin=pin, response=response, effort_refusal=effort_refusal)


def _invoke_probe(
    bound: InvokableToolModel, prompt: str, *, max_tokens: int | None
) -> BaseMessage:
    """Run one capability probe, carrying the output cap where one is pinned.

    ``max_tokens`` travels as a call-time kwarg the fakes record and the
    real adapters forward, exactly like the effort override below. ``None``
    sends the probe exactly as the MVP did.
    """
    if max_tokens is None:
        return invoke_with_retry(bound, prompt)
    return invoke_with_retry(_OutputCapOverride(bound, max_tokens), prompt)


class _OutputCapOverride:
    """``bound``, invoked with a call-time ``max_tokens`` output cap."""

    def __init__(self, bound: InvokableToolModel, max_tokens: int) -> None:
        self._bound = bound
        self._max_tokens = max_tokens

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> InvokableToolModel:
        return _OutputCapOverride(
            self._bound.bind_tools(tools, tool_choice=tool_choice), self._max_tokens
        )

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        invoke = cast(Callable[..., BaseMessage], self._bound.invoke)
        return invoke(input, max_tokens=self._max_tokens, **kwargs)


def _assert_effort_reaches_provider(
    pin: PinnedModel, bound: InvokableToolModel, *, max_tokens: int | None = None
) -> str:
    try:
        effort_probe: InvokableToolModel = _EffortOverride(bound, UNHONOURED_EFFORT)
        if max_tokens is not None:
            effort_probe = _OutputCapOverride(effort_probe, max_tokens)
        invoke_with_retry(effort_probe, _PROBE_PROMPT)
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
