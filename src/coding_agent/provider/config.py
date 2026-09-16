from __future__ import annotations

from coding_agent.provider.effective_token_ceiling import EffectiveTokenCeilingTable
from coding_agent.implement.context_handoff import ContextWindowConfig, ContextWindowTable
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import PriceTable, TokenPrices

PINNED_MODELS: dict[str, PinnedModel] = {
    "anthropic": PinnedModel(
        provider="anthropic", model="claude-sonnet-5", endpoint="messages", effort="medium"
    ),
    "openai": PinnedModel(
        provider="openai", model="gpt-5.6-terra", endpoint="responses", effort="medium"
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
        input=2.00, output=12.00, cache_read=0.20, cache_write=0.00
    ),
}

# The context-window handoff policy is per Pinned Model. The local estimate is
# kept below the provider's window by overhead and a safety margin; crossing
# the usable threshold hands the Attempt to a fresh node. Values are tuning
# constants, while the shape and accounting rules are part of the contract.
DEFAULT_CONTEXT_WINDOWS: ContextWindowTable = {
    PINNED_MODELS["anthropic"].key: ContextWindowConfig(
        threshold_tokens=50_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
        handoff_token_cap=2_000,
    ),
    PINNED_MODELS["openai"].key: ContextWindowConfig(
        threshold_tokens=50_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
        handoff_token_cap=2_000,
    ),
}

# Compatibility for callers from the pre-handoff S3 slice. Runtime Attempts
# receive DEFAULT_CONTEXT_WINDOWS; this alias is only an integer view for old
# integrations and tests that have not migrated their configuration yet.
DEFAULT_COMPACTION_THRESHOLDS: dict[str, int] = {
    key: value.threshold_tokens for key, value in DEFAULT_CONTEXT_WINDOWS.items()
}

# The effective-work budget is per pin. It limits fresh input (including
# cache writes) and output; cache reads still count toward the independent
# dollar ceiling at their real provider price.
DEFAULT_EFFECTIVE_TOKEN_CEILINGS: EffectiveTokenCeilingTable = {
    PINNED_MODELS["anthropic"].key: 400_000,
    PINNED_MODELS["openai"].key: 400_000,
}

# `implement.ceilings.DEFAULT_LOOP_CEILINGS` and
# `implement.result_capping.DEFAULT_RESULT_CAP_LIMIT` are the tool loop's
# remaining global tuning constants. This module keeps per-Pinned-Model
# tables here alongside prices and compaction thresholds without importing
# from `implement`, which would create a provider/import cycle.
