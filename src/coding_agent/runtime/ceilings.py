"""Attempt-wide meters: usage, cost per token bucket, and the ceilings.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Meters": usage is
counted after every model response and flushed to the Run Ledger at that
moment; cost is computed per token bucket from the Effective Configuration's
prices, because ``input_tokens`` includes cache hits and a cache write may
report in a provider-specific field; a crossed ceiling stops the loop inside
the node with usage flushed, then a deterministic finalization commits and
pushes with no final checks, ending ``failed-limit`` naming the limit) and
§14 (the Implementation Outcome → Terminal Outcome table) and §15 (the
terminal comment carries the limit crossed, the spend and the Continuation
Snapshot).

MVP row ids: L1-3, L3-IMP-6, L3-IMP-8, C-LIM-1, P-LIM-1, L3-IMP-7.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol, cast

from langchain_core.messages import AIMessage, BaseMessage, UsageMetadata

from coding_agent.provider.price_table import TokenPrices

# Anthropic's TTL-specific cache-write buckets. When either is non-zero the
# adapter zeroes the generic ``cache_creation`` (to avoid double counting it),
# so reading ``cache_creation`` alone prices every Anthropic cache write at
# nothing — ADR 0009's 0.8x under-charge.
_ANTHROPIC_CACHE_WRITE_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


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
    max_effective_tokens: int | None = None
    max_tool_calls: int | None = None


@dataclass(frozen=True)
class UsageTotals:
    """Cumulative provider-reported throughput, effective model work, cost,
    and tool calls for one Attempt.

    `tokens` preserves the provider's raw cache-inclusive total for audit and
    diagnostics. `effective_tokens` excludes prompt-cache reads, which are
    repeated context rather than new model work; they remain included in
    `cost_usd` at their provider-specific rate.

    The per-bucket token and dollar fields are the cumulative readable spend
    record the M7 hard-ceiling finalization flushes to the Run Ledger
    (contract §11, §15): an operator reads the spend directly from them
    instead of computing limit-minus-remaining.
    """

    tokens: int = 0
    effective_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_input_usd: float = 0.0
    cost_output_usd: float = 0.0
    cost_cache_read_usd: float = 0.0
    cost_cache_write_usd: float = 0.0

    def __add__(self, other: UsageTotals) -> UsageTotals:
        """Fold one more delta in: every meter, cost included, adds."""
        return UsageTotals(
            tokens=self.tokens + other.tokens,
            effective_tokens=self.effective_tokens + other.effective_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_input_usd=self.cost_input_usd + other.cost_input_usd,
            cost_output_usd=self.cost_output_usd + other.cost_output_usd,
            cost_cache_read_usd=self.cost_cache_read_usd + other.cost_cache_read_usd,
            cost_cache_write_usd=self.cost_cache_write_usd + other.cost_cache_write_usd,
        )

    def as_readable(self) -> dict[str, object]:
        """The flush payload: cumulative totals plus per-bucket tokens and dollars."""
        return {
            "tokens": self.tokens,
            "effective_tokens": self.effective_tokens,
            "cost_usd": self.cost_usd,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_input_usd": self.cost_input_usd,
            "cost_output_usd": self.cost_output_usd,
            "cost_cache_read_usd": self.cost_cache_read_usd,
            "cost_cache_write_usd": self.cost_cache_write_usd,
        }


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
    provider prices separately -- pulled out of `_usage_delta` so the
    step's bounded context can report the cache split (how much of the
    prompt was a cache hit, a cache write, or neither) without
    recomputing it, the same numbers `_usage_delta` prices from."""

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

    @property
    def effective_tokens(self) -> int:
        """Fresh input (including cache writes) plus output.

        A cache read is already-present prompt context and therefore does not
        consume the Attempt's effective-work budget a second time.
        """
        return max(0, self.input_tokens - self.cache_read_tokens) + self.output_tokens


