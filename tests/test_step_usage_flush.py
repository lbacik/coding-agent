"""A step-workflow Attempt flushes spend while its step still runs (#502).

Contract: ``RunLedger.record_usage`` is called after every settled model
response and every counted tool call, so ``current_attempt`` reports the
running step's spend — raw and effective tokens, every budget limit's used
side, cost, tool calls, elapsed time and cache-hit rate — before the step
ends. Only an Attempt that settled no response yet reads ``unavailable``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    run_agent_step,
)
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
    restore_budgets_from_usage,
)
from coding_agent.workflow.schema import (
    Agent,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import (
    make_agent_step_deps,
    make_attempt,
    make_prices,
    make_workflow,
)
from support.scripted_model import ScriptedModel


class _FakeClock:
    """A deterministic Attempt clock for budget tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "limits": {
                "wall_clock_minutes": 60,
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
                "max_parallel_subagents": 4,
            }
        }
    }


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "run-ledger.sqlite3")


def _open(ledger: RunLedger, attempt_id: str) -> None:
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())


def _budgets(
    workflow: Workflow, clock: _FakeClock, attempt_id: str
) -> AttemptBudgets:
    return build_budgets(
        workflow,
        prices=make_prices(),
        attempt_id=attempt_id,
        opened_at=clock.now,
        clock=clock,
    )


def _flush(ledger: RunLedger, attempt_id: str, budgets: AttemptBudgets) -> None:
    """The production flush: the Attempt meter plus the Attempt clock."""
    ledger.record_usage(
        attempt_id,
        BudgetSnapshot(
            elapsed_seconds=budgets.elapsed_seconds(),
            usage=budgets.attempt_totals(),
        ),
    )


def _live(attempt_id: str, issue_number: int, title: str) -> LiveView:
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    live.set_claim(attempt_id, issue_number, title)
    live.set_phase("run_step")
    return live


def _current(ledger: RunLedger, live: LiveView) -> dict[str, Any]:
    result = operator_listener.build_status_result(ledger.path, live)
    current = result["current_attempt"]
    assert isinstance(current, dict)
    return current


def _limits_used(budget: Any) -> dict[str, Any]:
    return {entry["name"]: entry for entry in budget["limits"]}


def _usage_dict(
    input_tokens: int, output_tokens: int, *, cache_read: int = 0
) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {
            "cache_read": cache_read,
            "cache_creation": 0,
        },
    }


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _report_ai(
    result: str,
    payload: dict[str, object],
    usage: dict[str, object] | None,
) -> AIMessage:
    return AIMessage(
        content="reporting",
        tool_calls=[
            _call(
                REPORT_TOOL_NAME,
                {"result": result, "payload_json": json.dumps(payload)},
                "report-1",
            )
        ],
        usage_metadata=usage,
    )


def _agent_step(step_id: str) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=("workspace-read", "workspace-write"), results={"done": {}}),
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )


# --- The open step reports spend before it ends ------------------------------


