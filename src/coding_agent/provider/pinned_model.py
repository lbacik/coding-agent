"""The Pinned Model and its real chat-model adapter.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (a ``models`` entry:
provider, model, endpoint, effort and ``api_key_env``) and
[ADR 0010](../../../docs/adr/0010-the-assistant-turn-is-carried-back-verbatim.md)
(the endpoint is part of the pin).

MVP row ids: L1-1, L1-4, L1-4a.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from langchain.chat_models import init_chat_model
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

Provider = Literal["anthropic", "openai"]
Endpoint = Literal["messages", "responses"]
Effort = Literal["low", "medium", "high"]

ENDPOINTS: tuple[Endpoint, ...] = ("messages", "responses")
"""The shared protocol vocabulary every pin's endpoint comes from.

A per-provider table (``PROVIDER_ENDPOINTS``) — and later a per-gateway-kind
table — admits one member of this vocabulary per provider or gateway kind.
"""

PROVIDER_ENDPOINTS: dict[Provider, Endpoint] = {
    "anthropic": "messages",
    "openai": "responses",
}
"""The admissible endpoint per provider, drawn from ``ENDPOINTS``.

The endpoint is the one that accepts tools together with a pinned effort.
OpenAI's default endpoint (chat completions) 400s on that combination;
Anthropic has one endpoint.
"""


@dataclass(frozen=True)
class PinnedModel:
    """Provider, model id, endpoint and effort level, together as one unit.

    The endpoint is part of the pin, not a tuning constant beneath it
    (ADR 0010): the same model id on a different endpoint accepts a
    different set of requests. Deployment configuration, chosen once for
    the Worker — never per Target Issue (ADR 0009 part 2).
    """

    provider: Provider
    model: str
    endpoint: Endpoint
    effort: Effort

    def __post_init__(self) -> None:
        required = PROVIDER_ENDPOINTS[self.provider]
        if self.endpoint != required:
            raise ValueError(
                f"{self.provider} is pinned to the {required!r} endpoint (the one "
                f"that accepts tools together with a pinned effort), got {self.endpoint!r}"
            )

    @property
    def key(self) -> str:
        """The Price Table key: provider, model and endpoint together."""
        return f"{self.provider}:{self.model}@{self.endpoint}"


class InvokableToolModel(Protocol):
    """A port: bind a toolset, then invoke for a single assistant turn.

    The real adapter is `build_chat_model`, via `init_chat_model`. Tests
    stand in a fake satisfying this same shape. Whatever `invoke` returns is
    asserted on verbatim by the caller, never rebuilt (ADR 0010).

    `tool_choice` forces the next request's activation call (contract §8):
    the step's first request names `activate_implement` (or `activate_step`)
    and the model must answer with that call.
    """

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> InvokableToolModel: ...

    def invoke(self, input: LanguageModelInput) -> BaseMessage: ...


def build_chat_model(pin: PinnedModel, *, api_key: str | None = None) -> InvokableToolModel:
    """The real adapter. `init_chat_model` owns provider dispatch; forcing
    the OpenAI pin onto the Responses API is the one place the endpoint
    choice actually reaches the wire (ADR 0010).

    ``api_key`` is the value of the Pinned Model's ``api_key_env`` variable
    (contract §2.1): passed explicitly so a configured name other than the
    provider SDK's default variable is the credential the call uses. ``None``
    leaves the SDK's own default lookup in place (the pre-M5 CLI path).
    """
    kwargs: dict[str, Any] = {"reasoning_effort": pin.effort}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if pin.provider == "openai":
        kwargs["use_responses_api"] = True
    return cast(
        InvokableToolModel, init_chat_model(f"{pin.provider}:{pin.model}", **kwargs)
    )
