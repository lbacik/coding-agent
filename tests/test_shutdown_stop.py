"""Live SIGTERM through the step runtime (§12, ADR 0024).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §12 (a SIGTERM
observed inside an ``agent`` step stops the run: no new model request starts,
a received response is metered then discarded, a running Sub-agent ends
``no-report`` / ``stopped`` even where a report is ready, queued members never
start, and no further Workflow Step runs) with live finalisation through
``finalise_shutdown`` (``interrupted`` / ``shutdown``, or ``withdrawn`` where
the input was withdrawn meanwhile).

Every test drives the production path — ``run_workflow`` with a scripted model
through ``ProductionStepExecutor``, then ``_finalise_live_attempt`` — never
``run_subagent`` or a private helper directly. The stop signal is a scripted
``is_stopped`` flag injected through ``AgentStepDeps`` and ``run_workflow``,
standing in for the process-wide shutdown flag.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.agent_step import REPORT_TOOL_NAME, StepAttemptFacts
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    SUBAGENT_QUEUED_KIND,
    SUBAGENT_STARTED_KIND,
)
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.runtime.step_limits import build_budgets
from coding_agent.provider.price_table import TokenPrices
from coding_agent.worker.attempt_run import _finalise_live_attempt
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps, make_attempt
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel

ATTEMPT = "implement#7/1"
NUMBER = 7
LABEL = "agent:test"


def _prices() -> TokenPrices:
    """One dollar a megatoken in, two out; reads a tenth, writes at par."""
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


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


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _main_report(result: str = "done") -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps({})},
        "report-1",
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
    return _call("report_subagent", {"report": report}, call_id)


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, Any]],
    *,
    tools: tuple[str, ...] = ("workspace-read",),
    routes: dict[str, RouteTarget] | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results=results),
        max_executions=1,
        routes=dict(routes or {"done": RouteTarget(end="completed")}),
    )


def _workflow(steps: dict[str, WorkflowStep], start: str) -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=LABEL),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start=start,
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps=steps,
    )


def _facts() -> StepAttemptFacts:
    return make_attempt()


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


def _issue() -> TrackedInput:
    return TrackedInput(
        number=NUMBER,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=True,
        labels=(LABEL,),
        assignees=("coding-agent",),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
        merged=False,
    )


def _open_attempt(ledger: RunLedger, workflow: Workflow) -> None:
    if not ledger.has_attempt(ATTEMPT):
        ledger.open_attempt(
            ATTEMPT,
            effective_configuration=full_effective_configuration(workflow),
        )


def _finalise(
    ledger: RunLedger,
    workflow: Workflow,
    workspace: Path,
    tracker: FakeStepTracker,
    result: Any,
    *,
    gate: Any = None,
    shutdown_grace_seconds: float | None = None,
) -> FakeStepTracker:
    """Finalise a ``run_workflow`` result the way the live Attempt run does."""
    _finalise_live_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        result=result,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels_for={"interrupted": (), "withdrawn": ()},
        workspace_path=workspace,
        workflow=workflow,
        attempt_branch="attempts/7",
        canonical_remote="file:///dev/null/remote.git",
        gate=gate if gate is not None else (lambda: GateResult(status="ok")),
        shutdown_grace_seconds=shutdown_grace_seconds,
    )
    return tracker


def _events(ledger: RunLedger, kind: str) -> list[dict[str, object]]:
    return [
        record.payload
        for record in ledger.records(ATTEMPT)
        if record.kind == kind
    ]


def test_shutdown_after_first_response_stops_step_and_finalises(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§12: the signal after the first response ends the run ``shutdown``.

    The first response is metered, then discarded: no second model request
    starts, the second Workflow Step never runs, and the live finalisation
    records Terminal Outcome ``interrupted`` / ``shutdown``.
    """
    first = _agent_step(
        "process",
        {"done": {}},
        routes={"done": RouteTarget(step="second")},
    )
    second = _agent_step("second", {"done": {}})
    workflow = _workflow({"process": first, "second": second}, "process")
    _open_attempt(ledger, workflow)
    model = ScriptedModel(
        [
            _ai(usage=_usage(10, 5)),
            _ai([_main_report()]),
            _ai([_main_report()]),
        ]
    )
    stopped = lambda: len(model.invocations) >= 1  # noqa: E731
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=build_budgets(
            workflow, prices=_prices(), output_cap=0, attempt_id=ATTEMPT
        ),
        prices=_prices(),
        is_stopped=stopped,
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id=ATTEMPT,
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=deps.budgets,
        is_stopped=stopped,
    )
    assert (result.outcome, result.reason) == ("interrupted", "shutdown")
    assert result.ended is False
    assert result.ending_step == "process"
    # One model request ran; nothing followed it: no second request and no
    # second Workflow Step.
    assert len(model.invocations) == 1
    executions = ledger.list_step_executions(ATTEMPT)
    assert [record.step_id for record in executions] == ["process"]
    assert executions[0].state == "interrupted"
    assert executions[0].result is None
    # The received response was metered exactly once before the discard.
    assert deps.budgets is not None
    assert deps.budgets.step_totals("process").tokens == 15

    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _finalise(ledger, workflow, workspace, tracker, result)

    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "shutdown")
    assert LABEL not in tracker.inputs[NUMBER].labels
    assert len(tracker.comments) == 1
    assert "Uncommitted files: 0" in tracker.comments[0].body


