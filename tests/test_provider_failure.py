"""A provider failure after its retries ends the run, never the Worker (#570).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.6 (a
non-retryable or exhausted ``ModelError`` ends the Step Execution ``error``
with the provider error class and status named in the detail and the
Execution Summary), §9.8 (a Sub-agent ends ``no-report`` / ``provider-failure``,
a context overflow ``no-report`` / ``context-window``) and §4.4 (a routed
provider-failure ``error`` counts toward the halt-after-three as an
infrastructure-class failure of class ``provider``; a Context Overflow never
counts).

A non-retryable ``ModelError``, or a retryable one whose three tries are
exhausted, used to escape the Worker and kill the process while the Attempt
was live; recovery then settled it as ``interrupted`` / ``process-loss``.
These tests drive every path through the scripted model and the real
``run_worker_loop`` and show the Attempt and the Worker go on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import (
    ContextOverflowError,
    ModelError,
    ModelInvalidRequestError,
    ModelRateLimitError,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.inputs.claim import ConfirmedClaim
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    SPAWN_TOOL_NAME,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.subagent import SUBAGENT_ENDED_KIND
from coding_agent.runtime.graph.workflow_graph import WorkflowResult, run_workflow
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    LimitFact,
    build_budgets,
)
from coding_agent.worker.run import (
    WorkerLoopConfig,
    attempt_had_provider_failure,
    is_pin_violation_reason,
    run_worker_loop,
)
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


def _prices() -> TokenPrices:
    """One dollar a megatoken in, two out; reads a tenth, writes at par."""
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


def _ceiling() -> AttemptCeiling:
    """A generous Attempt Ceiling: the provider failure binds first."""
    return AttemptCeiling(
        cost_usd=100000.0,
        effective_tokens=100000.0,
        tool_calls=10000.0,
        wall_clock_minutes=600.0,
    )


def _agent_step(step_id: str = "process") -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(
            tools=("workspace-read", "subagents"),
            results={"done": {}},
        ),
        max_executions=1,
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )


def _workflow(step: WorkflowStep) -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=_ceiling(),
        start=step.id,
        steps={step.id: step},
    )


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


def _budgets(step: WorkflowStep) -> AttemptBudgets:
    return build_budgets(
        _workflow(step), prices=_prices(), output_cap=0, attempt_id=ATTEMPT_ID
    )


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _ai(
    calls: list[dict[str, object]] | None = None,
    *,
    content: str = "",
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content=content, tool_calls=list(calls or []), usage_metadata=usage
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _report_ai(
    result: str, payload: dict[str, object], usage: dict[str, object]
) -> AIMessage:
    import json as _json

    return AIMessage(
        content="reporting",
        tool_calls=[
            _call(
                REPORT_TOOL_NAME,
                {"result": result, "payload_json": _json.dumps(payload)},
                "report-1",
            )
        ],
        usage_metadata=usage,
    )


def _spawn_call(
    description: str, instructions: str, call_id: str
) -> dict[str, object]:
    return _call(
        SPAWN_TOOL_NAME,
        {"description": description, "instructions": instructions},
        call_id,
    )


def _report_subagent_call(report: str, call_id: str = "sub-1") -> dict[str, object]:
    return _call("report_subagent", {"report": report}, call_id)


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


class _FailAlwaysModel:
    """A model failing every invoke: the exhausted-retry seam (§9.6).

    ``ScriptedModel`` cannot raise, so this minimal fake raises the same
    error on every turn. ``bind_tools`` returns ``self`` the way the retry
    seam in ``test_agent_model_request`` does.
    """

    def __init__(self, error: ModelError) -> None:
        self._error = error
        self.calls = 0

    def bind_tools(
        self, tools: Any, tool_choice: Any = None
    ) -> _FailAlwaysModel:
        _ = (tools, tool_choice)
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        _ = (input, kwargs)
        self.calls += 1
        raise self._error


class _ScriptedOrRaiseModel:
    """Queued answers with failures spliced in at chosen turns (§9.8).

    Each script entry is either the next ``AIMessage`` or the ``Exception``
    the ``invoke`` raises instead. Responses are stamped with the pinned
    identity the way ``ScriptedModel(response_models=...)`` stamps them.
    """

    def __init__(self, script: list[AIMessage | Exception]) -> None:
        self._script = list(script)
        self.calls = 0
        self.invocations: list[LanguageModelInput] = []

    def bind_tools(
        self, tools: Any, tool_choice: Any = None
    ) -> _ScriptedOrRaiseModel:
        _ = (tools, tool_choice)
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        from langchain_core.messages import AIMessage as _AIMessage

        _ = kwargs
        self.invocations.append(input)
        self.calls += 1
        action = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(action, Exception):
            raise action
        assert isinstance(action, _AIMessage)
        metadata = dict(action.response_metadata or {})
        metadata.setdefault("model", "test-model")
        return action.model_copy(update={"response_metadata": metadata})


def _run(
    step: WorkflowStep,
    model: Any,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
) -> Any:
    workflow = _workflow(step)
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
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
    return run_agent_step(workflow.steps[step.id], 1, deps, {})


def _capture_holds(
    budgets: AttemptBudgets, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Every Admission hold id, in admission order (main and Sub-agent)."""
    hold_ids: list[str] = []
    real_admit = budgets.admit_request

    def _admit(
        step_id: str, estimate_tokens: int, **kwargs: object
    ) -> tuple[LimitFact | None, str | None]:
        fact, hold_id = real_admit(step_id, estimate_tokens, **kwargs)  # type: ignore[arg-type]
        if hold_id is not None:
            hold_ids.append(hold_id)
        return fact, hold_id

    monkeypatch.setattr(budgets, "admit_request", _admit)
    return hold_ids


