"""Per-origin usage accounting: cells, flush, archive reads and restore (#576).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §11.1 (spend
is carried per Usage Origin in per-bucket cells; the Attempt is the sum of
all cells; a Step Execution's own spend is the step meter minus its opening
baseline per origin; worst-case charges land in the request's origin; tool
calls split per origin; enforcement counts all origins) and §13.2 (Step
Execution ``usage.by_origin``, the Attempt usage flush shape, archived
records reading ``unavailable`` never 0, joint restore of ``usage`` and
``by_origin``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator.snapshots import _by_origin_for_attempt
from coding_agent.operator.step_projection import spend_from_executions
from coding_agent.runtime.ceilings import OriginCell, UsageTotals
from coding_agent.runtime.graph.agent_model_request import _settle_response_spend
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    ExecutionTally,
    _meter_usage_dict,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import REPORT_SUBAGENT_TOOL
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
    by_origin_from_readable,
    restore_budgets_from_usage,
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
from support.agent_step_deps import (
    make_agent_step_deps,
    make_attempt,
    make_budgets,
    make_pin,
    make_prices,
    make_workflow,
)
from support.scripted_model import ScriptedModel

STEP_ID = "process"

_NOT_RECORDED = "not recorded"


def _budgets() -> AttemptBudgets:
    """Live meter state under a generous ceiling no scripted turn can cross."""
    return make_budgets(
        make_workflow("implement"),
        attempt_id="implement#7/1",
        prices=make_prices(),
        output_cap=100,
    )


def _main_delta() -> UsageTotals:
    return UsageTotals(
        tokens=1300,
        effective_tokens=1100,
        cost_usd=0.004,
        input_tokens=1000,
        output_tokens=300,
        cache_read_tokens=200,
        cache_write_tokens=100,
        cost_input_usd=0.001,
        cost_output_usd=0.002,
        cost_cache_read_usd=0.0005,
        cost_cache_write_usd=0.0005,
    )


def _subagent_delta() -> UsageTotals:
    return UsageTotals(
        tokens=600,
        effective_tokens=600,
        cost_usd=0.002,
        input_tokens=500,
        output_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_input_usd=0.001,
        cost_output_usd=0.001,
        cost_cache_read_usd=0.0,
        cost_cache_write_usd=0.0,
    )


def _handoff_delta() -> UsageTotals:
    return UsageTotals(
        tokens=260,
        effective_tokens=200,
        cost_usd=0.001,
        input_tokens=200,
        output_tokens=60,
        cache_read_tokens=60,
        cache_write_tokens=0,
        cost_input_usd=0.0005,
        cost_output_usd=0.0005,
        cost_cache_read_usd=0.0,
        cost_cache_write_usd=0.0,
    )


def _assert_origins_sum_to_attempt(budgets: AttemptBudgets) -> None:
    """Every origin cell sums to the Attempt meter (§11.1, §11.2)."""
    cells = budgets.attempt_by_origin()
    assert cells is not None
    totals = budgets.attempt_totals()
    assert sum(cell.input_tokens for cell in cells.values()) == totals.input_tokens
    assert sum(cell.output_tokens for cell in cells.values()) == totals.output_tokens
    assert (
        sum(cell.cache_read_tokens for cell in cells.values())
        == totals.cache_read_tokens
    )
    assert (
        sum(cell.cache_write_tokens for cell in cells.values())
        == totals.cache_write_tokens
    )
    assert (
        sum(cell.effective_tokens for cell in cells.values())
        == totals.effective_tokens
    )
    # Dollar sums compare approximately: cells accumulate per bucket while
    # the Attempt meter accumulates the response total, so the float order
    # differs in the last ulp.
    assert sum(cell.total_cost_usd for cell in cells.values()) == pytest.approx(
        totals.cost_usd
    )
    assert sum(cell.cost_input_usd for cell in cells.values()) == pytest.approx(
        totals.cost_input_usd
    )
    assert sum(cell.cost_output_usd for cell in cells.values()) == pytest.approx(
        totals.cost_output_usd
    )
    assert sum(
        cell.cost_cache_read_usd for cell in cells.values()
    ) == pytest.approx(totals.cost_cache_read_usd)
    assert sum(
        cell.cost_cache_write_usd for cell in cells.values()
    ) == pytest.approx(totals.cost_cache_write_usd)
    assert sum(cell.tool_calls for cell in cells.values()) == totals.tool_calls
    assert (
        sum(cell.input_tokens + cell.output_tokens for cell in cells.values())
        == totals.tokens
    )


def _assert_origins_sum_to_step(budgets: AttemptBudgets, step_id: str) -> None:
    """Every origin cell sums to the step meter (§11.2)."""
    cells = budgets.step_by_origin(step_id)
    totals = budgets.step_totals(step_id)
    assert sum(cell.input_tokens for cell in cells.values()) == totals.input_tokens
    assert sum(cell.output_tokens for cell in cells.values()) == totals.output_tokens
    assert (
        sum(cell.effective_tokens for cell in cells.values())
        == totals.effective_tokens
    )
    assert sum(cell.total_cost_usd for cell in cells.values()) == pytest.approx(
        totals.cost_usd
    )
    assert sum(cell.tool_calls for cell in cells.values()) == totals.tool_calls


def test_origins_land_in_own_cells() -> None:
    """Main, Sub-agent and handoff requests settle in their own cells."""
    budgets = _budgets()
    budgets.add_spend(STEP_ID, _main_delta(), origin="main", responses=1)
    budgets.add_spend(STEP_ID, _subagent_delta(), origin="subagent", responses=2)
    budgets.add_spend(STEP_ID, _handoff_delta(), origin="handoff", responses=1)

    cells = budgets.attempt_by_origin()
    assert cells is not None
    main = cells["main"]
    assert main.input_tokens == 1000
    assert main.output_tokens == 300
    assert main.cache_read_tokens == 200
    assert main.cache_write_tokens == 100
    assert main.effective_tokens == 1100
    assert main.responses == 1
    assert main.responses_without_usage == 0
    subagent = cells["subagent"]
    assert subagent.input_tokens == 500
    assert subagent.output_tokens == 100
    assert subagent.responses == 2
    handoff = cells["handoff"]
    assert handoff.input_tokens == 200
    assert handoff.responses == 1
    assert handoff.tool_calls == 0
    # The response counts are measurements: recorded per origin, never metered.
    assert sum(cell.responses for cell in cells.values()) == 1 + 2 + 1
    assert (
        sum(cell.responses_without_usage for cell in cells.values()) == 0
    )

    _assert_origins_sum_to_attempt(budgets)
    _assert_origins_sum_to_step(budgets, STEP_ID)


def test_worst_case_charge_lands_in_request_origin() -> None:
    """A charged hold books input-bucket tokens and a missing-usage count."""
    budgets = _budgets()
    estimate = 1000
    bound_tokens, bound_cost = worst_case_bound(
        estimate, 100, make_prices(), is_gateway=False
    )
    fact, hold_id = budgets.admit_request(STEP_ID, estimate, origin="subagent")
    assert fact is None
    assert hold_id is not None
    budgets.charge_hold(hold_id, origin="subagent")

    cells = budgets.attempt_by_origin()
    assert cells is not None
    charged = cells["subagent"]
    assert charged.input_tokens == bound_tokens
    assert charged.cost_input_usd == bound_cost
    assert charged.effective_tokens == bound_tokens
    assert charged.responses == 1
    assert charged.responses_without_usage == 1
    for origin in ("main", "handoff"):
        assert cells[origin] == OriginCell()
    _assert_origins_sum_to_attempt(budgets)


def test_charge_hold_defaults_to_admission_origin() -> None:
    """A hold charged without an origin settles under its admission origin."""
    budgets = _budgets()
    fact, hold_id = budgets.admit_request(STEP_ID, 500, origin="handoff")
    assert fact is None
    assert hold_id is not None
    budgets.charge_hold(hold_id)

    cells = budgets.attempt_by_origin()
    assert cells is not None
    assert cells["handoff"].responses_without_usage == 1
    assert cells["main"] == OriginCell()
    assert cells["subagent"] == OriginCell()


def test_tool_calls_split_per_origin() -> None:
    """Sub-agent tools are ``subagent``; the caller's own calls are ``main``."""
    budgets = _budgets()
    assert budgets.note_tool_calls(STEP_ID, 2) is None
    assert budgets.note_tool_calls(STEP_ID, 3, origin="subagent") is None

    cells = budgets.attempt_by_origin()
    assert cells is not None
    assert cells["main"].tool_calls == 2
    assert cells["subagent"].tool_calls == 3
    assert cells["handoff"].tool_calls == 0
    assert budgets.attempt_totals().tool_calls == 5
    _assert_origins_sum_to_attempt(budgets)
    _assert_origins_sum_to_step(budgets, STEP_ID)


