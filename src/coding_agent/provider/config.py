from __future__ import annotations

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

# The Pinned Prefix compaction threshold (`L3-IMP-13`), one entry per Pinned
# Model rather than a single global number -- contract §5 names it among the
# ceilings that rule binds, alongside cost and the overall token budget. A
# deployer raises or lowers a pin's entry the same way they would its Price
# Table row; a pin absent here refuses to compose a Pinned Prefix for it
# (`coding_agent.implement.pinned_prefix.assert_within_compaction_threshold`).
DEFAULT_COMPACTION_THRESHOLDS: dict[str, int] = {
    PINNED_MODELS["anthropic"].key: 50_000,
    PINNED_MODELS["openai"].key: 50_000,
}
