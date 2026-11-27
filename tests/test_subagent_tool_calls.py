"""Sub-agent tool calls count against the calling step and the Attempt (#567).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.8 (a
Sub-agent's billable tool-call batch is charged before it runs; a crossing
batch is not executed, ends the member ``no-report`` / ``ceiling`` and, once
the group settles, ends the calling Step Execution ``limit-exceeded``) and
§11.5 (tool calls count at request time, moment ``tool-call``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import REPORT_SUBAGENT_TOOL
from coding_agent.runtime.step_limits import AttemptBudgets, build_budgets
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps, make_pin
from support.scripted_model import ScriptedModel

ATTEMPT_ID = "implement#7/1"


def _prices() -> Any:
    from support.agent_step_deps import make_prices

    return make_prices()


def _ceiling(tool_calls: float = 10000.0) -> AttemptCeiling:
    return AttemptCeiling(
        cost_usd=100000.0,
        effective_tokens=100000.0,
        tool_calls=tool_calls,
        wall_clock_minutes=600.0,
    )


def _agent_step(
    step_id: str, *, limits: dict[str, float] | None = None
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(
            tools=("workspace-read", "subagents"),
            results={"done": {}},
        ),
        max_executions=1,
        limits=dict(limits or {}),
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )


def _workflow(step: WorkflowStep, *, ceiling_calls: float = 10000.0) -> Workflow:
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=_ceiling(ceiling_calls),
        start=step.id,
        steps={step.id: step},
    )
    return workflow


def _facts() -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id=ATTEMPT_ID,
        workflow_id="implement",
        input_type="issue",
        input_number=7,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )


def _budgets(step: WorkflowStep, *, ceiling_calls: float = 10000.0) -> AttemptBudgets:
    return build_budgets(
        _workflow(step, ceiling_calls=ceiling_calls),
        prices=_prices(),
        output_cap=0,
        attempt_id=ATTEMPT_ID,
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _ai(
    calls: list[dict[str, object]] | None = None,
    *,
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content="", tool_calls=list(calls or []), usage_metadata=usage
    )


def _report_ai(
    result: str, payload: dict[str, object], usage: dict[str, object] | None
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


def _spawn_call(
    description: str, instructions: str, call_id: str
) -> dict[str, object]:
    return _call(
        "spawn_subagent",
        {"description": description, "instructions": instructions},
        call_id,
    )


def _report_subagent_call(report: str, call_id: str = "sub-1") -> dict[str, object]:
    return _call(REPORT_SUBAGENT_TOOL, {"report": report}, call_id)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _run(
    step: WorkflowStep,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
) -> Any:
    import sqlite3 as _sqlite3

    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except _sqlite3.IntegrityError:
        pass
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
    )
    return run_agent_step(_workflow(step).steps[step.id], 1, deps, {})


def _count_subagent_tool_runs(
    monkeypatch: pytest.MonkeyPatch, counter: list[int]
) -> None:
    """Count every Sub-agent tool invocation behind the member."""
    import coding_agent.runtime.graph.subagent as subagent_module
    import coding_agent.runtime.graph.subagent_member as member_module

    real_build = subagent_module.build_subagent_tools

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        tools = real_build(*args, **kwargs)
        for tool in tools:
            if tool.name == REPORT_SUBAGENT_TOOL:
                continue
            original = getattr(tool, "func")

            def _counting(
                *rest: Any, _original: Any = original, **rest_kw: Any
            ) -> Any:
                counter[0] += 1
                return _original(*rest, **rest_kw)

            monkeypatch.setattr(tool, "func", _counting)
        return tools

    # The member owns the single toolset build: patch where it looks it up.
    monkeypatch.setattr(member_module, "build_subagent_tools", _wrapped)
    monkeypatch.setattr(subagent_module, "build_subagent_tools", _wrapped)


def _capture_flushes(
    ledger: RunLedger, seen: list[dict[str, object]]
) -> None:
    real_record = ledger.record_usage

    def _recording(
        attempt_id: str, snapshot: Any, **kwargs: Any
    ) -> Any:
        try:
            seen.append(dict(snapshot.usage.as_readable()))
        except Exception:
            pass
        return real_record(attempt_id, snapshot, **kwargs)

    ledger.record_usage = _recording  # type: ignore[method-assign]


def test_subagent_tool_calls_count_against_step_and_attempt_and_flush(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Sub-agent reads land in the step, Attempt and flush (§9.8, §11.5)."""
    step = _agent_step("process")
    budgets = _budgets(step)
    tool_runs = [0]
    _count_subagent_tool_runs(monkeypatch, tool_runs)
    flushed: list[dict[str, object]] = []
    _capture_flushes(ledger, flushed)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage(10, 5),
            ),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r2")],
                usage=_usage(10, 5),
            ),
            _ai([_report_subagent_call("nothing to report")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 5,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    assert tool_runs[0] == 2
    # The caller's spawn plus the two Sub-agent reads; neither report counts.
    assert budgets.step_totals("process").tool_calls == 3
    assert budgets.attempt_totals().tool_calls == 3
    flushed_counts = [entry.get("tool_calls") for entry in flushed]
    assert 2 in flushed_counts and 3 in flushed_counts
    latest = ledger.get_latest_usage(ATTEMPT_ID)
    assert latest is not None
    assert latest["usage"]["tool_calls"] == 3
    # The Execution Summary carries the Sub-agent billable calls as well.
    summary = outcome.summary
    assert isinstance(summary, dict)
    spend = summary["spend"]
    assert isinstance(spend, dict)
    assert spend["tool_calls_meter"] == 3
    assert spend["tool_calls"] >= 3


def test_subagent_report_call_adds_no_tool_calls(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A Sub-agent that only reports charges nothing for its exit (§9.8)."""
    step = _agent_step("process")
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai([_report_subagent_call("no findings")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    # Only the caller's spawn_subagent call; the report exit is unmetered.
    assert budgets.step_totals("process").tool_calls == 1
    assert budgets.attempt_totals().tool_calls == 1


def test_subagent_batch_crossing_step_allowance_stops_step(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch crossing the Step Allowance never runs; the step ends limited."""
    step = _agent_step("process", limits={"tool-calls": 2.0})
    budgets = _budgets(step)
    tool_runs = [0]
    _count_subagent_tool_runs(monkeypatch, tool_runs)
    hold_ids: list[str] = []
    real_admit = budgets.admit_request

    def _admit(
        step_id: str, estimate_tokens: int, **kwargs: object
    ) -> Any:
        fact, hold_id = real_admit(step_id, estimate_tokens, **kwargs)  # type: ignore[arg-type]
        if hold_id is not None:
            hold_ids.append(hold_id)
        return fact, hold_id

    monkeypatch.setattr(budgets, "admit_request", _admit)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai(
                [
                    _call("read_file", {"path": "README.md"}, "r1"),
                    _call("read_file", {"path": "README.md"}, "r2"),
                ],
                usage=_usage(10, 5),
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "limit-exceeded"
    assert tool_runs[0] == 0
    # The crossing batch still counts toward the reported spend.
    assert budgets.step_totals("process").tool_calls == 3
    fact = outcome.limit_fact
    assert fact is not None
    assert fact["scope"] == "step"
    assert "tool-calls" in list(fact["dimensions"])
    assert fact["moment"] == "tool-call"
    assert len(model.invocations) == 2
    assert budgets.open_hold_ids() == ()
    for hold_id in hold_ids:
        stored = ledger.get_admission_hold(hold_id)
        assert stored is not None
        assert stored.status in ("settled", "charged")
    ended = [
        event for event in outcome.events if event["kind"] == "subagent_ended"
    ]
    assert [event["payload"]["call_id"] for event in ended] == ["s1"]
    assert ended[0]["payload"]["reported"] is False


def test_subagent_batch_crossing_ceiling_wins_precedence(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing step and ceiling together records the ceiling (§11.7)."""
    step = _agent_step("process", limits={"tool-calls": 2.0})
    budgets = _budgets(step, ceiling_calls=2.0)
    tool_runs = [0]
    _count_subagent_tool_runs(monkeypatch, tool_runs)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai(
                [
                    _call("read_file", {"path": "README.md"}, "r1"),
                    _call("read_file", {"path": "README.md"}, "r2"),
                ],
                usage=_usage(10, 5),
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "limit-exceeded"
    assert tool_runs[0] == 0
    fact = outcome.limit_fact
    assert fact is not None
    assert fact["scope"] == "attempt-ceiling"
    assert "tool-calls" in list(fact["dimensions"])
    assert fact["moment"] == "tool-call"
    assert budgets.open_hold_ids() == ()
    ended = [
        event for event in outcome.events if event["kind"] == "subagent_ended"
    ]
    assert len(ended) == 1


def test_two_members_share_the_last_tool_call_unit(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unit of room runs at most one member batch; overrun stays bounded."""
    step = _agent_step("process", limits={"tool-calls": 3.0})
    budgets = _budgets(step)
    tool_runs = [0]
    _count_subagent_tool_runs(monkeypatch, tool_runs)
    model = ScriptedModel(
        [
            _ai(
                [
                    _spawn_call("first", "read the workspace files", "spawn-1"),
                    _spawn_call("second", "read the workspace files", "spawn-2"),
                ],
                usage=_usage(10, 5),
            ),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage(10, 5),
            ),
            _ai([_report_subagent_call("first findings")], usage=_usage(10, 5)),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r2")],
                usage=_usage(10, 5),
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 5,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "limit-exceeded"
    assert tool_runs[0] == 1
    # Two spawns, one admitted batch and one crossing batch: the meter
    # exceeds the allowance by exactly the crossing batch.
    assert budgets.step_totals("process").tool_calls == 4
    assert budgets.step_totals("process").tool_calls - 3.0 <= 1
    fact = outcome.limit_fact
    assert fact is not None
    assert fact["scope"] == "step"
    assert fact["moment"] == "tool-call"
    assert budgets.open_hold_ids() == ()
    ended = [
        event for event in outcome.events if event["kind"] == "subagent_ended"
    ]
    assert [event["payload"]["call_id"] for event in ended] == [
        "spawn-1",
        "spawn-2",
    ]


def test_run_subagent_counts_unknown_calls_not_the_report() -> None:
    """Direct ``run_subagent`` bills unknown names; the exit stays free."""
    from coding_agent.runtime.graph.subagent import run_subagent

    charged: list[int] = []

    def _charge(count: int) -> None:
        charged.append(count)
        return None

    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "no_such_tool",
                        "args": {},
                        "id": "u1",
                        "type": "tool_call",
                    },
                    {
                        "name": REPORT_SUBAGENT_TOOL,
                        "args": {"report": "no findings"},
                        "id": "subrep-1",
                        "type": "tool_call",
                    },
                ],
            )
        ]
    )
    result = run_subagent(
        model,
        description="review the change",
        instructions="review the change",
        call_id="call-1",
        workspace=Path("/tmp"),
        charge_tool_calls=_charge,
    )
    assert result.status == "reported"
    assert charged == [1]
    assert result.billable_tool_calls == 1
    assert result.limit_fact is None


def test_run_subagent_crossing_batch_runs_nothing() -> None:
    """Direct ``run_subagent`` ends ``ceiling`` with no tool executed."""
    from coding_agent.runtime.graph.subagent import run_subagent
    from coding_agent.runtime.step_limits import LimitFact

    crossing = LimitFact(
        scope="step",
        dimensions=("tool-calls",),
        limit={"tool-calls": 2.0},
        spend={"tool-calls": 3.0},
        moment="tool-call",
    )

    def _charge(count: int) -> LimitFact | None:
        assert count == 2
        return crossing

    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "README.md"},
                        "id": "r1",
                        "type": "tool_call",
                    },
                    {
                        "name": "read_file",
                        "args": {"path": "README.md"},
                        "id": "r2",
                        "type": "tool_call",
                    },
                ],
            )
        ]
    )
    result = run_subagent(
        model,
        description="review the change",
        instructions="review the change",
        call_id="call-1",
        workspace=Path("/tmp"),
        charge_tool_calls=_charge,
    )
    assert result.status == "no-report"
    assert result.reason == "ceiling"
    assert result.billable_tool_calls == 2
    assert result.limit_fact == crossing
    # No ToolMessage followed the requesting response: nothing ran.
    from langchain_core.messages import ToolMessage as _ToolMessage

    assert not any(isinstance(message, _ToolMessage) for message in result.messages)