def test_step_execution_spend_is_meter_delta_per_origin(tmp_path: Path) -> None:
    """One execution's usage is the step meter now minus its opening baseline."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
    )
    budgets = deps.budgets
    budgets.add_spend("other", _main_delta(), origin="main", responses=1)
    baseline = budgets.step_totals(STEP_ID)
    baseline_by_origin = budgets.step_by_origin(STEP_ID)
    budgets.add_spend(STEP_ID, _subagent_delta(), origin="subagent", responses=1)
    budgets.add_spend(STEP_ID, _handoff_delta(), origin="handoff", responses=1)

    usage = _meter_usage_dict(deps, STEP_ID, baseline, baseline_by_origin)
    assert usage["input_tokens"] == 500 + 200
    assert usage["output_tokens"] == 100 + 60
    raw_by_origin = usage["by_origin"]
    assert isinstance(raw_by_origin, dict)
    main_cell = raw_by_origin["main"]
    assert isinstance(main_cell, dict)
    assert main_cell["responses"] == 0
    subagent_cell = raw_by_origin["subagent"]
    assert isinstance(subagent_cell, dict)
    assert subagent_cell["input_tokens"] == 500
    handoff_cell = raw_by_origin["handoff"]
    assert isinstance(handoff_cell, dict)
    assert handoff_cell["input_tokens"] == 200


def test_archived_flush_without_by_origin_reads_unavailable() -> None:
    """A flush without ``by_origin`` disables tracking; figures stay unknown."""
    budgets = _budgets()
    budgets.add_spend(STEP_ID, _main_delta(), origin="main", responses=1)
    flown = {
        "usage": budgets.attempt_totals().as_readable(),
        "elapsed_seconds": 1.0,
    }
    assert by_origin_from_readable(flown) is None

    fresh = _budgets()
    restore_budgets_from_usage(fresh, flown)
    assert fresh.origins_enabled() is False
    assert fresh.attempt_by_origin() is None

    figures = _by_origin_for_attempt(None)
    for origin in ("main", "subagent", "handoff"):
        for figure in figures[origin].values():
            assert figure["basis"] == "unavailable"
            assert figure["reason"] == _NOT_RECORDED
            assert figure["value"] is None


def test_recorded_cells_read_measured_per_origin() -> None:
    """Status shows per-origin cells where the flush recorded them (§13.2)."""
    budgets = _budgets()
    budgets.add_spend(STEP_ID, _main_delta(), origin="main", responses=1)
    budgets.add_spend(STEP_ID, _subagent_delta(), origin="subagent", responses=1)
    cells = budgets.attempt_by_origin()
    assert cells is not None
    source = {origin: cell.as_readable() for origin, cell in cells.items()}

    figures = _by_origin_for_attempt(source)
    main_tokens = figures["main"]["tokens"]
    assert main_tokens["basis"] == "measured"
    assert main_tokens["value"] == 1000 + 300
    assert figures["main"]["responses"]["value"] == 1
    subagent_cost = figures["subagent"]["cost_usd"]
    assert subagent_cost["basis"] == "estimated"
    assert subagent_cost["value"] == pytest.approx(0.002)
    handoff_tokens = figures["handoff"]["tokens"]
    assert handoff_tokens["basis"] == "measured"
    assert handoff_tokens["value"] == 0


def test_execution_sum_unavailable_where_any_record_lacks_it() -> None:
    """Old spend is never assigned to ``main``: one legacy record blanks all."""
    legacy = {
        "tokens": 100,
        "effective_tokens": 100,
        "cost_usd": 0.001,
        "tool_calls": 1,
    }
    recorded = {
        "tokens": 50,
        "effective_tokens": 50,
        "cost_usd": 0.0005,
        "tool_calls": 0,
        "by_origin": {
            "main": OriginCell(input_tokens=40, output_tokens=10).as_readable(),
            "subagent": OriginCell().as_readable(),
            "handoff": OriginCell().as_readable(),
        },
    }
    spend = spend_from_executions([{"usage": legacy}, {"usage": recorded}])
    assert spend["cost_usd"] == 0.001 + 0.0005
    raw_by_origin = spend["by_origin"]
    assert isinstance(raw_by_origin, dict)
    for origin in ("main", "subagent", "handoff"):
        assert raw_by_origin[origin] is None

    figures = _by_origin_for_attempt(raw_by_origin["main"])
    for figure in figures["main"].values():
        assert figure["basis"] == "unavailable"


def test_disabled_origins_omit_by_origin_from_zero_shapes(tmp_path: Path) -> None:
    """After an archived restore, zero shapes carry no per-origin key."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
    )
    flown: dict[str, Any] = {"usage": UsageTotals(tokens=10).as_readable()}
    restore_budgets_from_usage(deps.budgets, flown)
    assert deps.budgets.origins_enabled() is False

    step = WorkflowStep(
        id=STEP_ID,
        agent=Agent(
            tools=("workspace-read",),
            results={"done": {"note": "string"}},
        ),
        max_executions=1,
        limits={},
        reserved=False,
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )
    tally = ExecutionTally(
        deps=deps,
        step=step,
        seq=1,
        baseline=deps.budgets.step_totals(step.id),
        baseline_by_origin=deps.budgets.step_by_origin(step.id),
    )
    outcome = tally.prefix_too_large()
    assert "by_origin" not in outcome.usage


