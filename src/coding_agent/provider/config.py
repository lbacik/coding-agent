from __future__ import annotations

from coding_agent.provider.effective_token_ceiling import EffectiveTokenCeilingTable
from coding_agent.provider.pinned_model import PinnedModel
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

# The Pinned Prefix compaction threshold (`L3-IMP-13`), one entry per Pinned
# Model rather than a single global number -- contract §5 names it among the
# ceilings that rule binds, alongside cost and the overall token budget. A
# deployer raises or lowers a pin's entry the same way they would its Price
# Table row; a pin absent here refuses to compose a Pinned Prefix for it
# (`coding_agent.runtime.pinned_prefix.assert_within_compaction_threshold`).
DEFAULT_COMPACTION_THRESHOLDS: dict[str, int] = {
    PINNED_MODELS["anthropic"].key: 50_000,
    PINNED_MODELS["openai"].key: 100_000,
}

# The effective-work budget is per pin. It limits fresh input (including
# cache writes) and output; cache reads still count toward the independent
# dollar ceiling at their real provider price.
DEFAULT_EFFECTIVE_TOKEN_CEILINGS: EffectiveTokenCeilingTable = {
    PINNED_MODELS["anthropic"].key: 400_000,
    PINNED_MODELS["openai"].key: 500_000,
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


def output_cap_for(pin: PinnedModel) -> int | None:
    """The code-owned output cap for ``pin``, or ``None`` outside the table."""
    return DEFAULT_OUTPUT_CAPS.get(pin.key)


def context_window_for_pin(pin: PinnedModel) -> int | None:
    """The code-owned context-window threshold for ``pin``, if pinned."""
    window = DEFAULT_CONTEXT_WINDOWS.get(pin.key)
    return window.threshold_tokens if window is not None else None
