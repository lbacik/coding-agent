"""Every Sub-agent model request passes Admission under its own hold (#528).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §11.4
(Admission runs before every model request — provider retries and Sub-agent
requests included) and §9.8 (each Sub-agent request is admitted
individually), plus ADR 0029 (each try passes Admission and counts against
the Attempt Ceiling).

Each Sub-agent request — every round and every provider retry — runs through
the shared model-request lifecycle, the same one the step loop uses: the
spawn-time hold becomes the member's first request's hold (admitted up front
in call order, so LIM-7 still holds), every later round and every retry is
admitted again under its own hold, and each hold settles with its own
response's actual spend (or its worst case). A refused request ends only
that Sub-agent (``no-report`` / ``ceiling``), never the calling Step
Execution.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import ModelError, ModelRateLimitError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.graph.agent_model_request import run_model_request
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    SUBAGENT_QUEUED_KIND,
    SubagentRecord,
    build_subagent_tools,
    estimate_spawn_admission,
)
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    LimitFact,
    build_budgets,
    worst_case_bound,
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
    """A generous Attempt Ceiling: the step allowance under test binds first."""
    return AttemptCeiling(
        cost_usd=100000.0,
        effective_tokens=100000.0,
        tool_calls=10000.0,
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
    result: str,
    payload: dict[str, object],
    usage: dict[str, object] | None,
    call_id: str = "report-1",
) -> AIMessage:
    import json as _json

    return AIMessage(
        content="reporting",
        tool_calls=[
            _call(
                REPORT_TOOL_NAME,
                {"result": result, "payload_json": _json.dumps(payload)},
                call_id,
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


def _run(
    step: WorkflowStep,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
) -> Any:
    import sqlite3 as _sqlite3

    workflow = _workflow(step)
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
    return run_agent_step(workflow.steps[step.id], 1, deps, {})


def _subagent_tools(workspace: Path) -> list[Any]:
    """The exact bound set the spawn group estimates admission with."""
    from coding_agent.runtime.result_capping import InMemoryArtifactStore

    return list(
        build_subagent_tools(
            workspace,
            SubagentRecord(description="probe"),
            artifact_store=InMemoryArtifactStore(),
            base_revision="abc123",
            change_artifact_id="read-change-probe",
        )
    )


def _bound_for(instructions: str, workspace: Path) -> int:
    """The spawn admission's worst-case token bound for ``instructions``."""
    tools = _subagent_tools(workspace)
    estimate = estimate_spawn_admission(instructions, tools)
    bound_tokens, _ = worst_case_bound(estimate, 0, _prices())
    return bound_tokens


def _main_bound(workspace: Path, tmp_path: Path) -> int:
    """The spawning turn's own worst-case token bound, computed not guessed."""
    from coding_agent.runtime.conversation_context import estimate_context_tokens
    from coding_agent.runtime.graph.agent_step import (
        build_agent_step_tools,
        compose_step_opening,
    )

    step = _agent_step("process")
    budgets = build_budgets(
        _workflow(step), prices=_prices(), output_cap=0, attempt_id=ATTEMPT_ID
    )
    probe_model = ScriptedModel(
        [_ai(usage=_usage(10, 5))], response_models=["test-model"]
    )
    deps = make_agent_step_deps(
        model=probe_model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
    )
    tools = list(build_agent_step_tools(step, deps))
    opening = list(compose_step_opening(step, deps, {}))
    bound_tokens, _ = worst_case_bound(
        estimate_context_tokens(opening, [], tools), 0, _prices()
    )
    return bound_tokens


def _events(ledger: RunLedger, kind: str) -> list[dict[str, object]]:
    return [
        record.payload
        for record in ledger.records(ATTEMPT_ID)
        if record.kind == kind
    ]


class _RaiseOnCallModel(ScriptedModel):
    """A ``ScriptedModel`` raising once on the ``fail_at``-th invoke.

    The failed call consumes no queued outcome, so the queue stays aligned:
    the retry (or the next conversation) pops what it would have popped
    without the failure.
    """

    def __init__(
        self,
        outcomes: list[AIMessage],
        *,
        fail_at: int,
        error: ModelError,
        response_models: list[str | None] | None = None,
    ) -> None:
        super().__init__(outcomes, response_models=response_models)
        self._fail_at = fail_at
        self._error = error
        self.calls = 0

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        _ = kwargs
        self.calls += 1
        if self.calls == self._fail_at:
            raise self._error
        return super().invoke(input, **kwargs)