def _zero_tally(tmp_path: Path) -> ExecutionTally:
    """A fresh tally over scripted deps with per-origin tracking enabled."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
    )
    step = WorkflowStep(
        id=STEP_ID,
        agent=Agent(
            tools=("workspace-read",),
            results={"done": {"note": "string"}},
        ),
        max_executions=1,
        limits={},
        reserved=False,
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )
    return ExecutionTally(
        deps=deps,
        step=step,
        seq=1,
        baseline=deps.budgets.step_totals(step.id),
        baseline_by_origin=deps.budgets.step_by_origin(step.id),
    )


def _assert_zero_by_origin(usage: dict[str, object]) -> None:
    """The full meter shape with zero per-origin cells, never missing fields."""
    for key in (
        "tokens",
        "effective_tokens",
        "cost_usd",
        "tool_calls_meter",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        assert usage[key] == 0
    raw_by_origin = usage["by_origin"]
    assert isinstance(raw_by_origin, dict)
    for origin in ("main", "subagent", "handoff"):
        cell = raw_by_origin[origin]
        assert isinstance(cell, dict)
        assert cell["responses"] == 0
        assert cell["responses_without_usage"] == 0
        assert cell["input_tokens"] == 0
        assert cell["effective_tokens"] == 0


def test_handoff_failed_records_zero_by_origin(tmp_path: Path) -> None:
    """A failed handoff turn records zero cells per origin, never no key."""
    outcome = _zero_tally(tmp_path).handoff_failed(RuntimeError("boom"))
    assert isinstance(outcome.usage, dict)
    _assert_zero_by_origin(outcome.usage)


def test_missing_inputs_records_zero_by_origin(tmp_path: Path) -> None:
    """Missing inputs record zero cells per origin, never missing fields."""
    outcome = _zero_tally(tmp_path).missing_inputs("no input")
    assert isinstance(outcome.usage, dict)
    _assert_zero_by_origin(outcome.usage)


def _tool_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _scripted_ai(
    calls: list[dict[str, object]] | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=list(calls or []),
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    )


def test_spawn_call_is_main_subagent_tools_are_subagent(tmp_path: Path) -> None:
    """The caller's ``spawn_subagent`` is ``main``; Sub-agent tools are ``subagent``."""
    attempt_id = make_attempt().attempt_id
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        try:
            ledger.open_attempt(attempt_id, effective_configuration={})
        except sqlite3.IntegrityError:
            pass
        step = WorkflowStep(
            id=STEP_ID,
            agent=Agent(
                tools=("workspace-read", "subagents"),
                results={"done": {}},
            ),
            max_executions=1,
            limits={},
            reserved=False,
            routes={
                "done": RouteTarget(end="completed"),
                "limit-exceeded": RouteTarget(end="unfinished"),
                "error": RouteTarget(end="failed"),
            },
        )
        workflow = Workflow(
            id="implement",
            model="sonnet",
            input=InputDecl(type="issue", label="agent:test"),
            ceiling=AttemptCeiling(
                cost_usd=100000.0,
                tokens=100000.0,
                tool_calls=10000.0,
                wall_clock_minutes=600.0,
            ),
            start=step.id,
            steps={step.id: step},
        )
        budgets = build_budgets(
            workflow,
            prices=make_prices(),
            output_cap=0,
            attempt_id=attempt_id,
        )
        model = ScriptedModel(
            [
                _scripted_ai(
                    [
                        _tool_call(
                            "spawn_subagent",
                            {
                                "description": "look",
                                "instructions": "read the workspace files",
                            },
                            "s1",
                        )
                    ]
                ),
                _scripted_ai(
                    [_tool_call("read_file", {"path": "README.md"}, "r1")]
                ),
                _scripted_ai(
                    [_tool_call("read_file", {"path": "README.md"}, "r2")]
                ),
                _scripted_ai(
                    [
                        _tool_call(
                            REPORT_SUBAGENT_TOOL, {"report": "nothing"}, "sub-1"
                        )
                    ]
                ),
                AIMessage(
                    content="reporting",
                    tool_calls=[
                        _tool_call(
                            REPORT_TOOL_NAME,
                            {"result": "done", "payload_json": json.dumps({})},
                            "report-1",
                        )
                    ],
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                ),
            ],
            response_models=["test-model"] * 5,
        )
        deps = make_agent_step_deps(
            model=model,
            workspace=workspace,
            tmp_path=tmp_path,
            attempt=make_attempt(),
            ledger=ledger,
            budgets=budgets,
            prices=make_prices(),
            pin=make_pin(),
        )
        outcome = run_agent_step(workflow.steps[step.id], 1, deps, {})

        assert outcome.status == "reported"
        cells = budgets.attempt_by_origin()
        assert cells is not None
        assert cells["main"].tool_calls == 1
        assert cells["subagent"].tool_calls == 2
        assert cells["handoff"].tool_calls == 0
        assert cells["main"].responses == 2
        assert cells["subagent"].responses == 3
        assert cells["handoff"].responses == 0
        _assert_origins_sum_to_attempt(budgets)

        latest = ledger.get_latest_usage(attempt_id)
        assert latest is not None
        assert latest["usage"]["tool_calls"] == 3
        raw_by_origin = latest["by_origin"]
        assert isinstance(raw_by_origin, dict)
        assert raw_by_origin["main"]["tool_calls"] == 1
        assert raw_by_origin["subagent"]["tool_calls"] == 2
        # The flush carries totals plus per-origin cells, never per-step data.
        assert "per_step" not in latest
        assert STEP_ID not in latest
    finally:
        ledger.close()


