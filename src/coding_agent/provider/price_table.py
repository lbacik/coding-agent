from __future__ import annotations

from dataclasses import dataclass

from coding_agent.provider.pinned_model import PinnedModel


@dataclass(frozen=True)
class TokenPrices:
    """Dollars per million tokens, one bucket at a time.

    Never a single input/output pair (ADR 0009): a cache read and a cache
    write are separate buckets from an uncached token — `input_tokens` is
    the whole prompt including cache hits, and a cache write may land in a
    provider-specific field entirely.
    """

    input: float
    output: float
    cache_read: float
    cache_write: float


PriceTable = dict[str, TokenPrices]


def price_for(table: PriceTable, pin: PinnedModel) -> TokenPrices | None:
    return table.get(pin.key)
