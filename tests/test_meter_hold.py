"""The Attempt meter owns the scoped hold lifecycle (issue #656).

One scoped hold carries admission, the Run Ledger mirror and the Attempt
usage flush as a single operation: callers admit once and settle, charge or
close once — never hand-pairing the meter mutation with mirror and flush
writes. These tests assert the Meter's interface plus ledger rows, never
call ordering.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.graph.agent_model_request import wire_meter_to_ledger
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
    worst_case_bound,
)
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)

ATTEMPT_ID = "implement#7/1"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _prices() -> TokenPrices:
    """One dollar a megatoken in, two out; reads a tenth, writes at par."""
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


def _workflow() -> Workflow:
    step = WorkflowStep(
        id="process",
        agent=Agent(tools=(), results={"done": {"note": "string"}}),
        max_executions=1,
        routes={"done": RouteTarget(end="completed")},
    )
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
        steps={"process": step},
    )


def _budgets(ledger: RunLedger) -> AttemptBudgets:
    budgets = build_budgets(
        _workflow(),
        prices=_prices(),
        output_cap=64,
        attempt_id=ATTEMPT_ID,
    )
    wire_meter_to_ledger(budgets, ledger, attempt_id=ATTEMPT_ID)
    return budgets


def test_admit_settle_mirrors_ledger_and_agrees_with_spend(
    ledger: RunLedger,
) -> None:
    """Admit then settle: the ledger row exists and matches meter spend."""
    budgets = _budgets(ledger)
    fact, hold = budgets.admit_hold("process", 500, seq=1)
    assert fact is None and hold is not None

    held = ledger.get_admission_hold(hold.hold_id)
    assert held is not None
    assert held.status == "held"
    assert held.bound_tokens == hold.bound_tokens

    actual = UsageTotals(
        tokens=110,
        effective_tokens=110,
        cost_usd=0.001,
        input_tokens=100,
        output_tokens=10,
        cost_input_usd=0.0005,
        cost_output_usd=0.0005,
    )
    hold.settle(actual, "process", origin="main", responses=1)

    settled = ledger.get_admission_hold(hold.hold_id)
    assert settled is not None
    assert settled.status == "settled"
    assert ledger.list_open_admission_holds(ATTEMPT_ID) == ()
    totals = budgets.attempt_totals()
    assert totals.tokens == 110
    assert totals.effective_tokens == 110


def test_admit_charge_books_worst_case_as_charged(ledger: RunLedger) -> None:
    """Admit then charge: the worst case books and the row reads charged."""
    budgets = _budgets(ledger)
    fact, hold = budgets.admit_hold("process", 500, seq=1)
    assert fact is None and hold is not None
    bound_tokens, _ = worst_case_bound(500, 64, _prices())

    hold.charge(origin="subagent")

    charged = ledger.get_admission_hold(hold.hold_id)
    assert charged is not None
    assert charged.status == "charged"
    totals = budgets.attempt_totals()
    assert totals.tokens == bound_tokens
    assert totals.effective_tokens == bound_tokens
    assert budgets.step_totals("process").tokens == bound_tokens


def test_unused_hold_settles_empty(ledger: RunLedger) -> None:
    """An unused hold settles empty: no spend, settled, flush still fires."""
    budgets = _budgets(ledger)
    fact, hold = budgets.admit_hold("process", 500, seq=1)
    assert fact is None and hold is not None

    hold.close()

    assert budgets.attempt_totals().tokens == 0
    row = ledger.get_admission_hold(hold.hold_id)
    assert row is not None
    assert row.status == "settled"
    assert ledger.get_latest_usage(ATTEMPT_ID) is not None


def test_double_settle_charge_close_does_not_rebook(
    ledger: RunLedger,
) -> None:
    """Settle, charge and close are idempotent after close."""
    budgets = _budgets(ledger)
    fact, hold = budgets.admit_hold("process", 500, seq=1)
    assert fact is None and hold is not None
    actual = UsageTotals(tokens=110, effective_tokens=110, cost_usd=0.001)
    hold.settle(actual, "process", origin="main", responses=1)
    settled = budgets.attempt_totals()

    hold.settle(UsageTotals(tokens=999, effective_tokens=999), "process")
    hold.charge()
    hold.close()

    assert budgets.attempt_totals() == settled
    row = ledger.get_admission_hold(hold.hold_id)
    assert row is not None
    assert row.status == "settled"


def test_ledger_failure_never_fails_the_hold() -> None:
    """Mirror and flush sinks are best-effort: raising never fails."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger is down")

    budgets = build_budgets(
        _workflow(), prices=_prices(), output_cap=64, attempt_id=ATTEMPT_ID
    )
    budgets.bind_meter_sinks(
        record_hold=_boom, settle_hold=_boom, flush_usage=_boom
    )
    fact, hold = budgets.admit_hold("process", 500, seq=1)
    assert fact is None and hold is not None

    hold.settle(UsageTotals(tokens=10, effective_tokens=10), "process")

    _, second = budgets.admit_hold("process", 500, seq=2)
    assert second is not None
    second.charge()
    _, third = budgets.admit_hold("process", 500, seq=3)
    assert third is not None
    third.close()


def test_gateway_pricing_and_origin_tagging_preserved(
    ledger: RunLedger,
) -> None:
    """Per-pin gateway pricing and origin booking ride on the hold."""
    budgets = _budgets(ledger)
    gateway_prices = TokenPrices(
        input=1.0, output=2.0, cache_read=0.1, cache_write=10.0
    )
    direct_tokens, direct_cost = worst_case_bound(500, 64, gateway_prices)
    _, gateway = budgets.admit_hold(
        "process", 500, seq=1, prices=gateway_prices, is_gateway=True
    )
    assert gateway is not None
    gateway_tokens, gateway_cost = worst_case_bound(
        500, 64, gateway_prices, is_gateway=True
    )
    assert gateway.bound_tokens == gateway_tokens == direct_tokens
    assert gateway.bound_cost_usd == gateway_cost
    assert gateway_cost > direct_cost
    gateway.close()
    assert budgets.attempt_totals().tokens == 0

    _, held = budgets.admit_hold("process", 500, seq=2, origin="subagent")
    assert held is not None
    held.settle(
        UsageTotals(tokens=50, effective_tokens=50), "process", responses=1
    )
    cells = budgets.step_by_origin("process")
    assert cells["subagent"].effective_tokens == 50
    assert cells["main"].effective_tokens == 0
