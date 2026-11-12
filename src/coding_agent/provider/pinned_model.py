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

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

Provider = Literal["anthropic", "openai"]
Endpoint = Literal["messages", "responses"]
Effort = Literal["low", "medium", "high"]
GatewayKind = Literal["litellm"]
"""The gateway kind a gateway pin may name (contract §2.1).

Closed and code-owned: only ``litellm`` exists until a later ticket adds a
kind together with its adapter.
"""

ENDPOINTS: tuple[Endpoint, ...] = ("messages", "responses")
"""The shared protocol vocabulary every pin's endpoint comes from.

A per-provider table (``PROVIDER_ENDPOINTS``) — and a per-gateway-kind table
(``GATEWAY_ENDPOINTS``) — admits one member of this vocabulary per provider
or gateway kind.
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

GATEWAY_KINDS: tuple[GatewayKind, ...] = ("litellm",)
"""The admissible gateway kinds (contract §2.1)."""

GATEWAY_ENDPOINTS: dict[GatewayKind, Endpoint] = {
    "litellm": "responses",
}
"""The admissible endpoint per gateway kind, drawn from ``ENDPOINTS``."""

DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS = 1_000
"""The code-owned safety margin every gateway pin's threshold must exceed."""

DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS = 1_000
"""The code-owned request overhead every gateway pin's threshold must exceed."""

DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP = 2_000
"""The code-owned handoff node output cap behind every gateway pin."""


def canonical_gateway_url(raw: str) -> str:
    """Canonicalise a gateway pin's ``gateway.url`` (contract §2.1).

    The URL is literal: an ``http`` or ``https`` URL with a host, an optional
    port and an optional path. Userinfo, a query or a fragment is refused, as
    is any other scheme or a missing host. The canonical form lowercases the
    scheme and host and drops a trailing ``/``, so two spellings of one URL
    produce the same record.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ValueError(f"gateway URL {raw!r} does not parse: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"gateway URL {raw!r} must use 'http' or 'https'")
    if parts.username or parts.password:
        raise ValueError(f"gateway URL {raw!r} must not include userinfo")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"gateway URL {raw!r} must include a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"gateway URL {raw!r} has a bad port: {exc}") from exc
    if parts.query:
        raise ValueError(f"gateway URL {raw!r} must not include a query")
    if parts.fragment:
        raise ValueError(f"gateway URL {raw!r} must not include a fragment")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


@dataclass(frozen=True)
class PinnedModel:
    """Provider, model id, endpoint and effort level, together as one unit.

    The endpoint is part of the pin, not a tuning constant beneath it
    (ADR 0010): the same model id on a different endpoint accepts a
    different set of requests. Deployment configuration, chosen once for
    the Worker — never per Target Issue (ADR 0009 part 2).

    A gateway pin reaches its model through a Model Gateway instead of a
    provider: ``provider`` then holds the gateway kind (today only
    ``litellm``), ``gateway_url`` the canonical gateway URL, and the pin
    carries its own output cap and context-window threshold. The pin key is
    the label ``litellm:<model>@responses`` — the gateway kind, the
    gateway's own model name and the endpoint together.
    """

    provider: Provider | GatewayKind
    model: str
    endpoint: Endpoint
    effort: Effort
    gateway_url: str | None = None
    """The canonical gateway URL, set only on gateway pins."""

    output_cap_tokens: int | None = None
    """The per-request output cap (``max_tokens``), carried by gateway pins."""

    context_threshold_tokens: int | None = None
    """The context-window threshold, carried by gateway pins."""

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("the pinned model name must be a non-empty string")
        if self.gateway_url is not None:
            if self.provider not in GATEWAY_KINDS:
                raise ValueError(
                    f"a gateway pin's provider {self.provider!r} must be a "
                    f"gateway kind (one of {', '.join(GATEWAY_KINDS)})"
                )
            required = GATEWAY_ENDPOINTS[self.provider]
            if self.endpoint != required:
                raise ValueError(
                    f"{self.provider} gateway pins use the {required!r} endpoint, "
                    f"got {self.endpoint!r}"
                )
            object.__setattr__(self, "gateway_url", canonical_gateway_url(self.gateway_url))
            for field_name, value in (
                ("output-cap-tokens", self.output_cap_tokens),
                ("context-threshold-tokens", self.context_threshold_tokens),
            ):
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 1
                ):
                    raise ValueError(
                        f"a gateway pin's {field_name!r} must be a positive "
                        f"integer, got {value!r}"
                    )
            assert self.context_threshold_tokens is not None
            if (
                self.context_threshold_tokens
                <= DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS
                + DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS
            ):
                raise ValueError(
                    f"a gateway pin's 'context-threshold-tokens' "
                    f"({self.context_threshold_tokens}) must exceed the code-owned "
                    f"safety margin ({DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS}) plus "
                    f"request overhead ({DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS})"
                )
            return
        if self.provider not in PROVIDER_ENDPOINTS:
            raise ValueError(
                f"unknown provider {self.provider!r} "
                f"(one of {', '.join(sorted(PROVIDER_ENDPOINTS))}, "
                f"or a gateway kind with 'gateway.url' set)"
            )
        required = PROVIDER_ENDPOINTS[self.provider]
        if self.endpoint != required:
            raise ValueError(
                f"{self.provider} is pinned to the {required!r} endpoint (the one "
                f"that accepts tools together with a pinned effort), got {self.endpoint!r}"
            )
        if self.output_cap_tokens is not None or self.context_threshold_tokens is not None:
            raise ValueError(
                "a direct 'provider' pin must not carry 'output-cap-tokens' or "
                "'context-threshold-tokens': those are gateway-pin fields"
            )

    @property
    def is_gateway(self) -> bool:
        """Whether this pin reaches its model through a Model Gateway."""
        return self.gateway_url is not None

    @property
    def key(self) -> str:
        """The Price Table key: provider (or gateway kind), model and endpoint."""
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


GATEWAY_BASE_PATHS: dict[GatewayKind, str] = {
    "litellm": "/v1",
}
"""The base path per gateway kind, drawn from the gateway's own URL space.

