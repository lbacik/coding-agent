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

# `implement.ceilings.DEFAULT_LOOP_CEILINGS` and `implement.result_capping
# .DEFAULT_RESULT_CAP_LIMIT` are the tool loop's own equivalents of the two
# tables above, kept in `implement` rather than here: `implement` already
# depends on `provider` (a Pinned Model, a Price Table entry), so a type
# from `implement` imported back into this module would cycle through
# `coding_agent.provider`'s own `__init__` the same way `CompactionThreshold
# Table` deliberately isn't imported here either -- this module states its
# shape as a plain `dict[str, int]` instead, for the same reason.
