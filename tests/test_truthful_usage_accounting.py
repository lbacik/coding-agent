"""Truthful-usage accounting: missing usage metadata never reads as zero (#254).

Contract: adoption step 4 of the operator contract (#245). Usage accounting
records, per Attempt, how many model responses settled at their Admission
hold's worst case — without usage metadata, or with partial usage (§11.4).
When any did, the Status Snapshot's token figures and the cost keep their
values and carry basis ``upper-bound`` with a reason: a ceiling on the true
spend, never a measured zero and never ``unavailable``. Budget enforcement
charges the worst case; the reporting marks it as a bound.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.runtime.ceilings import (
    LoopCeilings,
    UsageTotals,
    ceiling_crossed,
    response_lacks_usage_metadata,
    response_usage,
)
from coding_agent.workflow.schema import Agent, RouteTarget, WorkflowStep

from datetime import datetime, timezone


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _live() -> LiveView:
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "limits": {
                "wall_clock_minutes": 60,
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
            }
        }
    }


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "run-ledger.sqlite3")


def _open(ledger: RunLedger, attempt_id: str) -> None:
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())


def _flush(ledger: RunLedger, attempt_id: str) -> None:
    ledger.record_usage(
        attempt_id,
        BudgetSnapshot(
            elapsed_seconds=90.0,
            usage=UsageTotals(
                tokens=1200,
                effective_tokens=1000,
                cost_usd=0.02,
                tool_calls=7,
            ),
        ),
    )


def _claim(live: LiveView, attempt_id: str = "#40/1") -> LiveView:
    live.set_claim(attempt_id, 40, "Add a greeting")
    live.set_phase("run_step")
    return live


def _step_claim(live: LiveView, attempt_id: str = "implement#40/1") -> LiveView:
    live.set_claim(attempt_id, 40, "Add a greeting")
    live.set_phase("process#1")
    return live


def _record_step_execution(
    ledger: RunLedger, attempt_id: str = "implement#40/1"
) -> None:
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())
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
        },
    )


def test_response_lacks_usage_metadata_only_counts_assistant_turns() -> None:
    """Only assistant turns without a usage dict lack metadata (§11)."""
    assert response_lacks_usage_metadata(HumanMessage(content="hi")) is False
    assert response_lacks_usage_metadata(AIMessage(content="no usage")) is True
    usage: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert response_lacks_usage_metadata(AIMessage(content="measured", usage_metadata=usage)) is False


def test_missing_response_adds_nothing_to_budget_totals() -> None:
    """Budget enforcement keeps its current arithmetic (§11, reporting only)."""
    spent = response_usage(AIMessage(content="no usage"), None)
    assert spent == UsageTotals()
    assert ceiling_crossed(spent, elapsed_seconds=1.0, ceilings=LoopCeilings()) is None
    measured = UsageTotals(tokens=1200, effective_tokens=1000, cost_usd=0.02)
    assert ceiling_crossed(
        measured + spent,
        elapsed_seconds=1.0,
        ceilings=LoopCeilings(max_effective_tokens=100_000),
    ) is None


def test_missing_counter_starts_at_zero_and_survives_restart(tmp_path: Path) -> None:
    """The per-Attempt counter is durable across a Worker restart (§11, §17)."""
    path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(path)
    try:
        _open(ledger, "#40/1")
        assert ledger.get_responses_without_usage("#40/1") == 0
        assert ledger.get_responses_without_usage("#40/unknown") == 0
        assert ledger.record_response_without_usage("#40/1") == 1
        assert ledger.record_response_without_usage("#40/1") == 2
        assert ledger.get_responses_without_usage("#40/1") == 2
    finally:
        ledger.close()
    reopened = RunLedger(path)
    try:
        assert reopened.get_responses_without_usage("#40/1") == 2
    finally:
        reopened.close()


def test_older_ledger_file_without_counter_reads_as_complete(tmp_path: Path) -> None:
    """A pre-#254 ledger gains the counter on open, reading as complete."""
    path = tmp_path / "run-ledger.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                effective_configuration TEXT NOT NULL,
                effective_configuration_digest TEXT NOT NULL,
                fingerprint TEXT,
                base_revision TEXT,
                agent_identity_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    ledger = RunLedger(path)
    try:
        assert ledger.get_responses_without_usage("#40/1") == 0
    finally:
        ledger.close()


