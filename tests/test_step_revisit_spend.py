"""A revisited step records only its own spend per Step Execution.

Regression coverage for the usage double-count: the stored ``usage`` (and
the Execution Summary ``spend``) used to carry the step's cumulative meter,
so a revisited step's later executions re-counted every earlier execution.
Each execution now records the meter delta between its open and its end,
while the Step Allowance still accumulates across executions (§11.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.operator.step_projection import spend_from_executions
from coding_agent.runtime.graph.agent_step import (
    MAX_REPORT_CORRECTIONS,
    REPORT_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.workflow.schema import (
    Agent,
    FieldType,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps, make_budgets, make_workflow
from support.scripted_model import ScriptedModel


def _facts() -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id="implement#7/1",
        workflow_id="implement",
        input_type="issue",
        input_number=7,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, FieldType]],
    *,
    tools: tuple[str, ...] = ("workspace-read",),
    limits: dict[str, float] | None = None,
    max_executions: int = 10,
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results=results),
        max_executions=max_executions,
        limits=dict(limits or {}),
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {"cache_read": 0, "cache_creation": 0},
    }


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _report_ai(
    result: str,
    payload: dict[str, object],
    usage: dict[str, object],
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


def _deps(
    attempt: StepAttemptFacts,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    **overrides: Any,
) -> AgentStepDeps:
    return make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=attempt,
        **overrides,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


def test_revisited_step_records_only_its_own_spend(
    workspace: Path, tmp_path: Path
) -> None:
    """A second execution of a step stores its own spend, not the total."""
    workflow = make_workflow("implement")
    step = _agent_step("process", {"done": {}})
    workflow = Workflow(
        id=workflow.id,
        model=workflow.model,
        input=workflow.input,
        ceiling=workflow.ceiling,
        start=workflow.start,
        steps={"process": step},
    )
    budgets = make_budgets(workflow, attempt_id="implement#7/1")
    model = ScriptedModel(
        [
            _report_ai("done", {}, _usage(100, 10)),
            _report_ai("done", {}, _usage(100, 10)),
        ]
    )
    deps = _deps(_facts(), model, workspace, tmp_path, budgets=budgets)

    first = run_agent_step(step, 1, deps, {})
    second = run_agent_step(step, 2, deps, {})

    assert first.status == "reported"
    assert second.status == "reported"
    # Each execution spent 100 input + 10 output tokens of its own.
    assert first.usage["tokens"] == 110
    assert second.usage["tokens"] == 110
    assert second.usage["effective_tokens"] == 110
    assert second.usage["cost_usd"] == pytest.approx(0.00012)
    assert second.usage["input_tokens"] == 100
    assert second.usage["output_tokens"] == 10
    # The cumulative meter still holds both executions.
    assert budgets.step_totals("process").tokens == 220
    assert budgets.attempt_totals().tokens == 220


def test_revisit_spend_sums_to_the_attempt_meter(
    workspace: Path, tmp_path: Path
) -> None:
    """Summing executions over a revisit matches the Attempt meter."""
    workflow = make_workflow("implement")
    step = _agent_step("process", {"done": {}})
    workflow = Workflow(
        id=workflow.id,
        model=workflow.model,
        input=workflow.input,
        ceiling=workflow.ceiling,
        start=workflow.start,
        steps={"process": step},
    )
    budgets = make_budgets(workflow, attempt_id="implement#7/1")
    model = ScriptedModel(
        [
            _report_ai("done", {}, _usage(100, 10)),
            _report_ai("done", {}, _usage(100, 10)),
        ]
    )
    deps = _deps(_facts(), model, workspace, tmp_path, budgets=budgets)

    first = run_agent_step(step, 1, deps, {})
    second = run_agent_step(step, 2, deps, {})
    assert first.status == "reported"
    assert second.status == "reported"

    # Each summary agrees with its own stored usage.
    for outcome in (first, second):
        spend = outcome.summary["spend"]
        assert isinstance(spend, dict)
        assert spend == dict(outcome.usage)

    spend = spend_from_executions(
        [{"usage": dict(first.usage)}, {"usage": dict(second.usage)}]
    )
    attempt = budgets.attempt_totals()
    assert spend["effective_tokens"] == attempt.effective_tokens == 220
    assert spend["cost_usd"] == pytest.approx(attempt.cost_usd)


def test_step_limit_still_accumulates_across_executions(
    workspace: Path, tmp_path: Path
) -> None:
    """Combined spend over two executions still crosses the Step Allowance."""
    workflow = make_workflow("implement")
    step = _agent_step(
        "process", {"done": {}}, tools=("shell",), limits={"tool-calls": 2.0}
    )
    workflow = Workflow(
        id=workflow.id,
        model=workflow.model,
        input=workflow.input,
        ceiling=workflow.ceiling,
        start=workflow.start,
        steps={"process": step},
    )
    budgets = make_budgets(workflow, attempt_id="implement#7/1")

    def _working(call_id: str, report_id: str) -> AIMessage:
        return AIMessage(
            content="working",
            tool_calls=[
                _call("run_command", {"command": "echo one"}, f"{call_id}-1"),
                _call("run_command", {"command": "echo two"}, f"{call_id}-2"),
                _call(
                    REPORT_TOOL_NAME,
                    {"result": "done", "payload_json": "{}"},
                    report_id,
                ),
            ],
            usage_metadata=_usage(50, 10),
        )

    model = ScriptedModel([_working("c1", "report-1"), _working("c2", "report-2")])
    deps = _deps(_facts(), model, workspace, tmp_path, budgets=budgets)

    first = run_agent_step(step, 1, deps, {})
    assert first.status == "reported"
    assert first.usage["tool_calls_meter"] == 2
    # The second execution's two calls cross the allowance of two: the
    # Step Allowance still accumulates across executions.
    second = run_agent_step(step, 2, deps, {})

    assert second.status == "limit-exceeded"
    assert second.limit_fact is not None
    assert second.limit_fact["scope"] == "step"
    # The refused execution counts only its own two calls and one response.
    assert second.usage["tool_calls_meter"] == 2
    assert second.usage["tokens"] == 60
    # The cumulative meter still holds both executions.
    assert budgets.step_totals("process").tool_calls == 4
    assert budgets.step_totals("process").tokens == 120


def test_limit_exceeded_after_spend_records_only_its_own_spend(
    workspace: Path, tmp_path: Path
) -> None:
    """A tool-call crossing mid-execution stores the execution's own delta."""
    workflow = make_workflow("implement")
    step = _agent_step(
        "process", {"done": {}}, tools=("shell",), limits={"tool-calls": 1.0}
    )
    workflow = Workflow(
        id=workflow.id,
        model=workflow.model,
        input=workflow.input,
        ceiling=workflow.ceiling,
        start=workflow.start,
        steps={"process": step},
    )
    budgets = make_budgets(workflow, attempt_id="implement#7/1")
    model = ScriptedModel(
        [
            AIMessage(
                content="working",
                tool_calls=[
                    _call("run_command", {"command": "echo one"}, "c1"),
                    _call(
                        REPORT_TOOL_NAME,
                        {"result": "done", "payload_json": "{}"},
                        "report-1",
                    ),
                ],
                usage_metadata=_usage(50, 10),
            ),
            AIMessage(
                content="working",
                tool_calls=[
                    _call("run_command", {"command": "echo two"}, "c2"),
                    _call(
                        REPORT_TOOL_NAME,
                        {"result": "done", "payload_json": "{}"},
                        "report-2",
                    ),
                ],
                usage_metadata=_usage(50, 10),
            ),
        ]
    )
    deps = _deps(_facts(), model, workspace, tmp_path, budgets=budgets)

    first = run_agent_step(step, 1, deps, {})
    assert first.status == "reported"
    assert first.usage["tool_calls_meter"] == 1

    second = run_agent_step(step, 2, deps, {})
    assert second.status == "limit-exceeded"
    assert second.limit_fact is not None
    assert second.limit_fact["moment"] == "tool-call"
    # The second execution spent one tool call and one response of its own.
    assert second.usage["tool_calls_meter"] == 1
    assert second.usage["tokens"] == 60
    # The cumulative meter still holds both executions.
    assert budgets.step_totals("process").tool_calls == 2
    assert budgets.step_totals("process").tokens == 120