def test_two_round_subagent_records_two_holds_settled_at_actual(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Sub-agent rounds record two holds; each settles at its actual (§11.4)."""
    step = _agent_step("process")
    budgets = _budgets(step)
    hold_ids = _capture_holds(budgets, monkeypatch)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage(500, 50),
            ),
            _ai([_report_subagent_call("nothing to report")], usage=_usage(100, 10)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 4,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    # The spawning turn, the spawn-time hold (the member's first request),
    # the second round, and the final report: four holds, none counted twice.
    assert len(hold_ids) == 4
    assert len(set(hold_ids)) == 4
    assert budgets.open_hold_ids() == ()
    for hold_id in hold_ids:
        stored = ledger.get_admission_hold(hold_id)
        assert stored is not None
        assert stored.status == "settled"
    # Every response settled at its actual: the Sub-agent's 550 + 110 count.
    assert budgets.step_totals("process").tokens == 15 + 550 + 110 + 15


def test_retryable_error_then_success_charges_first_hold(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable failure charges its hold; the retry is admitted anew (§11.4)."""
    step = _agent_step("process")
    budgets = _budgets(step)
    hold_ids = _capture_holds(budgets, monkeypatch)
    report = _ai([_report_subagent_call("nothing to report")], usage=_usage(100, 10))
    model = _RaiseOnCallModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            report,
            _report_ai("done", {}, _usage(10, 5)),
        ],
        fail_at=2,
        error=ModelRateLimitError("slow down"),
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    assert model.calls == 4
    # The spawning turn, the failed first try, its retry, and the report.
    assert len(hold_ids) == 4
    subagent_first = hold_ids[1]
    subagent_retry = hold_ids[2]
    assert ledger.get_admission_hold(subagent_first) is not None
    assert ledger.get_admission_hold(subagent_first).status == "charged"  # type: ignore[union-attr]
    assert ledger.get_admission_hold(subagent_retry) is not None
    assert ledger.get_admission_hold(subagent_retry).status == "settled"  # type: ignore[union-attr]
    assert budgets.open_hold_ids() == ()


