"""Attempt-wide meters: per-bucket cost and the ceilings.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Meters").

MVP row ids: L1-3, L3-IMP-6, L3-IMP-8.
"""

from __future__ import annotations

import pytest
from anthropic.types import CacheCreation, Usage
from langchain_anthropic.chat_models import _create_usage_metadata
from langchain_core.messages import AIMessage, HumanMessage, UsageMetadata

from coding_agent.runtime.ceilings import (
    InMemoryUsageLedger,
    LoopCeilings,
    UsageTotals,
    ceiling_crossed,
    cost_usd,
    messages_usage,
    response_usage,
    usage_breakdown,
)
from coding_agent.provider.price_table import TokenPrices

_PRICE = TokenPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)


def _anthropic_usage(
    *, uncached: int, output: int, cache_read: int, cache_write_5m: int
) -> UsageMetadata:
    """``usage_metadata`` exactly as langchain-anthropic builds it from the
    provider's ``Usage``: the adapter adds cache hits and writes back into
    ``input_tokens``, reports the write in ``ephemeral_5m_input_tokens`` and
    zeroes the generic ``cache_creation``."""
    return _create_usage_metadata(
        Usage(
            input_tokens=uncached,
            output_tokens=output,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write_5m,
            cache_creation=CacheCreation(
                ephemeral_5m_input_tokens=cache_write_5m, ephemeral_1h_input_tokens=0
            ),
        )
    )


# --- L1-3: cost per token bucket --------------------------------------------


def test_l1_3_anthropic_cache_write_and_cache_read_are_priced_per_bucket() -> None:
    """L1-3: a response with cache reads and an Anthropic-style cache write.

    ``cache_creation`` stays zero while the write sits in
    ``ephemeral_5m_input_tokens``; pricing input and output alone, or reading
    ``cache_creation``, is wrong in both directions (ADR 0009).
    """
    usage = _anthropic_usage(
        uncached=250_000, output=100_000, cache_read=5_000_000, cache_write_5m=1_000_000
    )
    assert usage["input_tokens"] == 6_250_000
    assert usage.get("input_token_details", {}).get("cache_creation") == 0

    breakdown = usage_breakdown(usage)

    assert breakdown.cache_read_tokens == 5_000_000
    assert breakdown.cache_write_tokens == 1_000_000
    assert breakdown.uncached_input_tokens == 250_000
    # 0.25 M uncached × $2 + 0.1 M out × $10 + 5 M read × $0.2 + 1 M write × $2.5
    assert cost_usd(breakdown, _PRICE) == pytest.approx(0.5 + 1.0 + 1.0 + 2.5)
    naive = 6_250_000 / 1_000_000 * 2.0 + 100_000 / 1_000_000 * 10.0
    assert naive == pytest.approx(13.5)  # the input/output formula: 2.7x over


def test_l1_3_a_provider_reporting_none_for_a_bucket_prices_it_at_zero() -> None:
    """OpenAI reports ``None`` for an absent bucket rather than omitting it."""
    usage: UsageMetadata = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "total_tokens": 1_000_000,
        "input_token_details": {"cache_read": None, "cache_creation": None},  # type: ignore[typeddict-item]
    }

    assert cost_usd(usage_breakdown(usage), _PRICE) == pytest.approx(2.0)


def test_l1_3_response_usage_costs_the_turn_and_counts_its_tool_calls() -> None:
    usage = _anthropic_usage(uncached=1_000_000, output=0, cache_read=0, cache_write_5m=0)
    turn = AIMessage(
        content="",
        tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}],
        usage_metadata=usage,
    )

    totals = response_usage(turn, _PRICE)

    assert (totals.tokens, totals.effective_tokens, totals.tool_calls) == (1_000_000, 1_000_000, 1)
    assert totals.cost_usd == pytest.approx(2.0)
    # No bound price (the unit-test seam) meters tokens but no cost.
    assert response_usage(turn, None).cost_usd == 0.0
    # A non-assistant message spends nothing.
    assert response_usage(HumanMessage(content="hi"), _PRICE) == UsageTotals()


def test_l1_3_messages_usage_sums_every_assistant_turn() -> None:
    usage = _anthropic_usage(uncached=500_000, output=0, cache_read=0, cache_write_5m=0)
    messages = [
        HumanMessage(content="brief"),
        AIMessage(content="", usage_metadata=usage),
        AIMessage(content="", usage_metadata=usage),
    ]

    totals = messages_usage(messages, _PRICE)

    assert totals.tokens == 1_000_000
    assert totals.cost_usd == pytest.approx(2.0)


def test_usage_totals_add_every_meter_including_cost() -> None:
    left = UsageTotals(tokens=1, effective_tokens=2, cost_usd=0.5, tool_calls=3)
    right = UsageTotals(tokens=10, effective_tokens=20, cost_usd=1.25, tool_calls=30)

    assert left + right == UsageTotals(
        tokens=11, effective_tokens=22, cost_usd=1.75, tool_calls=33
    )


# --- InMemoryUsageLedger.flush_model_response -------------------------------


def test_flush_model_response_with_no_usage_metadata_adds_nothing() -> None:
    ledger = InMemoryUsageLedger()

    totals = ledger.flush_model_response(None, _PRICE)

    assert totals == UsageTotals()


def test_flush_model_response_prices_uncached_input_and_output() -> None:
    ledger = InMemoryUsageLedger()
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "total_tokens": 2_000_000}

    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]

    assert totals.tokens == 2_000_000
    assert totals.effective_tokens == 2_000_000
    assert totals.cost_usd == 2.0 + 10.0