def test_shutdown_during_subagent_stops_member_and_skips_queued(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§12: the signal during a member ends it ``stopped``; queued never start.

    Two members spawn in one group; the signal fires during the first
    member's run. The running member ends ``no-report`` / ``stopped``, the
    queued member starts nothing (no queued/started/ended events), and the
    Attempt ends ``interrupted`` / ``shutdown``.
    """
    step = _agent_step(
        "process", {"done": {}}, tools=("workspace-read", "subagents")
    )
    workflow = _workflow({"process": step}, "process")
    _open_attempt(ledger, workflow)
    model = ScriptedModel(
        [
            _ai(
                [
                    _spawn_call("first", "read the workspace files", "s1"),
                    _spawn_call("second", "read the workspace files", "s2"),
                ],
                usage=_usage(10, 5),
            ),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage(500, 50),
            ),
            _ai([_main_report()]),
        ]
    )
    stopped = lambda: len(model.invocations) >= 2  # noqa: E731
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=build_budgets(
            workflow, prices=_prices(), output_cap=0, attempt_id=ATTEMPT
        ),
        prices=_prices(),
        is_stopped=stopped,
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id=ATTEMPT,
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=deps.budgets,
        is_stopped=stopped,
    )
    assert (result.outcome, result.reason) == ("interrupted", "shutdown")
    # The spawning turn and the member's first round ran; nothing else did.
    assert len(model.invocations) == 2
    ended = _events(ledger, SUBAGENT_ENDED_KIND)
    assert len(ended) == 1
    assert ended[0]["call_id"] == "s1"
    assert ended[0]["reported"] is False
    assert ended[0]["ending"] == "stopped"
    # The member's pre-stop response is metered exactly once: the ended
    # payload carries that single round's spend (one response, 550 tokens),
    # and the Attempt-wide meter holds the spawning turn plus that round,
    # nothing doubled.
    usage = cast(dict[str, object], ended[0]["usage"])
    assert usage["responses"] == 1
    assert usage["input_tokens"] == 500
    assert usage["output_tokens"] == 50
    assert deps.budgets is not None
    assert deps.budgets.step_totals("process").tokens == 15 + 550
    for kind in (SUBAGENT_QUEUED_KIND, SUBAGENT_STARTED_KIND, SUBAGENT_ENDED_KIND):
        assert all(
            payload.get("call_id") != "s2"
            for payload in _events(ledger, kind)
        ), kind

    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _finalise(ledger, workflow, workspace, tracker, result)

    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "shutdown")


def test_stop_preempts_ready_subagent_report(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§12: the stop takes precedence over a ready Sub-agent report.

    The member's first response already carries an accepted report, but the
    signal fires first: the run still ends ``stopped`` and the report is
    discarded — the main loop never even asks for its own report.
    """
    step = _agent_step(
        "process", {"done": {}}, tools=("workspace-read", "subagents")
    )
    workflow = _workflow({"process": step}, "process")
    _open_attempt(ledger, workflow)
    model = ScriptedModel(
        [
            _ai(
                [_spawn_call("look", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
            _ai(
                [_report_subagent_call("nothing to report")],
                usage=_usage(100, 10),
            ),
            _ai([_main_report()]),
        ]
    )
    stopped = lambda: len(model.invocations) >= 2  # noqa: E731
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=build_budgets(
            workflow, prices=_prices(), output_cap=0, attempt_id=ATTEMPT
        ),
        prices=_prices(),
        is_stopped=stopped,
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id=ATTEMPT,
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=deps.budgets,
        is_stopped=stopped,
    )
    assert (result.outcome, result.reason) == ("interrupted", "shutdown")
    assert len(model.invocations) == 2
    ended = _events(ledger, SUBAGENT_ENDED_KIND)
    assert len(ended) == 1
    assert ended[0]["reported"] is False
    assert ended[0]["ending"] == "stopped"


def test_recorded_effect_intent_still_settles_then_interrupted(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§12/§14.2: an Intent recorded before the signal still settles.

    The write already under way is never diverted: the finalisation settles
    the recorded post-comment intent, then ends ``interrupted`` / ``shutdown``.
    """
    step = _agent_step("process", {"done": {}})
    workflow = _workflow({"process": step}, "process")
    _open_attempt(ledger, workflow)
    ledger.record_step_effect_intent(
        ATTEMPT,
        "process",
        1,
        0,
        rendered_payload="post-comment implement#7/1\nsettle-me body",
        expected_target={
            "operation": "post-comment",
            "input_number": NUMBER,
            "input_type": "issue",
        },
    )
    model = ScriptedModel([_ai([_main_report()])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=build_budgets(
            workflow, prices=_prices(), output_cap=0, attempt_id=ATTEMPT
        ),
        prices=_prices(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id=ATTEMPT,
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=deps.budgets,
        is_stopped=lambda: True,
    )
    assert (result.outcome, result.reason) == ("interrupted", "shutdown")

    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _finalise(ledger, workflow, workspace, tracker, result)

    assert "post_comment" in tracker.call_names()
    assert any(
        "settle-me body" in comment.body for comment in tracker.comments
    )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "shutdown")


def test_withdrawn_input_at_shutdown_ends_withdrawn(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§12: where the input was withdrawn, the live end is ``withdrawn``."""
    step = _agent_step("process", {"done": {}})
    workflow = _workflow({"process": step}, "process")
    _open_attempt(ledger, workflow)
    model = ScriptedModel([_ai([_main_report()])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=build_budgets(
            workflow, prices=_prices(), output_cap=0, attempt_id=ATTEMPT
        ),
        prices=_prices(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id=ATTEMPT,
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=deps.budgets,
        is_stopped=lambda: True,
    )
    assert (result.outcome, result.reason) == ("interrupted", "shutdown")

    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _finalise(
        ledger,
        workflow,
        workspace,
        tracker,
        result,
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )

    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert stored.outcome == "withdrawn"


def test_expired_grace_leaves_attempt_unfinished(
    ledger: RunLedger, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12/§16.1: a grace already past at entry records nothing.

    The shutdown bound is production-wired (``shutdown_grace_seconds`` →
    ``shutdown_grace_deadline`` → ``finalise_shutdown``): with the deadline
    already past, the live finalisation returns the Attempt unfinished and
    the next start finishes it — no Terminal Outcome, no terminal comment.
    """
    import coding_agent.worker.attempt_run as attempt_run_module

    step = _agent_step("process", {"done": {}})
    workflow = _workflow({"process": step}, "process")
    _open_attempt(ledger, workflow)
    model = ScriptedModel([_ai([_main_report()])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=build_budgets(
            workflow, prices=_prices(), output_cap=0, attempt_id=ATTEMPT
        ),
        prices=_prices(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id=ATTEMPT,
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=deps.budgets,
        is_stopped=lambda: True,
    )
    assert (result.outcome, result.reason) == ("interrupted", "shutdown")
    monkeypatch.setattr(
        attempt_run_module, "shutdown_grace_deadline", lambda seconds: 0.0
    )

    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _finalise(
        ledger, workflow, workspace, tracker, result, shutdown_grace_seconds=30.0
    )

    assert tracker.comments == []
    assert ledger.get_attempt(ATTEMPT).state != "finished"


def test_run_subagent_takes_no_legacy_callbacks() -> None:
    """The four request-lifecycle callbacks are gone from ``run_subagent``."""
    from coding_agent.runtime.graph.subagent import run_subagent

    parameters = inspect.signature(run_subagent).parameters
    for name in (
        "should_stop",
        "on_usage",
        "on_missing_usage",
        "on_unreported_cache_write",
    ):
        assert name not in parameters
    assert "is_stopped" in parameters