def test_round_two_refused_when_headroom_fits_only_round_one(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 refused ends only the Sub-agent; its request is never sent."""
    instructions = "read the workspace files"
    # The window below is computed, not guessed: the limit clears the
    # spawning turn's own bound, then covers the spawn hold and the first
    # round's actual spend — leaving less than even the first round's bound
    # for round 2, so the bigger second round cannot fit.
    probe = _bound_for(instructions, workspace)
    main_actual = 15
    first_actual = 550
    limit = float(_main_bound(workspace, tmp_path) + 50)
    assert probe <= limit - main_actual
    assert limit - main_actual - first_actual < probe
    step = _agent_step("process", limits={"effective-tokens": limit})
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", instructions, "s1")], usage=_usage(10, 5)),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage(500, 50),
            ),
            _ai([_report_subagent_call("nothing to report")], usage=_usage(100, 10)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 4,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    # The second round was refused: the scripted report was never consumed,
    # so the model saw the spawn and one Sub-agent round only. The refusal
    # ends only the Sub-agent — the step goes on and meets the limit at its
    # own next Admission.
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["scope"] == "step"
    assert len(model.invocations) == 2
    ended = _events(ledger, SUBAGENT_ENDED_KIND)
    assert len(ended) == 1
    assert ended[0]["call_id"] == "s1"
    assert ended[0]["reported"] is False
    assert budgets.step_totals("process").tokens == main_actual + first_actual
    assert budgets.open_hold_ids() == ()


def test_retry_refused_when_headroom_fits_only_the_first_try(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry that would exceed the headroom is never sent (ceiling, not error)."""
    instructions = "read the workspace files"
    probe = _bound_for(instructions, workspace)
    # The failed first try is charged at its worst case, so the retry's
    # bound — about the same size — no longer fits the remaining headroom.
    limit = float(_main_bound(workspace, tmp_path) + 50)
    assert probe <= limit - 15
    assert limit - 15 - probe < probe
    step = _agent_step("process", limits={"effective-tokens": limit})
    budgets = _budgets(step)
    model = _RaiseOnCallModel(
        [
            _ai([_spawn_call("look", instructions, "s1")], usage=_usage(10, 5)),
            _ai([_report_subagent_call("nothing to report")], usage=_usage(100, 10)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        fail_at=2,
        error=ModelRateLimitError("slow down"),
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    # The retry is never sent — the failed first try already consumed the
    # headroom at its worst case — and the step meets the limit at its own
    # Admission. The anchored main conversation is smaller than its old
    # whole-request estimate, so the main loop sends one more turn (the
    # queued report item lands as an unknown tool) before its own Admission
    # refuses: three invokes in all — the spawning turn, the failed first
    # try, and the final main turn — with no retry between the last two.
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["scope"] == "step"
    assert model.calls == 3
    ended = _events(ledger, SUBAGENT_ENDED_KIND)
    assert len(ended) == 1
    assert ended[0]["call_id"] == "s1"
    assert ended[0]["reported"] is False
    assert budgets.open_hold_ids() == ()


def test_missing_usage_charged_and_subagent_continues(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Sub-agent response without usage settles worst case; the run continues."""
    step = _agent_step("process")
    budgets = _budgets(step)
    hold_ids = _capture_holds(budgets, monkeypatch)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai([_call("read_file", {"path": "README.md"}, "r1")], usage=None),
            _ai([_report_subagent_call("nothing to report")], usage=_usage(100, 10)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 4,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    ended = _events(ledger, SUBAGENT_ENDED_KIND)
    assert len(ended) == 1
    assert ended[0]["reported"] is True
    # The usage-less round settled as charged; the report settled actual.
    assert ledger.get_admission_hold(hold_ids[1]) is not None
    assert ledger.get_admission_hold(hold_ids[1]).status == "charged"  # type: ignore[union-attr]
    assert ledger.get_admission_hold(hold_ids[2]) is not None
    assert ledger.get_admission_hold(hold_ids[2]).status == "settled"  # type: ignore[union-attr]
    assert ledger.get_responses_without_usage(ATTEMPT_ID) == 1
    assert budgets.open_hold_ids() == ()


def test_spawn_group_second_member_refused_in_call_order(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two members that fit alone but not together: first runs, second refused."""
    instructions = "read the workspace files"
    probe = _bound_for(instructions, workspace)
    # One member fits beside the main turn; two members' holds do not.
    limit = float(15 + probe + probe // 2)
    step = _agent_step("process", limits={"effective-tokens": limit})
    budgets = _budgets(step)
    hold_ids = _capture_holds(budgets, monkeypatch)
    model = ScriptedModel(
        [
            _ai(
                [
                    _spawn_call("first", instructions, "spawn-1"),
                    _spawn_call("second", instructions, "spawn-2"),
                ],
                usage=_usage(10, 5),
            ),
            _ai([_report_subagent_call("first findings")], usage=_usage(100, 10)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    # The second member never ran: the spawn and one Sub-agent round only,
    # in call order. The first member reported; the anchored main
    # conversation is smaller than its old whole-request estimate, so the
    # final report now fits beside the spent holds and the step reports.
    assert outcome.status == "reported"
    assert len(model.invocations) == 3
    queued = _events(ledger, SUBAGENT_QUEUED_KIND)
    refused = [payload for payload in queued if payload.get("call_id") == "spawn-2"]
    assert len(refused) == 1
    ended = _events(ledger, SUBAGENT_ENDED_KIND)
    assert {payload.get("call_id") for payload in ended} == {"spawn-1", "spawn-2"}
    by_call = {payload.get("call_id"): payload for payload in ended}
    first_ended = dict(by_call["spawn-1"])
    refused_ended = dict(by_call["spawn-2"])
    assert first_ended["reported"] is True
    assert first_ended["ending"] == "reported"
    assert refused_ended["reported"] is False
    assert refused_ended["ending"] == "refused"
    assert refused_ended["duration_seconds"] == 0.0
    refused_usage = refused_ended["usage"]
    assert isinstance(refused_usage, dict)
    assert refused_usage["responses"] == 0
    assert "conversation_context_peak" not in refused_ended
    # The refused member held nothing: the spawning turn, the one admitted
    # member, and the final report were admitted — never the refused member.
    assert len(hold_ids) == 3
    assert budgets.open_hold_ids() == ()


def test_shared_lifecycle_settles_subagent_spend_without_main_notes(
    tmp_path: Path,
) -> None:
    """The shared lifecycle settles a Sub-agent hold without main-conversation notes."""
    step = WorkflowStep(
        id="process",
        agent=Agent(tools=(), results={"done": {"note": "string"}}),
        max_executions=1,
        routes={"done": RouteTarget(end="completed")},
    )
    budgets = build_budgets(
        _workflow(step), prices=_prices(), output_cap=0, attempt_id=ATTEMPT_ID
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    response = _ai(usage=_usage(100, 10))
    model = ScriptedModel([response], response_models=["test-model"])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
    )
    from langchain_core.messages import HumanMessage

    from coding_agent.provider.pin_witness import PinWitness
    from coding_agent.runtime.graph.agent_step import ExecutionTally

    tally = ExecutionTally(
        deps=deps, step=step, seq=1, baseline=budgets.step_totals(step.id)
    )
    witness = PinWitness(
        record_event=tally.record_event,
        attempt_id=deps.attempt.attempt_id,
        pin=deps.pin,
        step_id=step.id,
        seq=1,
    )
    messages: list[BaseMessage] = [HumanMessage(content="hello")]
    tools = _subagent_tools(workspace)
    estimate = estimate_spawn_admission("hello", tools)
    fact, first_hold = budgets.admit_request(step.id, estimate)
    assert fact is None and first_hold is not None

    result = run_model_request(
        deps,
        witness,
        step.id,
        1,
        model.bind_tools([]),
        messages,
        [],
        first_hold_id=first_hold,
        record_main_context=False,
    )

    assert isinstance(result, AIMessage)
    assert budgets.step_totals(step.id).tokens == 110
    assert budgets.open_hold_ids() == ()
    # Main-conversation figures stay untouched: no used size, no cache note.
    snapshot = budgets.main_context_snapshot()
    assert snapshot["used"] is None
    assert snapshot["cache_read"] == 0
    assert snapshot["cache_input"] == 0
