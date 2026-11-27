"""The Pinned Model and its real chat-model adapter.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (a ``models`` entry:
provider, model, endpoint, effort and ``api_key_env``) and
[ADR 0010](../../../docs/adr/0010-the-assistant-turn-is-carried-back-verbatim.md)
(the endpoint is part of the pin).

MVP row ids: L1-1, L1-4, L1-4a.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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

SAFETY_MARGIN_TOKENS = 1_000
"""The code-owned safety margin every pin's threshold must exceed (§2.1)."""

REQUEST_OVERHEAD_TOKENS = 1_000
"""The code-owned request overhead every pin's threshold must exceed (§2.1)."""

DEFAULT_MAX_ROUNDS = 8
"""The default round bound for one Advisor or Sub-agent run (§8, §9).

A pin that omits ``max-rounds`` answers every Advisor call and Sub-agent
run with this bound: the run ends ``no-report`` / ``round-limit`` where it
reaches the bound without a report.
"""

MAX_ROUNDS_CAP = 32
"""The code-owned maximum round bound (defect guard, §8, §9).

Four times the default: it covers the observed failure class (an Advisor
that needs roughly twice the default to read before it can report) while
catching typos such as 10000. The static validator refuses ``max-rounds``
above this cap; the Step Execution loop itself has no round bound.
"""