def test_open_step_flush_reports_spend_before_step_ends(tmp_path: Path) -> None:
    """An open Step Execution with settled spend reads numeric, not unknown."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        ledger.open_step_execution(attempt_id, "process", 1)
        clock = _FakeClock()
        budgets = _budgets(make_workflow(), clock, attempt_id)
        clock.advance(90.0)
        budgets.add_spend(
            "process",
            UsageTotals(
                tokens=1200,
                effective_tokens=1000,
                cost_usd=0.02,
                input_tokens=800,
                output_tokens=200,
                cache_read_tokens=200,
            ),
        )
        budgets.note_tool_calls("process", 2)
        _flush(ledger, attempt_id, budgets)

        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        budget = current["budget"]
        assert budget["raw_tokens"]["value"] == 1200
        assert budget["raw_tokens"]["basis"] == "measured"
        assert budget["effective_tokens"]["value"] == 1000
        assert budget["effective_tokens"]["basis"] == "measured"
        used = _limits_used(budget)
        assert used["effective_tokens"]["used"]["value"] == 1000
        assert used["cost_usd"]["used"]["value"] == pytest.approx(0.02)
        assert used["wall_clock_seconds"]["used"]["value"] == pytest.approx(90.0)
        assert used["tool_calls"]["used"]["value"] == 2
        for name in (
            "effective_tokens",
            "cost_usd",
            "wall_clock_seconds",
            "tool_calls",
        ):
            assert used[name]["used"]["basis"] != "unavailable"
        figures = current["figures"]
        assert figures["cost_usd"]["value"] == pytest.approx(0.02)
        assert figures["tool_calls"]["value"] == 2
        assert figures["elapsed_seconds"]["value"] == pytest.approx(90.0)
        assert figures["cache_hit_rate"]["value"] == pytest.approx(200 / 800)
    finally:
        ledger.close()


def test_unreported_cache_write_marks_tokens_and_cost_estimated(
    tmp_path: Path,
) -> None:
    """An unreported cache write keeps the upper-bound basis with its reason."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        ledger.open_step_execution(attempt_id, "process", 1)
        clock = _FakeClock()
        budgets = _budgets(make_workflow(), clock, attempt_id)
        clock.advance(30.0)
        budgets.add_spend(
            "process",
            UsageTotals(
                tokens=1200,
                effective_tokens=1000,
                cost_usd=0.02,
                input_tokens=800,
                output_tokens=200,
                cache_read_tokens=200,
            ),
        )
        ledger.record_response_with_unreported_cache_write(attempt_id)
        _flush(ledger, attempt_id, budgets)

        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        budget = current["budget"]
        assert budget["raw_tokens"]["value"] == 1200
        effective = budget["effective_tokens"]
        assert effective["value"] == 1000
        assert effective["basis"] == "estimated"
        assert "unreported cache write" in str(effective.get("reason"))
        assert "upper bounds" in str(effective.get("reason"))
        used = _limits_used(budget)
        assert used["cost_usd"]["used"]["basis"] == "estimated"
        assert used["cost_usd"]["used"]["value"] == pytest.approx(0.02)
        assert "upper bounds" in str(used["cost_usd"]["used"].get("reason"))
        assert current["figures"]["cost_usd"]["basis"] == "estimated"
    finally:
        ledger.close()


def test_attempt_without_settled_response_reports_unavailable(
    tmp_path: Path,
) -> None:
    """An open step with no settled response leaves every figure unavailable."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        ledger.open_step_execution(attempt_id, "process", 1)
        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        budget = current["budget"]
        assert budget["raw_tokens"]["basis"] == "unavailable"
        assert budget["raw_tokens"]["value"] is None
        assert budget["effective_tokens"]["basis"] == "unavailable"
        assert budget["effective_tokens"]["value"] is None
        used = _limits_used(budget)
        for name in (
            "effective_tokens",
            "cost_usd",
            "wall_clock_seconds",
            "tool_calls",
        ):
            assert used[name]["used"]["basis"] == "unavailable"
            assert used[name]["used"]["value"] is None
        figures = current["figures"]
        assert figures["cost_usd"]["basis"] == "unavailable"
        assert figures["tool_calls"]["basis"] == "unavailable"
        assert figures["elapsed_seconds"]["basis"] == "unavailable"
        assert figures["cache_hit_rate"]["basis"] == "unavailable"
    finally:
        ledger.close()


# --- Revisits, restarts and the flush-less fallback --------------------------


def test_revisit_flush_equals_attempt_meter_not_double_count(
    tmp_path: Path,
) -> None:
    """After step#2 the flush is the Attempt meter, never executions doubled."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        first = UsageTotals(
            tokens=1100,
            effective_tokens=200,
            cost_usd=0.01,
            tool_calls=1,
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=900,
        )
        second = UsageTotals(
            tokens=550,
            effective_tokens=550,
            cost_usd=0.005,
            tool_calls=2,
            input_tokens=500,
            output_tokens=50,
        )
        clock = _FakeClock()
        budgets = _budgets(make_workflow(), clock, attempt_id)
        budgets.add_spend("process", first)
        ledger.open_step_execution(attempt_id, "process", 1)
        ledger.end_step_execution(
            attempt_id, "process", 1, result="done", usage=first.as_readable()
        )
        clock.advance(45.0)
        budgets.add_spend("process", second)
        ledger.open_step_execution(attempt_id, "process", 2)
        _flush(ledger, attempt_id, budgets)

        flushed = ledger.get_latest_usage(attempt_id)
        assert flushed is not None
        assert flushed["usage"] == budgets.attempt_totals().as_readable()
        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        budget = current["budget"]
        assert budget["effective_tokens"]["value"] == 750
        assert budget["raw_tokens"]["value"] == 1650
        used = _limits_used(budget)
        assert used["tool_calls"]["used"]["value"] == 3
        assert used["cost_usd"]["used"]["value"] == pytest.approx(0.015)
    finally:
        ledger.close()


