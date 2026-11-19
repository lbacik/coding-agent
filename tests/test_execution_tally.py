"""``ExecutionTally``: the agent loop's counters and outcome builders.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (one
conversation per Step Execution) and §13.2 (Step Execution ends).

Part of #487 (slice 1 of 4). A pure unit suite: every outcome the agent
loop can return is built here directly, without running ``run_agent_step``.
The hand-built shapes (prefix-too-large, missing inputs, handoff-count,
handoff-failed) reproduce today's field shapes exactly — including the
missing ``subagent_reports`` and meter fields — and are asserted, never
cleaned up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.runtime.graph.agent_step import ExecutionTally
from coding_agent.runtime.step_limits import LimitFact
from coding_agent.workflow.schema import Agent, RouteTarget, WorkflowStep
from support.agent_step_deps import make_agent_step_deps
from support.scripted_model import ScriptedModel

_METER_KEYS = frozenset(
    {
        "tokens",
        "effective_tokens",
        "cost_usd",
        "tool_calls_meter",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    }
)
"""The meter delta keys behind every metered outcome (§11.2)."""

_COUNTER_KEYS = frozenset(
    {"model_calls", "tool_calls", "subagent_reports", "handoffs"}
)
"""The four usage counters carried by every metered outcome."""


def _step() -> WorkflowStep:
    """One `agent` step declaring a single `done` result with a note."""
    return WorkflowStep(
        id="process",
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


def _tally(tmp_path: Path) -> ExecutionTally:
    """A fresh tally over scripted deps, the way the loop opens one."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    step = _step()
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
    )
    return ExecutionTally(
        deps=deps,
        step=step,
        seq=1,
        baseline=deps.budgets.step_totals(step.id),
    )


def test_tally_starts_empty(tmp_path: Path) -> None:
    """The loop opens with zero counters, no digest and no events (§9.6)."""
    tally = _tally(tmp_path)
    assert tally.model_calls == 0
    assert tally.tool_calls == 0
    assert tally.subagent_reports == 0
    assert tally.handoffs == 0
    assert tally.corrections == 0
    assert tally.snapshot_digest is None
    assert tally.events == []


def test_record_event_appends_in_order(tmp_path: Path) -> None:
    """Ledger events keep the loop's kinds, payloads and order (§13.2)."""
    tally = _tally(tmp_path)
    tally.record_event("first", "first detail", {"n": 1})
    tally.record_event("second", "second detail", {"n": 2})
    assert [event["kind"] for event in tally.events] == ["first", "second"]
    assert [event["detail"] for event in tally.events] == [
        "first detail",
        "second detail",
    ]
    outcome = tally.error("boom")
    assert tuple(outcome.events) == tuple(tally.events)


def test_limit_builds_limit_exceeded(tmp_path: Path) -> None:
    """``limit`` ends on the crossed fact with metered usage (§11.7)."""
    tally = _tally(tmp_path)
    tally.model_calls = 2
    tally.tool_calls = 3
    tally.subagent_reports = 1
    tally.handoffs = 0
    tally.record_event("model-response", "response 0", {"response_index": 0})
    fact = LimitFact(
        scope="step",
        dimensions=("tool-calls",),
        limit={"tool-calls": 5.0},
        spend={"tool-calls": 9.0},
        moment="tool-call",
    )
    outcome = tally.limit(fact, "the spend crossed the step limit")
    assert outcome.status == "limit-exceeded"
    assert outcome.detail == "the spend crossed the step limit"
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.usage["model_calls"] == 2
    assert outcome.usage["tool_calls"] == 3
    assert outcome.usage["subagent_reports"] == 1
    assert outcome.limit_fact == fact.to_dict()
    assert outcome.summary["limit"] == fact.to_dict()
    assert outcome.handoffs == 0
    assert [event["kind"] for event in outcome.events] == ["model-response"]


def test_deadline_falls_back_to_attempt_ceiling_fact(tmp_path: Path) -> None:
    """``deadline`` falls back where settled spend crossed nothing (§11.5)."""
    tally = _tally(tmp_path)
    outcome = tally.deadline("the remaining wall-clock time is exhausted")
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact == {
        "scope": "attempt-ceiling",
        "dimensions": ["wall-clock-minutes"],
        "limit": {},
        "spend": {},
        "moment": "deadline",
    }
    assert outcome.summary["limit"] == outcome.limit_fact
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)