def _canonical_gateway_url(raw: str, subject: str, *, include_detail: bool) -> str:
    """Canonicalise a gateway URL, naming ``subject`` in every refusal.

    The shared rule behind a literal ``gateway.url`` and an env-sourced
    ``gateway.url-env`` value (contract §2.1): an ``http`` or ``https`` URL
    with a host, an optional port and an optional path. Userinfo, a query or
    a fragment is refused, as is any other scheme or a missing host. The
    canonical form lowercases the scheme and host and drops a trailing
    ``/``, so two spellings of one URL produce the same record.

    ``include_detail`` controls whether refusals quote the refused value and
    its parse detail: a literal URL refusal does (the value is the
    operator's own YAML text), while an env-sourced refusal never does (a
    refused value may carry userinfo, which must not reach a message, a log
    or the live status).
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        if include_detail:
            raise ValueError(f"{subject} does not parse: {exc}") from exc
        raise ValueError(f"{subject} does not parse") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"{subject} must use 'http' or 'https'")
    if parts.username or parts.password:
        raise ValueError(f"{subject} must not include userinfo")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"{subject} must include a host")
    try:
        port = parts.port
    except ValueError as exc:
        if include_detail:
            raise ValueError(f"{subject} has a bad port: {exc}") from exc
        raise ValueError(f"{subject} has a bad port") from None
    if parts.query:
        raise ValueError(f"{subject} must not include a query")
    if parts.fragment:
        raise ValueError(f"{subject} must not include a fragment")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def canonical_gateway_url(raw: str) -> str:
    """Canonicalise a gateway pin's ``gateway.url`` (contract §2.1).

    The URL is literal: an ``http`` or ``https`` URL with a host, an optional
    port and an optional path. Userinfo, a query or a fragment is refused, as
    is any other scheme or a missing host. The canonical form lowercases the
    scheme and host and drops a trailing ``/``, so two spellings of one URL
    produce the same record.
    """
    return _canonical_gateway_url(raw, f"gateway URL {raw!r}", include_detail=True)


def resolve_gateway_url(
    model_id: str, name: str, env: Mapping[str, str]
) -> str:
    """Resolve a gateway pin's ``gateway.url-env`` name to its canonical URL.

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4.1. The
    value is read once per Worker process at startup, before the Provider
    Capability Assertion, and passes the same rules as a literal
    ``gateway.url``. An unset, empty or malformed value raises ``ValueError``
    naming the variable and the rule it broke — never the value, which may
    carry userinfo. The caller turns this into a startup refusal with no
    Attempt opened.
    """
    if name not in env:
        raise ValueError(
            f"models.{model_id}.gateway.url-env: {name} is not set"
        )
    value = env[name]
    if not value:
        raise ValueError(
            f"models.{model_id}.gateway.url-env: {name} is empty"
        )
    return _canonical_gateway_url(
        value,
        f"models.{model_id}.gateway.url-env: {name}",
        include_detail=False,
    )


def _check_pin_token_fields(
    kind: str,
    output_cap_tokens: int | None,
    context_threshold_tokens: int | None,
    context_window_tokens: int | None,
) -> None:
    """The token-budget rules every pin obeys, direct or gateway (§2.1).

    All three fields travel together: each is a positive integer, the
    threshold exceeds the code-owned safety margin plus request overhead,
    and threshold plus output cap fits the Model Context Window. One error
    shape for every pin: the messages name the field, never the pin kind.
    """
    for field_name, value in (
        ("output-cap-tokens", output_cap_tokens),
        ("context-threshold-tokens", context_threshold_tokens),
        ("context-window-tokens", context_window_tokens),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(
                f"a {kind} pin's {field_name!r} must be a positive "
                f"integer, got {value!r}"
            )
    assert context_threshold_tokens is not None
    assert output_cap_tokens is not None
    assert context_window_tokens is not None
    if (
        context_threshold_tokens
        <= SAFETY_MARGIN_TOKENS + REQUEST_OVERHEAD_TOKENS
    ):
        raise ValueError(
            f"a {kind} pin's 'context-threshold-tokens' "
            f"({context_threshold_tokens}) must exceed the code-owned "
            f"safety margin ({SAFETY_MARGIN_TOKENS}) plus "
            f"request overhead ({REQUEST_OVERHEAD_TOKENS})"
        )
    if context_threshold_tokens + output_cap_tokens > context_window_tokens:
        raise ValueError(
            f"a {kind} pin's 'context-threshold-tokens' "
            f"({context_threshold_tokens}) plus 'output-cap-tokens' "
            f"({output_cap_tokens}) must not exceed 'context-window-tokens' "
            f"({context_window_tokens})"
        )


@dataclass(frozen=True)
class PinnedModel:
    """Provider, model id, endpoint and effort level, together as one unit.

    The endpoint is part of the pin, not a tuning constant beneath it
    (ADR 0010): the same model id on a different endpoint accepts a
    different set of requests. Deployment configuration, chosen once for
    the Worker — never per Target Issue (ADR 0009 part 2).

    Every pin carries its own token budget: the Model Context Window
    (``context_window_tokens`` — the window a request and its response
    share), the context-window threshold (``context_threshold_tokens``)
    and the per-request output cap (``output_cap_tokens``,
    ``max_tokens``). A gateway pin reaches its model through a Model
    Gateway instead of a provider: ``provider`` then holds the gateway
    kind (today only ``litellm``) and ``gateway_url`` the canonical
    gateway URL. The pin key is the label ``litellm:<model>@responses``
    on a gateway pin — the gateway kind, the gateway's own model name
    and the endpoint together.
    """

    provider: Provider | GatewayKind
    model: str
    endpoint: Endpoint
    effort: Effort
    gateway_url: str | None = None
    """The canonical gateway URL, set only on gateway pins."""

    output_cap_tokens: int | None = None
    """The per-request output cap (``max_tokens``), carried by every pin."""

    context_threshold_tokens: int | None = None
    """The context-window threshold, carried by every pin."""

    context_window_tokens: int | None = None
    """The Model Context Window, carried by every pin."""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    """The round bound for Advisor calls and Sub-agent runs this pin answers.

    A plain integer between 1 and ``MAX_ROUNDS_CAP`` (the code-owned
    maximum); the constructor refuses anything else, like the static
    validator refuses a bad ``max-rounds`` key. The bound is an additional
    stop, never an allowance: ceilings, allowances and the context
    threshold still end a run first.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("the pinned model name must be a non-empty string")
        if (
            type(self.max_rounds) is not int
            or self.max_rounds < 1
            or self.max_rounds > MAX_ROUNDS_CAP
        ):
            raise ValueError(
                f"the round bound must be an integer between 1 and "
                f"{MAX_ROUNDS_CAP}, got {self.max_rounds!r}"
            )
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
            _check_pin_token_fields(
                "gateway",
                self.output_cap_tokens,
                self.context_threshold_tokens,
                self.context_window_tokens,
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
        if (
            self.output_cap_tokens is None
            and self.context_threshold_tokens is None
            and self.context_window_tokens is None
        ):
            # The code-owned provider-to-endpoint table carries no token
            # budget: it admits one endpoint per provider only, and the model
            # is free-form. Every pin the Worker runs carries all three
            # fields (the validator proves it).
            return
        _check_pin_token_fields(
            "direct",
            self.output_cap_tokens,
            self.context_threshold_tokens,
            self.context_window_tokens,
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

    Every work node binds its tools with no forced `tool_choice` (`auto`):
    the model is asked to call a tool and either calls it or answers with
    text. A non-`None` `tool_choice` is used only by the diagnostic forced
    probe of the Provider Capability Assertion (contract §4.1 step 7).
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


_RESPONSES_EFFORT_HOOK_INSTALLED = False
"""Whether the Responses reasoning-effort hook is installed (below)."""


def _reported_reasoning_effort(response: object) -> str | None:
    """The effort a Responses body reports it ran at, or ``None``.

    Reads the parsed response's ``reasoning.effort`` — the provider's own
    echo of the effort the turn ran at — where it is a non-blank string.
    """
    reasoning = getattr(response, "reasoning", None)
    effort = getattr(reasoning, "effort", None)
    if isinstance(effort, str) and effort.strip() != "":
        return effort
    return None


def _install_responses_effort_hook() -> None:
    """Surface the Responses body's reported effort on every message.

    ``langchain-openai`` builds each Responses message's
    ``response_metadata`` from a fixed list of response fields that leaves
    out ``reasoning``, so the effort a response reports is invisible to the
    Worker — on direct ``openai:`` pins and on gateway pins alike. This
    wraps that one construction function (idempotently, once per process)
    to copy a reported ``reasoning.effort`` string into the message's
    ``response_metadata["reasoning_effort"]``, the way gateway headers are
    already exposed there. Where a response reports nothing the key stays
    absent. The Anthropic path builds its metadata elsewhere and is
    unaffected.

    Pinned to ``langchain-openai``'s construction seam: the adapter-level
    tests prove a reported effort reaches ``response_metadata`` from a
    recorded Responses body, and fail loudly if an upgrade moves the seam.
    """
    global _RESPONSES_EFFORT_HOOK_INSTALLED
    if _RESPONSES_EFFORT_HOOK_INSTALLED:
        return
    from langchain_openai.chat_models import base as _openai_base

    original = _openai_base._construct_lc_result_from_responses_api

    def _construct_with_reasoning_effort(
        response: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = original(response, *args, **kwargs)
        effort = _reported_reasoning_effort(response)
        if effort is not None:
            for generation in result.generations:
                generation.message.response_metadata["reasoning_effort"] = effort
        return result

    _construct_with_reasoning_effort.__name__ = original.__name__
    _construct_with_reasoning_effort.__doc__ = original.__doc__
    _openai_base._construct_lc_result_from_responses_api = (
        _construct_with_reasoning_effort
    )
    _RESPONSES_EFFORT_HOOK_INSTALLED = True


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
    message, for the gateway identity assertion (§9.1).

    A direct ``openai`` pin builds the OpenAI chat model on the Responses
    API with the pinned effort, ``store: False`` and
    ``include: ["reasoning.encrypted_content"]`` — the same self-contained
    turn as a gateway pin (ADR 0010): the encrypted reasoning travels
    inside the turn and comes back byte-equal instead of referring to
    provider-side state.

    Every gateway request carries the per-request suppression
    (contract §9.1): ``disable_fallbacks: True`` and ``num_retries: 0``,
    sent through ``extra_body`` so the OpenAI client forwards them as
    top-level fields. The SDK's own retry is disabled (``max_retries=0``):
    the Worker's own provider retry is then the only retry, and each of
    its tries passes Admission and counts against the Attempt Ceiling.

    Every Responses message the adapter builds — direct or through a
    gateway — carries the effort the response body reports
    (``response_metadata["reasoning_effort"]``), so the Provider
    Capability Assertion and every Attempt response assert the pinned
    effort from the response's own evidence (§9.1).

    ``transport`` is the test seam: an injected ``httpx`` transport the
    gateway model sends on. Production passes none and uses the default.
    """
    _install_responses_effort_hook()
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
            "extra_body": {"disable_fallbacks": True, "num_retries": 0},
            "max_retries": 0,
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
        kwargs["store"] = False
        kwargs["include"] = ["reasoning.encrypted_content"]
    return cast(
        InvokableToolModel, init_chat_model(f"{pin.provider}:{pin.model}", **kwargs)
    )
