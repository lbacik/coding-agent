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