def test_deadline_restamps_crossed_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``deadline`` keeps the crossed fact, re-stamped at the deadline."""
    tally = _tally(tmp_path)
    crossed = LimitFact(
        scope="step",
        dimensions=("wall-clock-minutes",),
        limit={"wall-clock-minutes": 30.0},
        spend={"wall-clock-minutes": 31.0},
        moment="before-request",
    )

    def _check_after_spend(step_id: str, *, moment: str = "before-request") -> LimitFact | None:
        _ = (step_id, moment)
        return crossed

    monkeypatch.setattr(
        tally.deps.budgets, "check_after_spend", _check_after_spend
    )
    outcome = tally.deadline("the model request ran into the remaining time")
    assert outcome.status == "limit-exceeded"
    limit_fact = outcome.limit_fact
    assert limit_fact is not None
    assert limit_fact["scope"] == "step"
    assert limit_fact["dimensions"] == ["wall-clock-minutes"]
    assert limit_fact["moment"] == "deadline"
    assert limit_fact["limit"] == {"wall-clock-minutes": 30.0}
    assert limit_fact["spend"] == {"wall-clock-minutes": 31.0}


def test_error_builds_error_outcome(tmp_path: Path) -> None:
    """``error`` ends ``error`` with metered usage and no limit fact (§9.6)."""
    tally = _tally(tmp_path)
    tally.model_calls = 1
    tally.tool_calls = 2
    tally.subagent_reports = 0
    tally.handoffs = 1
    tally.corrections = 3
    tally.snapshot_digest = "digest-1"
    outcome = tally.error("no Step Result report after bounded correction")
    assert outcome.status == "error"
    assert outcome.detail == "no Step Result report after bounded correction"
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.limit_fact is None
    assert outcome.summary["limit"] is None
    assert outcome.summary["snapshot_digest"] == "digest-1"
    assert outcome.handoffs == 1
    assert outcome.corrections == 3


def test_error_with_limit_reaches_summary_only(tmp_path: Path) -> None:
    """The backstop ``error`` carries its record in the summary, not a fact."""
    tally = _tally(tmp_path)
    limit = {"scope": "step", "reason": "iteration-backstop", "backstop": 213}
    outcome = tally.error("the tool loop exceeded its iteration backstop", limit=limit)
    assert outcome.status == "error"
    assert outcome.summary["limit"] == limit
    assert outcome.limit_fact is None


def test_reported_builds_reported_outcome(tmp_path: Path) -> None:
    """``reported`` ends with the named Step Result and its payload (§9.5)."""
    tally = _tally(tmp_path)
    tally.model_calls = 1
    tally.tool_calls = 1
    outcome = tally.reported("done", {"note": "hello"})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert outcome.payload == {"note": "hello"}
    assert outcome.detail == "reported done"
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.limit_fact is None
    assert outcome.summary["limit"] is None


def test_prefix_too_large_shape(tmp_path: Path) -> None:
    """The prefix-too-large shape: zero usage, no events, a step fact (§9.2)."""
    tally = _tally(tmp_path)
    outcome = tally.prefix_too_large()
    assert outcome.status == "error"
    assert outcome.detail == "the Pinned Prefix does not fit the usable context"
    assert outcome.usage == {"model_calls": 0, "tool_calls": 0, "handoffs": 0}
    assert outcome.limit_fact == {"scope": "step", "reason": "prefix-too-large"}
    assert outcome.summary["limit"] == {"scope": "step", "reason": "prefix-too-large"}
    assert outcome.summary["spend"] == {
        "model_calls": 0,
        "tool_calls": 0,
        "handoffs": 0,
    }
    assert outcome.events == ()
    assert outcome.handoffs == 0
    assert outcome.corrections == 0


def test_missing_inputs_shape(tmp_path: Path) -> None:
    """The missing-inputs shape: zero usage, no events, no limit fact (§9.7)."""
    tally = _tally(tmp_path)
    outcome = tally.missing_inputs("required input 'x' is absent")
    assert outcome.status == "error"
    assert outcome.detail == "required input 'x' is absent"
    assert outcome.usage == {"model_calls": 0, "tool_calls": 0, "handoffs": 0}
    assert outcome.limit_fact is None
    assert outcome.summary["limit"] is None
    assert outcome.events == ()


def test_handoff_count_shape(tmp_path: Path) -> None:
    """The handoff-count shape: meter usage, no ``subagent_reports`` (§9.9)."""
    tally = _tally(tmp_path)
    tally.model_calls = 2
    tally.tool_calls = 3
    tally.subagent_reports = 4
    tally.handoffs = 5
    tally.corrections = 1
    tally.record_event("model-response", "response 0", {"response_index": 0})
    outcome = tally.handoff_count()
    assert outcome.status == "limit-exceeded"
    assert outcome.detail == "exceeded the code-owned handoff count"
    assert "subagent_reports" not in outcome.usage
    assert set(outcome.usage) == {"model_calls", "tool_calls", "handoffs"} | set(
        _METER_KEYS
    )
    assert outcome.usage["model_calls"] == 2
    spend = outcome.summary["spend"]
    assert isinstance(spend, dict)
    assert "subagent_reports" not in spend
    assert outcome.limit_fact == {"scope": "step", "reason": "handoff-count"}
    assert outcome.summary["limit"] == {"scope": "step", "reason": "handoff-count"}
    assert outcome.handoffs == 5
    assert outcome.corrections == 1
    assert len(outcome.events) == 1


def test_handoff_failed_shape_has_no_meter_fields(tmp_path: Path) -> None:
    """The handoff-failed shape: only the three counters, no meter (§9.9)."""
    tally = _tally(tmp_path)
    tally.model_calls = 1
    tally.tool_calls = 2
    tally.subagent_reports = 3
    tally.handoffs = 1
    tally.record_event("model-response", "response 0", {"response_index": 0})
    outcome = tally.handoff_failed(ValueError("no handoff producer"))
    assert outcome.status == "error"
    assert outcome.detail == "failed Context-Window Handoff: no handoff producer"
    assert outcome.usage == {"model_calls": 1, "tool_calls": 2, "handoffs": 1}
    spend = outcome.summary["spend"]
    assert spend == {"model_calls": 1, "tool_calls": 2, "handoffs": 1}
    assert outcome.limit_fact is None
    assert outcome.summary["limit"] is None
    assert len(outcome.events) == 1