def usage_breakdown(usage_metadata: UsageMetadata | None) -> UsageBreakdown:
    """`usage_metadata` read out into `UsageBreakdown`, or every field zero
    where a turn carried none (never a model call, or a provider that
    reports nothing back).

    Reasoning tokens stay inside the output count: every provider path
    reports them as part of ``output_tokens`` (the Responses shape carries
    the split in ``output_token_details`` for information only), so they
    price at the ``output`` rate with no separate bucket (§11.4). An
    absent cache-read or cache-write detail field counts as zero.
    """
    if usage_metadata is None:
        return UsageBreakdown()
    usage = cast(Mapping[str, object], usage_metadata)
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    details = cast(Mapping[str, object], usage.get("input_token_details") or {})
    cache_read = _count(details.get("cache_read"))
    # A provider-specific cache-write field wins over the generic one: an
    # Anthropic cache write reports in ``ephemeral_5m_input_tokens`` while
    # ``cache_creation`` stays zero (ADR 0009, L1-3).
    specific_write = sum(_count(details.get(key)) for key in _ANTHROPIC_CACHE_WRITE_KEYS)
    cache_write = specific_write or _count(details.get("cache_creation"))
    total_raw = usage.get("total_tokens")
    total = _count(total_raw) if total_raw is not None else input_tokens + output_tokens
    return UsageBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=total,
    )


def _count(value: object) -> int:
    """A token count, or zero where a provider reports none (``None``)."""
    return value if isinstance(value, int) else 0


def bucket_costs(breakdown: UsageBreakdown, price: TokenPrices) -> tuple[float, float, float, float]:
    """Dollars per bucket for one response: input, output, cache-read, cache-write (§11, L1-3)."""
    return (
        breakdown.uncached_input_tokens / 1_000_000 * price.input,
        breakdown.output_tokens / 1_000_000 * price.output,
        breakdown.cache_read_tokens / 1_000_000 * price.cache_read,
        breakdown.cache_write_tokens / 1_000_000 * price.cache_write,
    )


def cost_usd(breakdown: UsageBreakdown, price: TokenPrices) -> float:
    """Dollars for one response, priced per token bucket (§11, L1-3).

    The uncached remainder, not ``input_tokens`` itself, prices at the plain
    input rate: ``input_tokens`` is the whole prompt including cache hits.
    """
    return sum(bucket_costs(breakdown, price))


def response_usage(message: BaseMessage, price: TokenPrices | None) -> UsageTotals:
    """One assistant turn's meter spend: its provider usage, its cost from
    ``price`` (zero where no price is bound, the unit-test seam) and its
    tool calls. A non-assistant message spends nothing.

    The per-bucket token and dollar fields are cumulative-ready: the M7
    hard-ceiling flush reads the spend directly from them (contract §11,
    §15; C-LIM-1, P-LIM-1).
    """
    if not isinstance(message, AIMessage):
        return UsageTotals()
    raw_usage = getattr(message, "usage_metadata", None)
    metadata = cast(UsageMetadata, raw_usage) if isinstance(raw_usage, dict) else None
    breakdown = usage_breakdown(metadata)
    if price is None:
        return UsageTotals(
            tokens=breakdown.total_tokens,
            effective_tokens=breakdown.effective_tokens,
            cost_usd=0.0,
            tool_calls=len(message.tool_calls or []),
            input_tokens=breakdown.input_tokens,
            output_tokens=breakdown.output_tokens,
            cache_read_tokens=breakdown.cache_read_tokens,
            cache_write_tokens=breakdown.cache_write_tokens,
        )
    cost_in, cost_out, cost_read, cost_write = bucket_costs(breakdown, price)
    return UsageTotals(
        tokens=breakdown.total_tokens,
        effective_tokens=breakdown.effective_tokens,
        cost_usd=cost_in + cost_out + cost_read + cost_write,
        tool_calls=len(message.tool_calls or []),
        input_tokens=breakdown.input_tokens,
        output_tokens=breakdown.output_tokens,
        cache_read_tokens=breakdown.cache_read_tokens,
        cache_write_tokens=breakdown.cache_write_tokens,
        cost_input_usd=cost_in,
        cost_output_usd=cost_out,
        cost_cache_read_usd=cost_read,
        cost_cache_write_usd=cost_write,
    )


def messages_usage(messages: Iterable[BaseMessage], price: TokenPrices | None) -> UsageTotals:
    """The summed `response_usage` of every assistant turn in ``messages``."""
    total = UsageTotals()
    for message in messages:
        total = total + response_usage(message, price)
    return total