def test_measured_scripted_model_stays_measured(tmp_path: Path) -> None:
    """Responses carrying usage keep measured figures, as before (#254)."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush(ledger, "#40/1")
        result = operator_listener.build_status_result(ledger.path, _claim(_live()))
        current = result["current_attempt"]
        assert current is not None
        assert current["budget"]["raw_tokens"] == {
            "value": 1200,
            "unit": "tokens",
            "basis": "measured",
        }
        assert current["budget"]["effective_tokens"] == {
            "value": 1000,
            "unit": "tokens",
            "basis": "measured",
        }
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        assert limits["cost_usd"]["used"]["value"] == 0.02
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
        assert ledger.get_responses_without_usage("#40/1") == 0
    finally:
        ledger.close()


def test_missing_usage_reports_upper_bound_with_reason_current(tmp_path: Path) -> None:
    """One worst-case charge keeps figures and marks them upper-bound (#343)."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush(ledger, "#40/1")
        ledger.record_response_without_usage("#40/1")
        result = operator_listener.build_status_result(ledger.path, _claim(_live()))
        current = result["current_attempt"]
        assert current is not None
        assert current["budget"]["raw_tokens"] == {
            "value": 1200,
            "unit": "tokens",
            "basis": "measured",
        }
        assert current["budget"]["effective_tokens"]["value"] == 1000
        assert current["budget"]["effective_tokens"]["basis"] == "estimated"
        assert isinstance(current["budget"]["effective_tokens"].get("reason"), str)
        assert current["budget"]["effective_tokens"]["reason"] != ""
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        assert limits["effective_tokens"]["used"]["value"] == 1000
        assert limits["effective_tokens"]["used"]["basis"] == "estimated"
        assert isinstance(limits["effective_tokens"]["used"].get("reason"), str)
        assert limits["cost_usd"]["used"]["value"] == 0.02
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
        assert isinstance(limits["cost_usd"]["used"].get("reason"), str)
        assert ledger.get_responses_without_usage("#40/1") == 1
        # Non-token figures stay measured: reporting change only.
        assert current["figures"]["tool_calls"]["value"] == 7
        assert current["figures"]["elapsed_seconds"]["value"] == 90.0
    finally:
        ledger.close()


def test_missing_usage_reports_upper_bound_with_reason_last_finished(
    tmp_path: Path,
) -> None:
    """``last_finished`` carries the same upper-bound basis and reason (#343)."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush(ledger, "#40/1")
        ledger.record_response_without_usage("#40/1")
        ledger.record_response_without_usage("#40/1")
        ledger.record_terminal_outcome(
            "#40/1", outcome="delivered", reason="verified-completion"
        )
        ledger.set_attempt_state("#40/1", "finished")
        result = operator_listener.build_status_result(ledger.path, _live())
        assert result["current_attempt"] is None
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "#40/1"
        assert last["outcome"] == {
            "native": "delivered",
            "reason": "verified-completion",
            "label": "delivered (verified-completion)",
        }
        assert ledger.get_responses_without_usage("#40/1") == 2
    finally:
        ledger.close()


def test_measured_last_finished_stays_measured(tmp_path: Path) -> None:
    """A fully-measured Attempt keeps measured figures in ``last_finished``."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush(ledger, "#40/1")
        ledger.record_terminal_outcome(
            "#40/1", outcome="delivered", reason="verified-completion"
        )
        ledger.set_attempt_state("#40/1", "finished")
        result = operator_listener.build_status_result(ledger.path, _live())
        last = result["last_finished"]
        assert last is not None
        assert last["outcome"] == {
            "native": "delivered",
            "reason": "verified-completion",
            "label": "delivered (verified-completion)",
        }
        assert ledger.get_responses_without_usage("#40/1") == 0
    finally:
        ledger.close()


def _spawn_step() -> WorkflowStep:
    """A one-step workflow step whose agent may spawn Sub-agents and report."""
    return WorkflowStep(
        id="process",
        agent=Agent(tools=("workspace-read", "subagents"), results={"done": {}}),
        max_executions=1,
        limits={},
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )


def test_subagent_missing_usage_marks_ledger_once(tmp_path: Path) -> None:
    """A Sub-agent response without metadata marks the Attempt once (#254).

    The production spawn path (``run_agent_step`` through the shared
    model-request lifecycle) records the missing-usage ledger marker for the
    member's response, and the run still reports.
    """
    import json as _json

    from coding_agent.runtime.graph.agent_step import REPORT_TOOL_NAME, run_agent_step
    from coding_agent.workflow.schema import AttemptCeiling, InputDecl, Workflow
    from support.agent_step_deps import make_agent_step_deps, make_attempt
    from support.scripted_model import ScriptedModel

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    attempt_id = "implement#7/1"
    step = _spawn_step()
    workflow = Workflow(
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
    ledger = _ledger(tmp_path)
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())
    from coding_agent.runtime.step_limits import build_budgets
    from coding_agent.provider.price_table import TokenPrices

    prices = TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)
    budgets = build_budgets(
        workflow, prices=prices, output_cap=0, attempt_id=attempt_id
    )
    main_usage: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "spawn_subagent",
                        "args": {
                            "description": "review the change",
                            "instructions": "review the change",
                        },
                        "id": "s1",
                        "type": "tool_call",
                    }
                ],
                usage_metadata=dict(main_usage),
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "report_subagent",
                        "args": {"report": "no findings"},
                        "id": "sub-report-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="reporting",
                tool_calls=[
                    {
                        "name": REPORT_TOOL_NAME,
                        "args": {
                            "result": "done",
                            "payload_json": _json.dumps({}),
                        },
                        "id": "report-1",
                        "type": "tool_call",
                    }
                ],
                usage_metadata=dict(main_usage),
            ),
        ]
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=make_attempt(attempt_id=attempt_id),
        ledger=ledger,
        budgets=budgets,
        prices=prices,
    )
    try:
        outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
        assert outcome.status == "reported"
        assert ledger.get_responses_without_usage(attempt_id) == 1
    finally:
        ledger.close()


