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

# The endpoint every provider's pin is forced onto — the one that accepts
# tools together with a pinned effort. OpenAI's default endpoint
# (chat completions) 400s on that combination; Anthropic has one endpoint.
_REQUIRED_ENDPOINT: dict[Provider, Endpoint] = {
    "anthropic": "messages",
    "openai": "responses",
}


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
        required = _REQUIRED_ENDPOINT[self.provider]
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
    """

    def bind_tools(self, tools: Sequence[BaseTool]) -> InvokableToolModel: ...

    def invoke(self, input: LanguageModelInput) -> BaseMessage: ...


def build_chat_model(pin: PinnedModel) -> InvokableToolModel:
    """The real adapter. `init_chat_model` owns provider dispatch; forcing
    the OpenAI pin onto the Responses API is the one place the endpoint
    choice actually reaches the wire (ADR 0010)."""
    kwargs: dict[str, Any] = {"reasoning_effort": pin.effort}
    if pin.provider == "openai":
        kwargs["use_responses_api"] = True
    return cast(
        InvokableToolModel, init_chat_model(f"{pin.provider}:{pin.model}", **kwargs)
    )
