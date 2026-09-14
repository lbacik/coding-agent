from __future__ import annotations

from coding_agent.implement.ceilings import (
    InMemoryUsageLedger,
    LoopCeilings,
    UsageTotals,
    ceiling_crossed,
)
from coding_agent.provider.price_table import TokenPrices

_PRICE = TokenPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)


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


def test_flush_model_response_accumulates_across_calls() -> None:
    ledger = InMemoryUsageLedger()
    usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}

    ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]
    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]

    assert totals.tokens == 400


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
    assert totals.cost_usd == 1.5


# --- L3-IMP-8: a re-entered node does not double-count already-flushed usage


def test_replaying_with_the_same_ledger_only_adds_the_new_deltas() -> None:
    """Simulated restart: a ledger already carrying usage from an earlier
    (interrupted) run is handed back in, rather than a fresh one. The
    node's own new work adds on top — the earlier total is neither reset
    nor re-derived."""
    ledger = InMemoryUsageLedger(UsageTotals(tokens=500, cost_usd=1.0, tool_calls=3))
    usage = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}

    totals = ledger.flush_model_response(usage, _PRICE)  # type: ignore[arg-type]
    totals = ledger.flush_tool_call()

    assert totals.tokens == 700
    assert totals.tool_calls == 4
    assert ledger.totals == totals


# --- ceiling_crossed: L3-IMP-6 -----------------------------------------------


def test_ceiling_crossed_is_none_when_every_ceiling_has_headroom() -> None:
    ceilings = LoopCeilings(
        max_wall_clock_seconds=60, max_cost_usd=10, max_tokens=1000, max_tool_calls=10
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
    ceilings = LoopCeilings(max_tokens=1000)
    totals = UsageTotals(tokens=1001)

    assert ceiling_crossed(totals, elapsed_seconds=0, ceilings=ceilings) == "tokens"


def test_ceiling_crossed_reports_tool_calls() -> None:
    ceilings = LoopCeilings(max_tool_calls=5)
    totals = UsageTotals(tool_calls=6)

    assert ceiling_crossed(totals, elapsed_seconds=0, ceilings=ceilings) == "tool_calls"


def test_ceiling_crossed_at_exactly_the_ceiling_is_not_crossed() -> None:
    ceilings = LoopCeilings(
        max_wall_clock_seconds=60, max_cost_usd=5.0, max_tokens=1000, max_tool_calls=5
    )
    totals = UsageTotals(tokens=1000, cost_usd=5.0, tool_calls=5)

    assert ceiling_crossed(totals, elapsed_seconds=60, ceilings=ceilings) is None
