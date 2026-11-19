"""One Context-Window Handoff in an `agent` Step Execution, driven directly.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (the
Context-Window Handoff) and §11 (meters and the ``limit-exceeded`` fact).

Part of #487 (slice 4 of 4). A pure unit suite: every path of
``perform_handoff`` is driven here directly, with the existing
``AgentStepDeps`` test builder, without running ``run_agent_step``. The pin
verdict functions are called by the loop, never copied here — the tests only
assert the handoff outcomes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from coding_agent.runtime.context_handoff import (
    MAX_HANDOFFS_PER_STEP,
    SNAPSHOT_SIZE_LIMIT_BYTES,
    ContinuationSnapshot,
)
from coding_agent.runtime.graph.agent_handoff import perform_handoff
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    AgentStepOutcome,
    ExecutionTally,
)
from coding_agent.runtime.transcript import work_conversation
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps, make_budgets, make_prices
from support.scripted_model import ScriptedModel


def _step() -> WorkflowStep:
    """One `agent` step: the handoff path never reports, so one result suffices."""
    return WorkflowStep(
        id="process",
        agent=Agent(
            instructions=(),
            tools=(),
            inputs=(),
            results={"done": {"note": "string"}},
        ),
        max_executions=1,
        limits={},
        reserved=False,
        routes={"done": RouteTarget(end="completed")},
    )


def _snapshot() -> ContinuationSnapshot:
    """One small valid Continuation Snapshot under the size cap."""
    return ContinuationSnapshot(
        version=1,
        attempt_id="implement#7/1",
        base_revision="abc123",
        branch="agent/implement/7/1-x",
        commit="abc123",
        changed_files=("x.py",),
        commands=({"command": "true", "result": "ok"},),
        validation_evidence=None,
        unresolved_questions=(),
        next_intended_action="continue",
    )


def _harness(
    tmp_path: Path,
    *,
    tokens: float = 100000.0,
) -> tuple[AgentStepDeps, ExecutionTally, WorkflowStep, list[BaseMessage], list[BaseTool]]:
    """Deps, tally, step, messages and tools the way the loop builds them."""
    from support.agent_step_deps import make_workflow

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    step = _step()
    workflow: Workflow
    if tokens != 100000.0:
        workflow = Workflow(
            id="implement",
            model="sonnet",
            input=InputDecl(type="issue", label="agent:test"),
            ceiling=AttemptCeiling(
                cost_usd=100000.0,
                tokens=tokens,
                tool_calls=10000.0,
                wall_clock_minutes=600.0,
            ),
            start="process",
        )
    else:
        workflow = make_workflow()
    budgets = make_budgets(
        workflow,
        attempt_id="implement#7/1",
        prices=make_prices(),
        output_cap=0,
    )
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        budgets=budgets,
        handoff_producer=lambda messages: _snapshot(),
    )
    tally = ExecutionTally(
        deps=deps,
        step=step,
        seq=1,
        baseline=deps.budgets.step_totals(step.id),
    )
    messages: list[BaseMessage] = [HumanMessage(content="prior work")]
    tools: list[BaseTool] = []
    return deps, tally, step, messages, tools


def _fresh_opening() -> list[BaseMessage]:
    """The loop's fresh Step opening, without step inputs (owned by the loop)."""
    return [HumanMessage(content="fresh opening")]


def test_success_stores_snapshot_and_forks_continuation(tmp_path: Path) -> None:
    """A handoff stores the snapshot, counts it and forks the conversation."""
    deps, tally, step, messages, tools = _harness(tmp_path)
    snapshot = _snapshot()

    result = perform_handoff(deps, tally, step, 1, messages, tools, _fresh_opening)

    assert not isinstance(result, AgentStepOutcome)
    new_messages, bound_model = result
    assert tally.handoffs == 1
    assert tally.snapshot_digest == snapshot.digest
    stored = deps.artifact_store.read(f"continuation-snapshots/{snapshot.digest}.json")
    assert stored == snapshot.to_json()
    assert len(new_messages) == 2
    assert str(new_messages[0].content) == "fresh opening"
    assert "Continuation Snapshot" in str(new_messages[1].content)
    assert snapshot.to_json() in str(new_messages[1].content)
    conversation = getattr(bound_model, "conversation", None)
    assert conversation == work_conversation(step.id, 1, 1)


def test_handoff_count_limit_ends_limit_exceeded(tmp_path: Path) -> None:
    """A step at its handoff bound ends ``limit-exceeded`` without producing."""
    deps, tally, step, messages, tools = _harness(tmp_path)
    tally.handoffs = MAX_HANDOFFS_PER_STEP
    called: list[int] = []

    def _producer(previous: list[BaseMessage]) -> ContinuationSnapshot:
        called.append(len(previous))
        return _snapshot()

    deps.handoff_producer = _producer

    result = perform_handoff(deps, tally, step, 1, messages, tools, _fresh_opening)

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "limit-exceeded"
    assert result.detail == "exceeded the code-owned handoff count"
    assert result.limit_fact == {"scope": "step", "reason": "handoff-count"}
    assert result.handoffs == MAX_HANDOFFS_PER_STEP
    assert called == []


def test_missing_producer_ends_handoff_failed(tmp_path: Path) -> None:
    """No producer ends ``error`` through the handoff-failed shape."""
    deps, tally, step, messages, tools = _harness(tmp_path)
    deps.handoff_producer = None

    result = perform_handoff(deps, tally, step, 1, messages, tools, _fresh_opening)

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "error"
    assert result.detail.startswith("failed Context-Window Handoff:")
    assert "no handoff producer" in result.detail
    assert result.handoffs == 0
    assert tally.handoffs == 0
    assert tally.snapshot_digest is None


def test_oversized_snapshot_ends_handoff_failed(tmp_path: Path) -> None:
    """An over-limit snapshot ends ``error`` and stores nothing."""
    deps, tally, step, messages, tools = _harness(tmp_path)
    big = replace(_snapshot(), next_intended_action="x" * (SNAPSHOT_SIZE_LIMIT_BYTES + 1))

    def _producer(previous: list[BaseMessage]) -> ContinuationSnapshot:
        _ = previous
        return big

    deps.handoff_producer = _producer

    result = perform_handoff(deps, tally, step, 1, messages, tools, _fresh_opening)

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "error"
    assert "exceeds" in result.detail
    assert tally.handoffs == 0
    assert tally.snapshot_digest is None


def test_handoff_spend_crossing_limit_ends_limit_exceeded(tmp_path: Path) -> None:
    """Snapshot spend crossing a limit ends ``limit-exceeded`` after counting."""
    deps, tally, step, messages, tools = _harness(tmp_path, tokens=1.0)

    result = perform_handoff(deps, tally, step, 1, messages, tools, _fresh_opening)

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "limit-exceeded"
    assert "the handoff spend crossed the" in result.detail
    assert result.limit_fact is not None
    assert tally.handoffs == 1
    assert tally.snapshot_digest == _snapshot().digest
