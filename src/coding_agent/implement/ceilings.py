from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from langchain_core.messages import UsageMetadata

from coding_agent.provider.price_table import TokenPrices


@dataclass(frozen=True)
class LoopCeilings:
    """Every ceiling contract §5 names for a work node's tool loop, each
    independently optional: a `None` field is never enforced. Concrete
    values are tuning constants set against observed runs, the same as
    the compaction threshold and the Price Table — not part of the
    contract itself, and configured the same way a deployer configures
    those."""

    max_wall_clock_seconds: float | None = None
    max_cost_usd: float | None = None
    max_tokens: int | None = None
    max_tool_calls: int | None = None


@dataclass(frozen=True)
class UsageTotals:
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0


class UsageLedger(Protocol):
    """The Run Ledger's stand-in (`L3-IMP-6`, `L3-IMP-8`): every model
    response and every tool call is flushed here the instant it happens,
    never batched — a re-entered node adds its own deltas on top of
    whatever is already here rather than recomputing a total from the
    conversation, which is what makes a restart's usage additive, never
    double-counted."""

    @property
    def totals(self) -> UsageTotals: ...

    def flush_model_response(
        self, usage_metadata: UsageMetadata | None, price: TokenPrices
    ) -> UsageTotals: ...

    def flush_tool_call(self) -> UsageTotals: ...


@dataclass(frozen=True)
class UsageBreakdown:
    """One model response's `usage_metadata`, split into the buckets a
    provider prices separately -- pulled out of `_usage_delta` so
    `implement.loop` can report the cache split (how much of the prompt
    was a cache hit, a cache write, or neither) without recomputing it,
    the same numbers `_usage_delta` prices from."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        """`input_tokens` is the whole prompt including cache hits -- the
        uncached remainder is what prices at the plain input rate, a cache
        read and a cache write each pricing at their own bucket instead."""
        return max(0, self.input_tokens - self.cache_read_tokens - self.cache_write_tokens)


def usage_breakdown(usage_metadata: UsageMetadata | None) -> UsageBreakdown:
    """`usage_metadata` read out into `UsageBreakdown`, or every field zero
    where a turn carried none (never a model call, or a provider that
    reports nothing back)."""
    if usage_metadata is None:
        return UsageBreakdown()
    input_tokens = usage_metadata.get("input_tokens", 0)
    output_tokens = usage_metadata.get("output_tokens", 0)
    details = usage_metadata.get("input_token_details") or {}
    cache_read = details.get("cache_read", 0)
    cache_write = details.get("cache_creation", 0)
    total = usage_metadata.get("total_tokens", input_tokens + output_tokens)
    return UsageBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=total,
    )


def _usage_delta(usage_metadata: UsageMetadata | None, price: TokenPrices) -> tuple[int, float]:
    """Tokens and dollars for one model response — see `UsageBreakdown` for
    why the uncached remainder, not `input_tokens` itself, prices at the
    plain input rate."""
    breakdown = usage_breakdown(usage_metadata)
    cost = (
        breakdown.uncached_input_tokens / 1_000_000 * price.input
        + breakdown.output_tokens / 1_000_000 * price.output
        + breakdown.cache_read_tokens / 1_000_000 * price.cache_read
        + breakdown.cache_write_tokens / 1_000_000 * price.cache_write
    )
    return breakdown.total_tokens, cost


class InMemoryUsageLedger:
    """The only `UsageLedger` this codebase has today — a process-local
    stand-in for the Run Ledger, which does not exist yet (`implement.
    attempt` has no source to read an Attempt count from either, for the
    same reason). A caller simulating a restart passes the same instance
    back in rather than constructing a fresh one: nothing here ever
    recomputes a total from the conversation, it only ever adds the one
    delta it was just handed, so replaying is additive rather than a
    reset — `L3-IMP-8`."""

    def __init__(self, initial: UsageTotals | None = None) -> None:
        self._totals = initial if initial is not None else UsageTotals()

    @property
    def totals(self) -> UsageTotals:
        return self._totals

    def flush_model_response(
        self, usage_metadata: UsageMetadata | None, price: TokenPrices
    ) -> UsageTotals:
        tokens, cost = _usage_delta(usage_metadata, price)
        self._totals = replace(
            self._totals,
            tokens=self._totals.tokens + tokens,
            cost_usd=self._totals.cost_usd + cost,
        )
        return self._totals

    def flush_tool_call(self) -> UsageTotals:
        self._totals = replace(self._totals, tool_calls=self._totals.tool_calls + 1)
        return self._totals


# Contract §5's ceilings -- wall clock, cost, tokens, tool calls -- for the
# implementer's tool loop. Tuning constants set against observed runs, not
# part of the contract itself; a deployer raises or lowers them the same
# way they would `provider.config.DEFAULT_PRICE_TABLE`.
DEFAULT_LOOP_CEILINGS = LoopCeilings(
    max_wall_clock_seconds=3_600,
    max_cost_usd=5.0,
    max_tokens=400_000,
    max_tool_calls=200,
)


def ceiling_crossed(totals: UsageTotals, elapsed_seconds: float, ceilings: LoopCeilings) -> str | None:
    """`L3-IMP-6`: the name of the first ceiling `totals`/`elapsed_seconds`
    are over — checked in the fixed order wall clock, cost, tokens, tool
    calls — or `None` where every ceiling `ceilings` configures still has
    headroom."""
    if ceilings.max_wall_clock_seconds is not None and elapsed_seconds > ceilings.max_wall_clock_seconds:
        return "wall_clock"
    if ceilings.max_cost_usd is not None and totals.cost_usd > ceilings.max_cost_usd:
        return "cost"
    if ceilings.max_tokens is not None and totals.tokens > ceilings.max_tokens:
        return "tokens"
    if ceilings.max_tool_calls is not None and totals.tool_calls > ceilings.max_tool_calls:
        return "tool_calls"
    return None
