from __future__ import annotations

from dataclasses import dataclass

from coding_agent.provider.pinned_model import (
    DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP,
    DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS,
    DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS,
    PinnedModel,
)
from coding_agent.provider.price_table import PriceTable, TokenPrices

PINNED_MODELS: dict[str, PinnedModel] = {
    "anthropic": PinnedModel(
        provider="anthropic", model="claude-sonnet-5", endpoint="messages", effort="medium"
    ),
    "openai": PinnedModel(
        provider="openai", model="gpt-5.6-luna", endpoint="responses", effort="medium"
    ),
}

# Dollars per million tokens, public list pricing as of 2026-09-14. A pin
# absent here refuses to start (ADR 0009) rather than costing silently; a
# deployer updates this table when a provider's rates change.
DEFAULT_PRICE_TABLE: PriceTable = {
    PINNED_MODELS["anthropic"].key: TokenPrices(
        input=2.00, output=10.00, cache_read=0.20, cache_write=2.50
    ),
    PINNED_MODELS["openai"].key: TokenPrices(
        input=0.20, output=1.20, cache_read=0.02, cache_write=0.25
    ),
}

# The deterministic context policy per Pinned Model (contract §11,
# ADR 0013): the estimated threshold, request overhead, safety margin and
# the handoff node's own output cap. The estimate includes the Pinned
# Prefix, whole Exchange Units and bound tool schemas; provider-reported
# usage is recorded separately and never substituted for this signal.
#
# MVP row ids: L3-IMP-4, L3-IMP-13, L3-IMP-23.
from coding_agent.runtime.context_handoff import (
    ContextWindowConfig,
    ContextWindowTable,
)

DEFAULT_CONTEXT_WINDOWS: ContextWindowTable = {
    PINNED_MODELS["anthropic"].key: ContextWindowConfig(
        threshold_tokens=50_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
        handoff_token_cap=2_000,
    ),
    PINNED_MODELS["openai"].key: ContextWindowConfig(
        threshold_tokens=100_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
        handoff_token_cap=2_000,
    ),
}

# The code-owned per-request output cap (``max_tokens``) per Pinned Model.
#
# Step-workflow contract §2.1, §2.10, §11.4: the Pinned Model's context
# window and output cap stay in code, in a code-owned table keyed by
# (provider, model) beside the prices' meaning. A pair outside this table
# is refused; the cap is sent with every request, is part of the Effective
# Configuration digest, and the Provider Capability Assertion runs with it
# set (matrix row STEP-17).
DEFAULT_OUTPUT_CAPS: dict[str, int] = {
    PINNED_MODELS["anthropic"].key: 32000,
    PINNED_MODELS["openai"].key: 32000,
}


@dataclass(frozen=True)
class PinPolicy:
    """The code-owned per-pin values one pin resolves to.

    Step-workflow contract §2.1, §2.10, §11.4: a direct Pinned Model's
    context window and output cap stay in code. This is the single per-pin
    lookup every reader uses — keyed on the pin, never by indexing the
    code-owned tables directly. Direct pins resolve from the code-owned
    tables keyed by (provider, model); gateway pins resolve their output cap
    and context-window threshold from the pin itself, with the safety margin,
    request overhead and handoff cap staying code-owned defaults.
    """

    output_cap: int | None
    """The per-request output cap (``max_tokens``), or ``None`` outside the table."""

    context_window: ContextWindowConfig | None
    """The deterministic context policy, or ``None`` outside the table."""


def policy_for_pin(pin: PinnedModel) -> PinPolicy:
    """The code-owned policy for ``pin``.

    A direct pair outside the code-owned tables yields ``None`` members; the
    caller refuses with the pin's key, the cap is sent with every request, is
    part of the Effective Configuration digest, and the Provider Capability
    Assertion runs with it set (matrix row STEP-17). A gateway pin resolves
    its output cap and context-window threshold from the pin itself; the
    safety margin, request overhead and handoff cap stay code-owned defaults.
    The code-owned per-pin tables never hold gateway keys.
    """
    if pin.is_gateway:
        if pin.output_cap_tokens is None or pin.context_threshold_tokens is None:
            return PinPolicy(output_cap=None, context_window=None)
        try:
            context_window = ContextWindowConfig(
                threshold_tokens=pin.context_threshold_tokens,
                safety_margin_tokens=DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS,
                request_overhead_tokens=DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS,
                handoff_token_cap=DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP,
            )
        except ValueError:
            return PinPolicy(output_cap=pin.output_cap_tokens, context_window=None)
        return PinPolicy(
            output_cap=pin.output_cap_tokens, context_window=context_window
        )
    return PinPolicy(
        output_cap=DEFAULT_OUTPUT_CAPS.get(pin.key),
        context_window=DEFAULT_CONTEXT_WINDOWS.get(pin.key),
    )