def _is_non_negative_int(value: object) -> bool:
    """Whether ``value`` is a real non-negative token count (§11.4).

    A ``bool`` is never a count, even though it subclasses ``int``; ``None``,
    negatives, strings and floats are partial usage, never zero.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def response_has_complete_usage(message: BaseMessage) -> bool:
    """Whether one model response carries complete usage metadata (§11.4).

    Complete means the raw ``usage_metadata`` is a dict whose
    ``input_tokens`` and ``output_tokens`` are both non-negative integers.
    Anything else — no metadata, a missing field, ``None``, a negative or a
    non-integer — is partial usage and settles at the Admission hold's worst
    case, exactly like missing usage. The raw dict is judged, never the
    normalised breakdown: a normaliser may turn an absent field into ``0``,
    which would let partial usage under-charge. A non-assistant message
    spends nothing, so its absent usage is vacuously complete.
    """
    if not isinstance(message, AIMessage):
        return True
    raw = getattr(message, "usage_metadata", None)
    if not isinstance(raw, dict):
        return False
    usage = cast(Mapping[str, object], raw)
    return _is_non_negative_int(usage.get("input_tokens")) and _is_non_negative_int(
        usage.get("output_tokens")
    )


def response_lacks_usage_metadata(message: BaseMessage) -> bool:
    """Whether one model response carried no usage metadata (§11).

    Truthful-usage accounting counts, per Attempt, how many model responses
    lacked ``usage_metadata``: a missing response adds nothing to the usage
    totals (budget enforcement keeps its current arithmetic) but the
    Sub-agent path reports each one through ``on_missing_usage`` at once, so
    the caller bumps the Attempt's durable counter and the Status Snapshot
    marks its token and cost figures ``upper-bound`` (§11.4). Only assistant
    turns count; any other message never lacks metadata. Partial usage —
    a dict missing a count — still reads as present here; the Step Execution
    judges completeness separately (``response_has_complete_usage``).
    """
    if not isinstance(message, AIMessage):
        return False
    return not isinstance(getattr(message, "usage_metadata", None), dict)


def _usage_delta(usage_metadata: UsageMetadata | None, price: TokenPrices) -> UsageTotals:
    """One model response's meter delta, per bucket (see `cost_usd`)."""
    breakdown = usage_breakdown(usage_metadata)
    cost_in, cost_out, cost_read, cost_write = bucket_costs(breakdown, price)
    return UsageTotals(
        tokens=breakdown.total_tokens,
        effective_tokens=breakdown.effective_tokens,
        cost_usd=cost_in + cost_out + cost_read + cost_write,
        input_tokens=breakdown.input_tokens,
        output_tokens=breakdown.output_tokens,
        cache_read_tokens=breakdown.cache_read_tokens,
        cache_write_tokens=breakdown.cache_write_tokens,
        cost_input_usd=cost_in,
        cost_output_usd=cost_out,
        cost_cache_read_usd=cost_read,
        cost_cache_write_usd=cost_write,
    )


class InMemoryUsageLedger:
    """The only `UsageLedger` this codebase has today — a process-local
    stand-in for the Run Ledger, which does not exist yet (`runtime.
    graph.attempt` has no source to read an Attempt count from either, for the
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
        self._totals = self._totals + _usage_delta(usage_metadata, price)
        return self._totals

    def flush_tool_call(self) -> UsageTotals:
        self._totals = replace(self._totals, tool_calls=self._totals.tool_calls + 1)
        return self._totals


def ceiling_crossed(totals: UsageTotals, elapsed_seconds: float, ceilings: LoopCeilings) -> str | None:
    """`L3-IMP-6`: the name of the first ceiling `totals`/`elapsed_seconds`
    are over — checked in the fixed order wall clock, cost, effective tokens, tool
    calls — or `None` where every ceiling `ceilings` configures still has
    headroom."""
    if ceilings.max_wall_clock_seconds is not None and elapsed_seconds > ceilings.max_wall_clock_seconds:
        return "wall_clock"
    if ceilings.max_cost_usd is not None and totals.cost_usd > ceilings.max_cost_usd:
        return "cost"
    if (
        ceilings.max_effective_tokens is not None
        and totals.effective_tokens > ceilings.max_effective_tokens
    ):
        return "tokens"
    if ceilings.max_tool_calls is not None and totals.tool_calls > ceilings.max_tool_calls:
        return "tool_calls"
    return None
