"""``ExecutionTally``: the agent loop's counters and outcome builders.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (one
conversation per Step Execution) and §13.2 (Step Execution ends).

Part of #487 (slice 1 of 4). A pure unit suite: every outcome the agent
loop can return is built here directly, without running ``run_agent_step``.
The hand-built shapes (prefix-too-large, missing inputs) reproduce today's
field shapes exactly and are asserted, never cleaned up.
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
        "by_origin",
    }
)
"""The meter delta keys behind every metered outcome (§11.2)."""

_COUNTER_KEYS = frozenset(
    {"model_calls", "tool_calls", "subagent_reports"}
)
"""The three usage counters carried by every metered outcome."""


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
    """The loop opens with zero counters and no events (§9.6)."""
    tally = _tally(tmp_path)
    assert tally.model_calls == 0
    assert tally.tool_calls == 0
    assert tally.subagent_reports == 0
    assert tally.corrections == 0
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
    tally.corrections = 3
    outcome = tally.error("no Step Result report after bounded correction")
    assert outcome.status == "error"
    assert outcome.detail == "no Step Result report after bounded correction"
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.limit_fact is None
    assert outcome.summary["limit"] is None
    assert "snapshot_digest" not in outcome.summary
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


def _zero_by_origin() -> dict[str, object]:
    """Zero per-origin cells, in flush order, as readable payload."""
    from coding_agent.runtime.ceilings import ORIGINS, OriginCell

    return {origin: OriginCell().as_readable() for origin in ORIGINS}


def test_prefix_too_large_shape(tmp_path: Path) -> None:
    """The prefix-too-large shape: zero usage, no events, a step fact (§9.2)."""
    tally = _tally(tmp_path)
    outcome = tally.prefix_too_large()
    assert outcome.status == "error"
    assert outcome.detail == "the Pinned Prefix does not fit the usable context"
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    for key in (
        "model_calls",
        "tool_calls",
        "subagent_reports",
        "tokens",
        "effective_tokens",
        "tool_calls_meter",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        assert outcome.usage[key] == 0
    assert outcome.usage["cost_usd"] == 0.0
    assert outcome.usage["by_origin"] == _zero_by_origin()
    assert outcome.limit_fact == {"scope": "step", "reason": "prefix-too-large"}
    assert outcome.summary["limit"] == {"scope": "step", "reason": "prefix-too-large"}
    spend = outcome.summary["spend"]
    assert isinstance(spend, dict)
    assert set(spend) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert spend["by_origin"] == _zero_by_origin()
    assert outcome.events == ()
    assert outcome.corrections == 0


def test_missing_inputs_shape(tmp_path: Path) -> None:
    """The missing-inputs shape: zero usage, no events, no limit fact (§9.7)."""
    tally = _tally(tmp_path)
    outcome = tally.missing_inputs("required input 'x' is absent")
    assert outcome.status == "error"
    assert outcome.detail == "required input 'x' is absent"
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    for key in (
        "model_calls",
        "tool_calls",
        "subagent_reports",
        "tokens",
        "effective_tokens",
        "tool_calls_meter",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        assert outcome.usage[key] == 0
    assert outcome.usage["cost_usd"] == 0.0
    assert outcome.usage["by_origin"] == _zero_by_origin()
    spend = outcome.summary["spend"]
    assert isinstance(spend, dict)
    assert set(spend) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.limit_fact is None
    assert outcome.summary["limit"] is None
    assert outcome.events == ()


def test_context_threshold_shape(tmp_path: Path) -> None:
    """The context-threshold end: ``limit-exceeded``, metered usage (§9.9)."""
    from coding_agent.runtime.conversation_context import ConversationContext

    tally = _tally(tmp_path)
    tally.model_calls = 2
    tally.tool_calls = 3
    tally.subagent_reports = 4
    tally.corrections = 1
    tally.record_event("model-response", "response 0", {"response_index": 0})
    crossing = ConversationContext(
        value=5047,
        basis="anchored",
        anchor_input_tokens=4000,
        anchor_request_ref="step/process/1#response-1",
        appended_estimate_tokens=1047,
    )
    outcome = tally.context_threshold(crossing, 3970)
    assert outcome.status == "limit-exceeded"
    assert outcome.detail == (
        "the conversation context (5047 tokens, anchored) would exceed the "
        "usable threshold (3970 tokens)"
    )
    assert set(outcome.usage) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.usage["model_calls"] == 2
    assert outcome.usage["subagent_reports"] == 4
    expected = {
        "scope": "step",
        "reason": "context-threshold",
        "conversation_context": crossing.to_record(),
        "usable_threshold": 3970,
    }
    assert outcome.limit_fact == expected
    assert outcome.summary["limit"] == expected
    spend = outcome.summary["spend"]
    assert isinstance(spend, dict)
    assert set(spend) == set(_COUNTER_KEYS) | set(_METER_KEYS)
    assert outcome.corrections == 1
    assert [event["kind"] for event in outcome.events] == [
        "model-response",
        "context_threshold",
    ]
    assert outcome.events[-1]["payload"] == {
        "conversation_context": crossing.to_record(),
        "usable_threshold": 3970,
    }
