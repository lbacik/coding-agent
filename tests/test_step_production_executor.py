"""The production Step executor: agent mapping and operation delegation.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (the
``agent`` step) and §10 (operations) through the graph's one Step executor
port (§8.5). The production adapter picks between the agent loop and the
operation runner by step kind and owns the mapping from the agent loop's
``reported`` / ``limit-exceeded`` / ``error`` status to a Step Result.
``GateFailed`` and ``PinViolation`` propagate to the graph, which ends the
Attempt exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.graph.agent_step import (
    MAX_REPORT_CORRECTIONS,
    REPORT_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
)
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.workflow_graph import WorkflowDefect
from coding_agent.runtime.step_limits import AttemptBudgets, build_budgets
from coding_agent.runtime.step_operations import (
    GateFailed,
    OperationExecution,
    OperationRunner,
)
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    FieldType,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps
from support.scripted_model import ScriptedModel


def _prices() -> TokenPrices:
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, Any] = {
        "attempt_id": "w#1/1",
        "workflow_id": "w",
        "input_type": "issue",
        "input_number": 1,
        "title": "Fix the crash",
        "body": "The worker crashes on startup.",
        "base_revision": "abc123",
        "current_revision": "abc123",
    }
    base.update(overrides)
    return StepAttemptFacts(
        attempt_id=str(base["attempt_id"]),
        workflow_id=str(base["workflow_id"]),
        input_type=str(base["input_type"]),
        input_number=int(base["input_number"]),
        title=str(base["title"]),
        body=str(base["body"]),
        base_revision=str(base["base_revision"]),
        current_revision=str(base["current_revision"]),
    )


def _agent_step(step_id: str, results: dict[str, dict[str, FieldType]]) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=("workspace-read",), results=results),
        routes={
            "done": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )


def _op_step(step_id: str, operation: str) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        operation=operation,
        routes={
            "pushed": RouteTarget(end="completed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
        },
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {"cache_read": 0, "cache_creation": 0},
    }


def _report_ai(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> AIMessage:
    return AIMessage(
        content="reporting",
        tool_calls=[
            _call(
                REPORT_TOOL_NAME,
                {"result": result, "payload_json": json.dumps(payload)},
                call_id,
            )
        ],
        usage_metadata=_usage(10, 5),
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
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


class _FakeRunner(OperationRunner):
    def __init__(
        self,
        results: dict[str, str],
        gate_status: str | None = None,
    ) -> None:
        self.results = dict(results)
        self.gate_status = gate_status
        self.calls: list[str] = []

    def run(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> OperationExecution:
        self.calls.append(step.id)
        if self.gate_status is not None and step.operation == "commit-and-push":
            raise GateFailed(self.gate_status, "content-changed")
        result = self.results[step.id]
        return OperationExecution(
            result=result,
            payload={"result": result},
            facts={"scripted": True},
        )


def test_reported_maps_to_result_with_payload_and_facts(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("process", {"done": {"summary": "string"}})
    model = ScriptedModel([_report_ai("done", {"summary": "ok"})])
    deps = _deps(_facts(), model, workspace, tmp_path)
    executor = ProductionStepExecutor(agent_deps=deps)
    outcome = executor.execute(step, 1, "w#1/1", ledger)
    assert outcome.result == "done"
    assert outcome.payload == {"summary": "ok"}
    assert outcome.limit_fact is None
    assert outcome.usage != {}


def test_limit_exceeded_maps_to_limit_exceeded(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    workflow = Workflow(
        id="w",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0, effective_tokens=100000.0, tool_calls=10000.0, wall_clock_minutes=600.0
        ),
        start="process",
        steps={
            "process": WorkflowStep(
                id="process",
                agent=Agent(tools=("workspace-read",), results={"done": {}}),
                limits={"effective-tokens": 100.0},
                routes={
                    "done": RouteTarget(end="completed"),
                    "limit-exceeded": RouteTarget(end="unfinished"),
                    "error": RouteTarget(end="failed"),
                },
            )
        },
    )
    budgets: AttemptBudgets = build_budgets(
        workflow, prices=_prices(), output_cap=64, attempt_id="w#1/1"
    )
    budgets.add_spend("process", UsageTotals(tokens=50, effective_tokens=50))
    budgets.add_spend("process", UsageTotals(tokens=40, effective_tokens=40))
    model = ScriptedModel([_report_ai("done", {})])
    deps = _deps(_facts(), model, workspace, tmp_path, budgets=budgets)
    executor = ProductionStepExecutor(agent_deps=deps)
    outcome = executor.execute(workflow.steps["process"], 3, "w#1/1", ledger)
    assert outcome.result == "limit-exceeded"
    assert outcome.payload is None
    assert outcome.limit_fact is not None
    assert model.invocations == []


def test_error_maps_to_error(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("s", {"done": {}})
    bad = [
        AIMessage(
            content="bad",
            tool_calls=[
                _call(
                    REPORT_TOOL_NAME,
                    {"result": "bogus", "payload_json": "{}"},
                    f"r-{i}",
                )
            ],
        )
        for i in range(MAX_REPORT_CORRECTIONS + 1)
    ]
    model = ScriptedModel(bad)
    deps = _deps(_facts(), model, workspace, tmp_path)
    executor = ProductionStepExecutor(agent_deps=deps)
    outcome = executor.execute(step, 1, "w#1/1", ledger)
    assert outcome.result == "error"
    assert outcome.payload is None
    assert outcome.limit_fact is None


def test_operation_delegates_to_runner(ledger: RunLedger) -> None:
    step = _op_step("push", "commit-and-push")
    runner = _FakeRunner({"push": "pushed"})
    executor = ProductionStepExecutor(operation_runner=runner)
    outcome = executor.execute(step, 1, "w#1/1", ledger)
    assert outcome.result == "pushed"
    assert outcome.payload == {"result": "pushed"}
    assert outcome.facts == {"scripted": True}
    assert outcome.usage == {}
    assert outcome.limit_fact is None
    assert runner.calls == ["push"]


def test_missing_agent_deps_raises_defect(ledger: RunLedger) -> None:
    step = _agent_step("s", {"done": {}})
    executor = ProductionStepExecutor(
        operation_runner=_FakeRunner({"s": "done"})
    )
    with pytest.raises(WorkflowDefect):
        executor.execute(step, 1, "w#1/1", ledger)


def test_missing_operation_runner_raises_defect(
    ledger: RunLedger, tmp_path: Path
) -> None:
    step = _op_step("push", "commit-and-push")
    model = ScriptedModel([_report_ai("done", {})])
    deps = make_agent_step_deps(
        model=model,
        workspace=tmp_path,
        tmp_path=tmp_path,
        attempt=_facts(),
    )
    executor = ProductionStepExecutor(agent_deps=deps)
    with pytest.raises(WorkflowDefect):
        executor.execute(step, 1, "w#1/1", ledger)


def test_gate_failed_passes_through(ledger: RunLedger) -> None:
    step = _op_step("push", "commit-and-push")
    runner = _FakeRunner({"push": "pushed"}, gate_status="drift")
    executor = ProductionStepExecutor(operation_runner=runner)
    with pytest.raises(GateFailed):
        executor.execute(step, 1, "w#1/1", ledger)