def test_archived_restore_flush_carries_no_by_origin(tmp_path: Path) -> None:
    """Later flushes carry none where the last flush had none (§13.2)."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        attempt_id = make_attempt().attempt_id
        ledger.open_attempt(attempt_id, effective_configuration={})
        budgets = _budgets()
        budgets.add_spend(STEP_ID, _main_delta(), origin="main", responses=1)
        archived = {"usage": budgets.attempt_totals().as_readable()}

        restarted = _budgets()
        restore_budgets_from_usage(restarted, archived)
        restarted.add_spend(STEP_ID, _subagent_delta(), origin="subagent")
        assert restarted.attempt_by_origin() is None
        ledger.record_usage(
            attempt_id,
            BudgetSnapshot(
                elapsed_seconds=1.0,
                usage=restarted.attempt_totals(),
                context=restarted.main_context_snapshot(),
                by_origin=restarted.attempt_by_origin(),
            ),
            detail="attempt usage flush",
        )
        latest = ledger.get_latest_usage(attempt_id)
        assert latest is not None
        assert latest["usage"]["tokens"] == 1300 + 600
        assert "by_origin" not in latest
    finally:
        ledger.close()


def test_pin_path_empty_settle_still_counts_response(tmp_path: Path) -> None:
    """A pin-path settle without usage counts the response under its origin.

    The violating response settles no spend (only a failed request is
    charged its worst case), but the request did respond — so the origin
    cell records one response without usage and stays equal to the
    tally's ``model_calls`` (§11.1).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
    )
    deps.ledger.open_attempt(deps.attempt.attempt_id, effective_configuration={})
    fact, hold_id = deps.budgets.admit_request(STEP_ID, 100, origin="main")
    assert fact is None
    assert hold_id is not None

    _settle_response_spend(
        deps,
        STEP_ID,
        hold_id,
        AIMessage(content="", usage_metadata=None),
        origin="main",
    )

    cells = deps.budgets.attempt_by_origin()
    assert cells is not None
    assert cells["main"].responses == 1
    assert cells["main"].responses_without_usage == 1
    assert cells["main"].input_tokens == 0
    assert cells["main"].total_cost_usd == 0.0
    for origin in ("subagent", "handoff"):
        assert cells[origin] == OriginCell()
    _assert_origins_sum_to_attempt(deps.budgets)