def test_step_snapshot_marks_worst_case_charge_upper_bound_current(
    tmp_path: Path,
) -> None:
    """A step Attempt with a worst-case charge reports upper-bound (#343)."""
    ledger = _ledger(tmp_path)
    try:
        _record_step_execution(ledger)
        ledger.record_usage(
            "implement#40/1",
            BudgetSnapshot(
                elapsed_seconds=90.0,
                usage=UsageTotals(
                    tokens=1200,
                    effective_tokens=1000,
                    cost_usd=0.02,
                    tool_calls=7,
                ),
            ),
        )
        ledger.record_response_without_usage("implement#40/1")
        result = operator_listener.build_status_result(
            ledger.path, _step_claim(_live())
        )
        current = result["current_attempt"]
        assert current is not None
        assert current["attempt_id"] == "implement#40/1"
        assert current["native_phase"] == "process#1"
        assert ledger.get_responses_without_usage("implement#40/1") == 1
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        assert limits["effective_tokens"]["used"]["basis"] == "estimated"
        assert (
            isinstance(limits["effective_tokens"]["used"].get("reason"), str)
            and limits["effective_tokens"]["used"]["reason"] != ""
        )
        assert limits["cost_usd"]["used"]["value"] == 0.02
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
        assert (
            isinstance(limits["cost_usd"]["used"].get("reason"), str)
            and limits["cost_usd"]["used"]["reason"] != ""
        )
    finally:
        ledger.close()


def test_step_snapshot_marks_worst_case_charge_upper_bound_last_finished(
    tmp_path: Path,
) -> None:
    """``last_finished`` on the step path carries the upper-bound basis (#343)."""
    ledger = _ledger(tmp_path)
    try:
        _record_step_execution(ledger)
        ledger.record_response_without_usage("implement#40/1")
        ledger.record_terminal_outcome(
            "implement#40/1", outcome="delivered", reason="verified-completion"
        )
        ledger.set_attempt_state("implement#40/1", "finished")
        result = operator_listener.build_status_result(ledger.path, _live())
        assert result["current_attempt"] is None
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "implement#40/1"
        assert ledger.get_responses_without_usage("implement#40/1") == 1
        assert last["outcome"] is not None
    finally:
        ledger.close()