def test_flush_model_response_prices_cache_read_and_cache_write_separately() -> None:
    """`input_tokens` is the whole prompt including cache hits — a cache
    read/write bucket is a subset of it, not an addition, so the uncached
    remainder is what prices at the plain input rate."""
    ledger = InMemoryUsageLedger()
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "total_tokens": 1_000_000,
        "input_token_details": {"cache_read": 400_000, "cache_creation": 100_000},
    }

    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]

    uncached = 1_000_000 - 400_000 - 100_000
    expected = uncached / 1_000_000 * 2.0 + 400_000 / 1_000_000 * 0.2 + 100_000 / 1_000_000 * 2.5
    assert totals.cost_usd == expected
    assert totals.tokens == 1_000_000
    assert totals.effective_tokens == 600_000


def test_effective_tokens_include_fresh_input_cache_writes_and_output() -> None:
    ledger = InMemoryUsageLedger()
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_token_details": {"cache_read": 70, "cache_creation": 10},
    }

    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]

    # The cache write remains newly processed work; only the cache read is
    # removed from the effective-work calculation.
    assert totals.effective_tokens == 50


def test_flush_model_response_accumulates_across_calls() -> None:
    ledger = InMemoryUsageLedger()
    usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}

    ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]
    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]

    assert totals.tokens == 400
    assert totals.effective_tokens == 400


# --- InMemoryUsageLedger.flush_tool_call ------------------------------------


def test_flush_tool_call_increments_the_running_total() -> None:
    ledger = InMemoryUsageLedger()

    ledger.flush_tool_call()
    totals = ledger.flush_tool_call()

    assert totals.tool_calls == 2


def test_flush_tool_call_never_touches_tokens_or_cost() -> None:
    ledger = InMemoryUsageLedger(UsageTotals(tokens=10, cost_usd=1.5, tool_calls=0))

    totals = ledger.flush_tool_call()

    assert totals.tokens == 10
    assert totals.effective_tokens == 0
    assert totals.cost_usd == 1.5


# --- L3-IMP-8: a re-entered node does not double-count already-flushed usage


def test_replaying_with_the_same_ledger_only_adds_the_new_deltas() -> None:
    """Simulated restart: a ledger already carrying usage from an earlier
    (interrupted) run is handed back in, rather than a fresh one. The
    node's own new work adds on top — the earlier total is neither reset
    nor re-derived."""
    ledger = InMemoryUsageLedger(
        UsageTotals(tokens=500, effective_tokens=500, cost_usd=1.0, tool_calls=3)
    )
    usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}

    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]
    totals = ledger.flush_tool_call()

    assert totals.tokens == 700
    assert totals.effective_tokens == 700
    assert totals.tool_calls == 4
    assert ledger.totals == totals


# --- ceiling_crossed: L3-IMP-6 -----------------------------------------------


def test_ceiling_crossed_is_none_when_every_ceiling_has_headroom() -> None:
    ceilings = LoopCeilings(
        max_wall_clock_seconds=60, max_cost_usd=10, max_effective_tokens=1000, max_tool_calls=10
    )
    totals = UsageTotals(tokens=1, cost_usd=0.01, tool_calls=1)

    assert ceiling_crossed(totals, elapsed_seconds=1, ceilings=ceilings) is None


def test_ceiling_crossed_is_none_when_no_ceiling_is_configured() -> None:
    totals = UsageTotals(tokens=10**9, cost_usd=10**9, tool_calls=10**9)

    assert ceiling_crossed(totals, elapsed_seconds=10**9, ceilings=LoopCeilings()) is None


def test_ceiling_crossed_reports_wall_clock() -> None:
    ceilings = LoopCeilings(max_wall_clock_seconds=60)

    assert ceiling_crossed(UsageTotals(), elapsed_seconds=61, ceilings=ceilings) == "wall_clock"


def test_ceiling_crossed_reports_cost() -> None:
    ceilings = LoopCeilings(max_cost_usd=5.0)
    totals = UsageTotals(cost_usd=5.01)

    assert ceiling_crossed(totals, elapsed_seconds=0, ceilings=ceilings) == "cost"


def test_ceiling_crossed_reports_tokens() -> None:
    ceilings = LoopCeilings(max_effective_tokens=1000)
    totals = UsageTotals(tokens=10_000, effective_tokens=1001)

    assert ceiling_crossed(totals, elapsed_seconds=0, ceilings=ceilings) == "tokens"


def test_token_ceiling_ignores_raw_cache_read_throughput() -> None:
    ceilings = LoopCeilings(max_effective_tokens=1000)
    totals = UsageTotals(tokens=100_000, effective_tokens=1000)

    assert ceiling_crossed(totals, elapsed_seconds=0, ceilings=ceilings) is None


def test_ceiling_crossed_reports_tool_calls() -> None:
    ceilings = LoopCeilings(max_tool_calls=5)
    totals = UsageTotals(tool_calls=6)

    assert ceiling_crossed(totals, elapsed_seconds=0, ceilings=ceilings) == "tool_calls"


def test_ceiling_crossed_at_exactly_the_ceiling_is_not_crossed() -> None:
    ceilings = LoopCeilings(
        max_wall_clock_seconds=60, max_cost_usd=5.0, max_effective_tokens=1000, max_tool_calls=5
    )
    totals = UsageTotals(tokens=10_000, effective_tokens=1000, cost_usd=5.0, tool_calls=5)

    assert ceiling_crossed(totals, elapsed_seconds=60, ceilings=ceilings) is None