The endpoint is code-owned per gateway kind (ADR 0010): the adapter appends
this path to the pin's gateway URL, so the pin names the gateway and code
names where that gateway serves the Responses API.
"""


def gateway_base_url(pin: PinnedModel) -> str:
    """The gateway's Responses base URL: the pin's URL plus the kind's path.

    A pin whose URL already ends in the kind's path keeps it as is, so one
    gateway has one base URL however the operator spelled it.
    """
    assert pin.gateway_url is not None
    base_path = GATEWAY_BASE_PATHS[cast(GatewayKind, pin.provider)]
    base = pin.gateway_url.rstrip("/")
    if base.endswith(base_path):
        return base
    return f"{base}{base_path}"


def build_chat_model(
    pin: PinnedModel,
    *,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> InvokableToolModel:
    """The real adapter. `init_chat_model` owns provider dispatch; forcing
    the OpenAI pin onto the Responses API is the one place the endpoint
    choice actually reaches the wire (ADR 0010).

    ``api_key`` is the value of the Pinned Model's ``api_key_env`` variable
    (contract §2.1): passed explicitly so a configured name other than the
    provider SDK's default variable is the credential the call uses. ``None``
    leaves the SDK's own default lookup in place (the pre-M5 CLI path).

    A gateway pin builds the OpenAI chat model on the Responses API against
    the gateway's ``/v1`` with the gateway key, the pinned effort, the pin's
    output cap on every request, ``store: False`` and
    ``include: ["reasoning.encrypted_content"]`` — the encrypted reasoning
    then travels inside the turn and comes back byte-equal instead of
    referring to server-side state — and response headers exposed on each
    message, for the gateway identity ticket.

    ``transport`` is the test seam: an injected ``httpx`` transport the
    gateway model sends on. Production passes none and uses the default.
    """
    if pin.gateway_url is not None:
        from langchain_openai import ChatOpenAI

        if pin.provider not in GATEWAY_KINDS:
            raise ValueError(
                f"unknown gateway kind {pin.provider!r} "
                f"(one of {', '.join(GATEWAY_KINDS)})"
            )
        if GATEWAY_ENDPOINTS[pin.provider] != pin.endpoint:
            raise ValueError(
                f"{pin.provider} gateway pins use the "
                f"{GATEWAY_ENDPOINTS[pin.provider]!r} endpoint, "
                f"got {pin.endpoint!r}"
            )
        if pin.output_cap_tokens is None:
            raise ValueError(
                f"gateway pin {pin.key!r} carries no output-cap-tokens"
            )
        gateway_kwargs: dict[str, Any] = {
            "model": pin.model,
            "base_url": gateway_base_url(pin),
            "reasoning_effort": pin.effort,
            "max_tokens": pin.output_cap_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "include_response_headers": True,
            "use_responses_api": True,
        }
        if api_key is not None:
            gateway_kwargs["api_key"] = api_key
        if transport is not None:
            gateway_kwargs["http_client"] = httpx.Client(transport=transport)
        return cast(InvokableToolModel, ChatOpenAI(**gateway_kwargs))
    if transport is not None:
        raise ValueError(
            "a direct pin takes no injected transport: "
            "the transport seam is gateway-only"
        )
    kwargs: dict[str, Any] = {"reasoning_effort": pin.effort}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if pin.provider == "openai":
        kwargs["use_responses_api"] = True
    return cast(
        InvokableToolModel, init_chat_model(f"{pin.provider}:{pin.model}", **kwargs)
    )