def _assert_charged(ledger: RunLedger, hold_ids: list[str]) -> None:
    """Every captured hold settled charged at its worst case (§11.4)."""
    assert hold_ids != []
    for hold_id in hold_ids:
        stored = ledger.get_admission_hold(hold_id)
        assert stored is not None
        assert stored.status == "charged"


def _subagent_ended(ledger: RunLedger) -> list[dict[str, object]]:
    """Every ``subagent_ended`` payload, in emission order."""
    return [
        record.payload
        for record in ledger.records(ATTEMPT_ID)
        if record.kind == SUBAGENT_ENDED_KIND
    ]


# --- §9.6: the main conversation --------------------------------------------


def test_main_non_retryable_error_ends_error(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-retryable failure ends the Step Execution ``error`` (#570)."""
    step = _agent_step()
    budgets = _budgets(step)
    hold_ids = _capture_holds(budgets, monkeypatch)
    model = _FailAlwaysModel(ModelInvalidRequestError("bad request"))

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    # The Worker survives: the step ends instead of raising.
    assert outcome.status == "error"
    assert "ModelInvalidRequestError" in outcome.detail
    assert outcome.summary.get("provider_failure") == {
        "error": "ModelInvalidRequestError",
        "status": 400,
        "context_overflow": False,
    }
    # One call, worst-case charge (§11.4): the hold settles charged.
    assert model.calls == 1
    assert budgets.open_hold_ids() == ()
    assert hold_ids != []
    _assert_charged(ledger, hold_ids)
    assert budgets.step_totals("process").tokens > 0


def test_main_retryable_error_exhausted_ends_error(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retryable failure ends ``error`` once its three tries are spent."""
    step = _agent_step()
    budgets = _budgets(step)
    hold_ids = _capture_holds(budgets, monkeypatch)
    model = _FailAlwaysModel(ModelRateLimitError("slow down"))

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "error"
    assert "ModelRateLimitError" in outcome.detail
    assert outcome.summary.get("provider_failure") == {
        "error": "ModelRateLimitError",
        "status": 429,
        "context_overflow": False,
    }
    # All three tries ran; each hold settled charged at its worst case.
    assert model.calls == 3
    assert budgets.open_hold_ids() == ()
    assert len(hold_ids) == 3
    _assert_charged(ledger, hold_ids)


def test_main_provider_error_is_routed_by_the_workflow(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The Workflow routes the provider-failed ``error`` like any other."""
    workflow = _workflow(_agent_step())
    attempt = StepAttemptFacts(
        attempt_id="implement#9/1",
        workflow_id="implement",
        input_type="issue",
        input_number=9,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )
    model = _FailAlwaysModel(ModelInvalidRequestError("bad request"))
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=attempt,
        ledger=ledger,
        prices=_prices(),
        pin=make_pin(),
    )

    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#9/1",
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
    )

    # The Attempt is routed to the ``failed`` End and goes on: nothing raises.
    # (The Terminal Outcome itself is recorded later by finalisation, the
    # way ``run_one_attempt`` does — ``run_workflow`` only routes.)
    assert result.outcome == "failed"
    executions = ledger.list_step_executions("implement#9/1")
    assert len(executions) == 1
    assert executions[0].result == "error"
    # The durable marker survives a restart: reopening the ledger from the
    # same file still reports the provider failure (§4.4).
    assert attempt_had_provider_failure(ledger, "implement#9/1") is True
    reopened = RunLedger(ledger.path)
    try:
        assert attempt_had_provider_failure(reopened, "implement#9/1") is True
    finally:
        reopened.close()


def test_main_context_overflow_ends_error_but_never_counts(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A Context Overflow ends ``error`` yet never counts toward the halt."""
    step = _agent_step()
    budgets = _budgets(step)
    model = _FailAlwaysModel(ContextOverflowError("context too large"))

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "error"
    assert "ContextOverflowError" in outcome.detail
    assert outcome.summary.get("provider_failure") == {
        "error": "ContextOverflowError",
        "status": None,
        "context_overflow": True,
    }
    assert model.calls == 1
    assert budgets.open_hold_ids() == ()
    # #563 decision 1: a Context Overflow never halts the Worker.
    assert attempt_had_provider_failure(ledger, ATTEMPT_ID) is False
    reopened = RunLedger(ledger.path)
    try:
        assert attempt_had_provider_failure(reopened, ATTEMPT_ID) is False
    finally:
        reopened.close()


# --- §9.8: Sub-agents --------------------------------------------------------


def test_subagent_provider_failure_ends_no_report(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A failing Sub-agent ends ``no-report`` / ``provider-failure`` (#570)."""
    step = _agent_step()
    budgets = _budgets(step)
    model = _ScriptedOrRaiseModel(
        [
            _ai(
                [_spawn_call("look", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
            ModelInvalidRequestError("bad request"),
            _report_ai("done", {}, _usage(10, 5)),
        ]
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    # The caller gets the Sub-agent result as its tool result and reports.
    assert outcome.status == "reported"
    ended = _subagent_ended(ledger)
    assert len(ended) == 1
    assert ended[0]["reported"] is False
    provider_error = ended[0].get("provider_error")
    assert isinstance(provider_error, str)
    assert "ModelInvalidRequestError" in provider_error
    assert budgets.open_hold_ids() == ()


def test_subagent_context_overflow_ends_context_window(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A Sub-agent context overflow ends ``no-report`` / ``context-window``."""
    step = _agent_step()
    budgets = _budgets(step)
    model = _ScriptedOrRaiseModel(
        [
            _ai(
                [_spawn_call("look", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
            ContextOverflowError("context too large"),
            _report_ai("done", {}, _usage(10, 5)),
        ]
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    ended = _subagent_ended(ledger)
    assert len(ended) == 1
    assert ended[0]["reported"] is False
    assert "provider_error" not in ended[0]
    assert budgets.open_hold_ids() == ()


# --- §12: the pin keeps its own path ----------------------------------------


def test_pin_violation_still_takes_the_pin_path(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A pin-violating response ends ``interrupted`` / ``pin-violation``."""
    workflow = _workflow(_agent_step())
    attempt = StepAttemptFacts(
        attempt_id="implement#8/1",
        workflow_id="implement",
        input_type="issue",
        input_number=8,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )
    model = ScriptedModel(
        [_ai([_call(REPORT_TOOL_NAME, {"result": "done", "payload_json": "{}"}, "r1")])],
        response_models=["model-other"],
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=attempt,
        ledger=ledger,
        prices=_prices(),
        pin=make_pin(),
    )

    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#8/1",
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
    )

    assert (result.outcome, result.reason) == ("interrupted", "pin-violation")
    assert is_pin_violation_reason(result.reason) is True


# --- §4.4: halt-after-three ---------------------------------------------------


def _provider_failure_runner(
    ledger: RunLedger,
    fail_numbers: set[int],
    ran: list[int],
) -> Callable[[ConfirmedClaim], WorkflowResult]:
    """A stub Attempt run: failed Attempts carry the provider marker.

    Attempts in ``fail_numbers`` end with a routed provider-failure
    ``error`` — a recorded Step Execution whose facts carry the
    ``provider_failure`` marker, and a non-infrastructure Terminal
    Outcome. The rest verify cleanly.
    """

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        if claimed.input_number in fail_numbers:
            ledger.open_step_execution(claimed.attempt_id, "work", 1)
            ledger.end_step_execution(
                claimed.attempt_id,
                "work",
                1,
                result="error",
                facts={
                    "provider_failure": {
                        "error": "ModelRateLimitError",
                        "status": 429,
                        "context_overflow": False,
                    }
                },
            )
            outcome, reason = "failed", "step-error"
        else:
            outcome, reason = "delivered", "verified-completion"
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome=outcome, reason=reason
        )
        return WorkflowResult(
            attempt_id=claimed.attempt_id, outcome=outcome, reason=reason
        )

    return run_one


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> Any:
    from support.fake_step_tracker import FakeStepTracker
    from coding_agent.inputs.port import LabeledEvent, TrackedInput

    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = TrackedInput(
            number=number,
            input_type="issue",
            title=f"Add a greeting {number}",
            body="body",
            is_open=True,
            labels=("agent:implement",),
            assignees=(),
            created_at=created_at,
            author_login="human",
            author_id=7,
        )
        tracker.labeled_events[(number, "agent:implement")] = LabeledEvent(
            label="agent:implement",
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at=created_at,
        )
    return tracker


def _loop_config(ledger: RunLedger) -> WorkerLoopConfig:
    from coding_agent.inputs.claim import ClaimConfig
    from support.effective_config import full_effective_configuration

    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:implement"),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="work",
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": RouteTarget(end="delivered")},
            ),
        },
    )
    return WorkerLoopConfig(
        claim_config=ClaimConfig(
            workflow_id="implement",
            input_type="issue",
            selection_label="agent:implement",
            base_branch="main",
            agent_login="coding-agent",
            agent_id=999,
            effective_configuration=full_effective_configuration(workflow),
        ),
        workflow=workflow,
        poll_interval_seconds=60.0,
    )


def _release_on_finish(
    tracker: Any, runner: Callable[[ConfirmedClaim], WorkflowResult]
) -> Callable[[ConfirmedClaim], WorkflowResult]:
    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        result = runner(claimed)
        tracker.remove_label(claimed.input_number, "issue", "agent:implement")
        tracker.unassign_input(claimed.input_number, "issue")
        return result

    return run_one


def test_three_routed_provider_failures_halt_after_the_third_is_finalized(
    ledger: RunLedger,
) -> None:
    """Three routed provider failures halt with the infrastructure halt."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
        (34, "2026-10-04T00:00:00Z"),
    )
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(ledger),
        run_one_attempt=_release_on_finish(
            tracker, _provider_failure_runner(ledger, {31, 32, 33}, ran)
        ),
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    assert ran == [31, 32, 33]
    assert ledger.get_attempt("implement#33/1").state == "finished"
    assert not ledger.has_unfinished_attempt()
    connection = sqlite3.connect(ledger.path)
    try:
        row = connection.execute(
            "SELECT reason, detail FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert str(row[0]) == "infrastructure-failure"


def test_a_clean_attempt_between_provider_failures_resets_the_count(
    ledger: RunLedger,
) -> None:
    """An Attempt without such a failure resets the halt counter."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
        (34, "2026-10-04T00:00:00Z"),
    )
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(ledger),
        run_one_attempt=_release_on_finish(
            tracker, _provider_failure_runner(ledger, {31, 32, 34}, ran)
        ),
        sleep=lambda seconds: None,
        max_attempts=4,
    )

    assert exit_code == 0
    assert ran == [31, 32, 33, 34]


def test_three_context_overflow_errors_never_halt(
    ledger: RunLedger,
) -> None:
    """Three Context Overflow ``error`` ends still leave the Worker running."""

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ledger.open_step_execution(claimed.attempt_id, "work", 1)
        ledger.end_step_execution(
            claimed.attempt_id,
            "work",
            1,
            result="error",
            facts={
                "provider_failure": {
                    "error": "ContextOverflowError",
                    "status": None,
                    "context_overflow": True,
                }
            },
        )
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="failed", reason="step-error"
        )
        return WorkflowResult(
            attempt_id=claimed.attempt_id, outcome="failed", reason="step-error"
        )

    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
    )
    ran: list[int] = []

    def recording_runner(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        return run_one(claimed)

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(ledger),
        run_one_attempt=_release_on_finish(tracker, recording_runner),
        sleep=lambda seconds: None,
        max_attempts=3,
    )

    assert exit_code == 0
    assert ran == [31, 32, 33]