def test_reopened_ledger_returns_last_flush_and_restores_meter(
    tmp_path: Path,
) -> None:
    """A restart reads the last flush back and keeps accumulating on top."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        ledger.open_step_execution(attempt_id, "process", 1)
        clock = _FakeClock()
        budgets = _budgets(make_workflow(), clock, attempt_id)
        clock.advance(60.0)
        settled = UsageTotals(
            tokens=1100,
            effective_tokens=200,
            cost_usd=0.01,
            tool_calls=1,
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=900,
        )
        budgets.add_spend("process", settled)
        _flush(ledger, attempt_id, budgets)
        before = ledger.get_latest_usage(attempt_id)
        assert before is not None
    finally:
        ledger.close()

    reopened = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        after = reopened.get_latest_usage(attempt_id)
        assert after == before
        current = _current(reopened, _live(attempt_id, 40, "Add a greeting"))
        assert current["budget"]["effective_tokens"]["value"] == 200

        fresh = _budgets(make_workflow(), _FakeClock(), attempt_id)
        restore_budgets_from_usage(fresh, after)
        extra = UsageTotals(
            tokens=550,
            effective_tokens=550,
            cost_usd=0.005,
            tool_calls=1,
            input_tokens=500,
            output_tokens=50,
        )
        fresh.add_spend("process", extra)
        reopened.record_usage(
            attempt_id,
            BudgetSnapshot(
                elapsed_seconds=fresh.elapsed_seconds(),
                usage=fresh.attempt_totals(),
            ),
        )
        resumed = reopened.get_latest_usage(attempt_id)
        assert resumed is not None
        assert resumed["usage"] == fresh.attempt_totals().as_readable()
        resumed_current = _current(
            reopened, _live(attempt_id, 40, "Add a greeting")
        )
        assert resumed_current["budget"]["effective_tokens"]["value"] == 750
        assert resumed_current["budget"]["raw_tokens"]["value"] == 1650
    finally:
        reopened.close()


def test_fallback_reports_raw_tokens_and_cache_hit_rate_without_flush(
    tmp_path: Path,
) -> None:
    """Flush-less ledgers keep raw buckets and the cache-hit rate."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        ledger.open_step_execution(attempt_id, "process", 1)
        ledger.end_step_execution(
            attempt_id,
            "process",
            1,
            result="done",
            usage={
                "tokens": 1200,
                "effective_tokens": 1000,
                "cost_usd": 0.02,
                "tool_calls": 7,
                "input_tokens": 800,
                "output_tokens": 200,
                "cache_read_tokens": 200,
                "cache_write_tokens": 0,
                "wall_clock_seconds": 42.0,
            },
        )
        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        budget = current["budget"]
        assert budget["raw_tokens"]["value"] == 1200
        assert budget["raw_tokens"]["basis"] == "measured"
        assert budget["effective_tokens"]["value"] == 1000
        figures = current["figures"]
        assert figures["cache_hit_rate"]["value"] == pytest.approx(200 / 800)
        assert figures["cache_hit_rate"]["basis"] == "measured"
        assert figures["elapsed_seconds"]["value"] == pytest.approx(42.0)
        assert figures["tool_calls"]["value"] == 7
    finally:
        ledger.close()