def test_error_ending_records_only_its_own_spend(
    workspace: Path, tmp_path: Path
) -> None:
    """An error ending after a revisit stores the execution's own delta."""
    workflow = make_workflow("implement")
    step = _agent_step("process", {"done": {}})
    workflow = Workflow(
        id=workflow.id,
        model=workflow.model,
        input=workflow.input,
        ceiling=workflow.ceiling,
        start=workflow.start,
        steps={"process": step},
    )
    budgets = make_budgets(workflow, attempt_id="implement#7/1")
    responses = [_report_ai("done", {}, _usage(100, 10))]
    responses.extend(
        AIMessage(content="stalling", usage_metadata=_usage(50, 5))
        for _ in range(MAX_REPORT_CORRECTIONS + 1)
    )
    model = ScriptedModel(responses)
    deps = _deps(_facts(), model, workspace, tmp_path, budgets=budgets)

    first = run_agent_step(step, 1, deps, {})
    assert first.status == "reported"

    second = run_agent_step(step, 2, deps, {})
    assert second.status == "error"
    own_tokens = (MAX_REPORT_CORRECTIONS + 1) * 55
    assert second.usage["tokens"] == own_tokens
    assert second.usage["effective_tokens"] == own_tokens
    assert budgets.step_totals("process").tokens == 110 + own_tokens
    spend = second.summary["spend"]
    assert isinstance(spend, dict)
    assert spend == dict(second.usage)