# --- The agent step flushes per settled response ------------------------------


def test_agent_step_flushes_usage_per_settled_response(
    tmp_path: Path,
) -> None:
    """Every settled response and tool call lands in the Attempt flush."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        workspace = tmp_path / "work"
        workspace.mkdir()
        (workspace / "README.md").write_text("hello\n", encoding="utf-8")
        clock = _FakeClock()
        budgets = _budgets(make_workflow(), clock, attempt_id)
        model = ScriptedModel(
            [
                AIMessage(
                    content="working",
                    tool_calls=[
                        _call(
                            "write_file",
                            {"path": "note.txt", "content": "from step one"},
                            "w1",
                        )
                    ],
                    usage_metadata=_usage_dict(1000, 100, cache_read=900),
                ),
                _report_ai("done", {}, _usage_dict(500, 50)),
            ]
        )
        deps = make_agent_step_deps(
            model=model,
            workspace=workspace,
            tmp_path=tmp_path,
            attempt=make_attempt(
                attempt_id=attempt_id,
                input_number=40,
                title="Add a greeting",
            ),
            ledger=ledger,
            budgets=budgets,
        )
        outcome = run_agent_step(_agent_step("process"), 1, deps, {})
        assert outcome.status == "reported"

        flushed = ledger.get_latest_usage(attempt_id)
        assert flushed is not None
        assert flushed["usage"] == budgets.attempt_totals().as_readable()
        assert flushed["usage"]["tokens"] == 1650
        assert flushed["usage"]["effective_tokens"] == 750
        assert flushed["usage"]["tool_calls"] == 1
        assert flushed["usage"]["input_tokens"] == 1500
        assert flushed["usage"]["cache_read_tokens"] == 900
        assert isinstance(flushed.get("elapsed_seconds"), (int, float))

        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        assert current["budget"]["effective_tokens"]["value"] == 750
        assert current["budget"]["raw_tokens"]["value"] == 1650
        assert current["figures"]["tool_calls"]["value"] == 1
        assert current["figures"]["cache_hit_rate"]["value"] == pytest.approx(
            900 / 1500
        )
    finally:
        ledger.close()


def test_agent_step_response_without_usage_stays_upper_bound(
    tmp_path: Path,
) -> None:
    """A response without usage settles worst-case and stays estimated."""
    attempt_id = "#40/1"
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, attempt_id)
        workspace = tmp_path / "work"
        workspace.mkdir()
        (workspace / "README.md").write_text("hello\n", encoding="utf-8")
        clock = _FakeClock()
        budgets = _budgets(make_workflow(), clock, attempt_id)
        model = ScriptedModel([_report_ai("done", {}, None)])
        deps = make_agent_step_deps(
            model=model,
            workspace=workspace,
            tmp_path=tmp_path,
            attempt=make_attempt(
                attempt_id=attempt_id,
                input_number=40,
                title="Add a greeting",
            ),
            ledger=ledger,
            budgets=budgets,
        )
        outcome = run_agent_step(_agent_step("process"), 1, deps, {})
        assert outcome.status == "reported"

        flushed = ledger.get_latest_usage(attempt_id)
        assert flushed is not None
        current = _current(ledger, _live(attempt_id, 40, "Add a greeting"))
        assert current["budget"]["effective_tokens"]["basis"] == "estimated"
        assert "upper bounds" in str(
            current["budget"]["effective_tokens"].get("reason")
        )
    finally:
        ledger.close()
