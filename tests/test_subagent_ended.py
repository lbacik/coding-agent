"""``subagent_ended`` carries the Sub-agent's own cell, peak, ending and duration.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.8 (every
ending path carries the Sub-agent's own usage cell, its peak Conversation
Context, its ending and ``duration_seconds``) and §13.2 (the ``subagent_ended``
fields; archived records without them read ``unavailable``, never zero).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import OriginCell, origin_cell_from_readable
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    SPAWN_TOOL_NAME,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    recorded_subagent_peak,
    recorded_subagent_usage,
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
    """A generous Attempt Ceiling: the scripted path under test binds first."""
    return AttemptCeiling(
        cost_usd=100000.0,
        effective_tokens=100000.0,
        tool_calls=10000.0,
        wall_clock_minutes=600.0,
    )


def _agent_step(
    step_id: str = "process",
    *,
    limits: dict[str, float] | None = None,
    tools: tuple[str, ...] = ("workspace-read", "subagents"),
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(
            tools=tools,
            results={"done": {}},
        ),
        max_executions=1,
        limits=dict(limits or {}),
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
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content="", tool_calls=list(calls or []), usage_metadata=usage
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _spawn_call(
    description: str, instructions: str, call_id: str
) -> dict[str, object]:
    return _call(
        SPAWN_TOOL_NAME,
        {"description": description, "instructions": instructions},
        call_id,
    )


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


def _run(
    step: WorkflowStep,
    model: Any,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
    *,
    context_window: ContextWindowConfig | None = None,
    pin: Any = None,
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
        pin=pin if pin is not None else make_pin(),
        context_window=context_window,
    )
    return run_agent_step(workflow.steps[step.id], 1, deps, {})


def _ended(outcome: Any, call_id: str = "s1") -> dict[str, object]:
    """The one ``subagent_ended`` payload for ``call_id``."""
    ended = [
        event
        for event in outcome.events
        if event["kind"] == SUBAGENT_ENDED_KIND
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("call_id") == call_id
    ]
    assert len(ended) == 1
    payload = ended[0]["payload"]
    assert isinstance(payload, dict)
    return dict(payload)


def _assert_common_fields(payload: dict[str, object], ending: str) -> None:
    """Every ending carries the ending, a duration and a usage cell."""
    assert payload["ending"] == ending
    duration = payload["duration_seconds"]
    assert isinstance(duration, (int, float))
    assert float(duration) >= 0.0
    usage = payload["usage"]
    assert isinstance(usage, dict)


def _assert_peak_present(payload: dict[str, object]) -> dict[str, object]:
    """The peak travels with its value and basis."""
    peak = payload.get("conversation_context_peak")
    assert isinstance(peak, dict)
    value = peak.get("value")
    basis = peak.get("basis")
    assert isinstance(value, int) and value > 0
    assert basis in ("anchored", "estimated")
    return peak


class _ScriptedOrRaiseModel:
    """Queued answers with failures spliced in at chosen turns."""

    def __init__(self, script: list[AIMessage | Exception]) -> None:
        self._script = list(script)

    def bind_tools(
        self, tools: Any, tool_choice: Any = None
    ) -> _ScriptedOrRaiseModel:
        _ = (tools, tool_choice)
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        from langchain_core.messages import AIMessage as _AIMessage

        _ = kwargs
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        assert isinstance(action, _AIMessage)
        metadata = dict(action.response_metadata or {})
        metadata.setdefault("model", "test-model")
        return action.model_copy(update={"response_metadata": metadata})


def test_reported_carries_cell_peak_ending_and_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A reported Sub-agent's ending carries its cell, peak, ending and duration."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai(
                [_spawn_call("look", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
            _ai([_report_subagent_call("findings")], usage=_usage(100, 10)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    payload = _ended(outcome)
    assert payload["reported"] is True
    _assert_common_fields(payload, "reported")
    peak = _assert_peak_present(payload)
    assert peak["basis"] == "estimated"
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.responses == 1
    assert cell.input_tokens == 100
    assert cell.output_tokens == 10


def test_silent_carries_cell_peak_ending_and_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A silent Sub-agent's ending carries its cell, peak, ending and duration."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai(
                [_spawn_call("look", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
            _ai(None, usage=_usage(40, 6)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    payload = _ended(outcome)
    assert payload["reported"] is False
    _assert_common_fields(payload, "silent")
    _assert_peak_present(payload)
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.responses == 1
    assert cell.input_tokens == 40


def test_round_limit_carries_cell_peak_ending_and_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Eight tool rounds without a report end ``round-limit`` with the fields."""
    step = _agent_step()
    budgets = _budgets(step)
    script: list[AIMessage] = [
        _ai(
            [_spawn_call("look", "read the workspace files", "s1")],
            usage=_usage(10, 5),
        )
    ]
    for index in range(8):
        script.append(
            _ai(
                [_call("read_file", {"path": "README.md"}, f"r{index}")],
                usage=_usage(20 + index, 4),
            )
        )
    script.append(_report_ai("done", {}, _usage(10, 5)))
    model = ScriptedModel(script, response_models=["test-model"] * len(script))

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    payload = _ended(outcome)
    assert payload["reported"] is False
    _assert_common_fields(payload, "round-limit")
    peak = _assert_peak_present(payload)
    assert peak["basis"] == "estimated"
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.responses == 8


def test_context_window_carries_cell_peak_ending_and_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A context-window Sub-agent carries the peak beside the crossing value."""
    (workspace / "big.txt").write_text("0123456789abcdef\n" * 1250, encoding="utf-8")
    from coding_agent.runtime.conversation_context import estimate_context_tokens
    from coding_agent.runtime.graph.agent_step import (
        build_agent_step_tools,
        compose_step_opening,
    )

    probe_step = _agent_step("s", tools=("workspace-read", "subagents"))
    probe = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
    )
    opening = compose_step_opening(probe_step, probe, {})
    estimate = estimate_context_tokens(
        list(opening), [], list(build_agent_step_tools(probe_step, probe))
    )
    usable = estimate + 400
    window = ContextWindowConfig(
        threshold_tokens=usable + 2_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
    )
    assert window.usable_threshold == usable
    step = _agent_step("s", tools=("workspace-read", "subagents"))
    budgets = build_budgets(
        Workflow(
            id="implement",
            model="sonnet",
            input=InputDecl(type="issue", label="agent:test"),
            ceiling=_ceiling(),
            start="s",
            steps={"s": step},
        ),
        prices=_prices(),
        output_cap=0,
        attempt_id=ATTEMPT_ID,
    )
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        SPAWN_TOOL_NAME,
                        {
                            "description": "check diff",
                            "instructions": "read big.txt twice, fully",
                        },
                        "s1",
                    )
                ],
            ),
            _ai(
                [
                    _call("read_file", {"path": "big.txt"}, "r1"),
                    _call("read_file", {"path": "big.txt"}, "r2"),
                ]
            ),
            _ai(
                [_call(REPORT_TOOL_NAME, {"result": "done", "payload_json": "{}"}, "r1")]
            ),
        ]
    )
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=_ceiling(),
        start="s",
        steps={"s": step},
    )
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
        context_window=window,
    )
    outcome = run_agent_step(workflow.steps["s"], 1, deps, {})

    assert outcome.status == "reported"
    payload = _ended(outcome)
    assert payload["reported"] is False
    _assert_common_fields(payload, "context-window")
    peak = _assert_peak_present(payload)
    assert peak["basis"] == "estimated"
    assert "conversation_context" in payload
    assert payload["usable_threshold"] == usable
    assert payload.get("context_cause") == "guard"
    assert "overflow_signal" not in payload


def test_context_window_before_first_request_is_all_zero_and_unavailable(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A member stopped before its first request spends nothing, peak unknown."""
    from coding_agent.runtime.graph.subagent import (
        SUBAGENT_QUEUED_KIND,
        SUBAGENT_STARTED_KIND,
        SubagentRecord,
        build_subagent_tools,
        estimate_spawn_admission,
    )
    from coding_agent.runtime.graph.subagent_member import SubagentMember
    from coding_agent.runtime.result_capping import InMemoryArtifactStore

    step = _agent_step("s", tools=("workspace-read", "subagents"))
    # A fixed usable threshold below the member's opening estimate: the guard
    # ends the member before its first request. The group is driven directly
    # — no main conversation — so only the member's own measure matters.
    window = ContextWindowConfig(
        threshold_tokens=3_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
    )
    assert window.usable_threshold == 1_000
    instructions = "read the workspace files. " + ("Pay close attention. " * 200)
    member_tools = list(
        build_subagent_tools(
            workspace,
            SubagentRecord(description="look"),
            artifact_store=InMemoryArtifactStore(),
            base_revision="abc123",
            change_artifact_id="read-change-s1",
        )
    )
    assert estimate_spawn_admission(instructions, member_tools) > 1_000
    budgets = build_budgets(
        Workflow(
            id="implement",
            model="sonnet",
            input=InputDecl(type="issue", label="agent:test"),
            ceiling=_ceiling(),
            start="s",
            steps={"s": step},
        ),
        prices=_prices(),
        output_cap=0,
        attempt_id=ATTEMPT_ID,
    )
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass
    deps = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
        context_window=window,
    )
    events: list[dict[str, object]] = []

    def record_event(kind: str, detail: str, payload: dict[str, object]) -> None:
        events.append({"kind": kind, "detail": detail, "payload": payload})

    member = SubagentMember.admit(
        deps, step, 1, "s1", "look", instructions, record_event
    )
    assert member.early_outcome is None
    outcome = member.run(deps, step, record_event)

    assert outcome.limit_fact is None
    assert outcome.billable_tool_calls == 0
    assert "context-window" in outcome.text
    assert outcome.report is None
    assert outcome.ok is False
    kinds = [event["kind"] for event in events]
    assert kinds == [SUBAGENT_QUEUED_KIND, SUBAGENT_STARTED_KIND, SUBAGENT_ENDED_KIND]
    payload = events[-1]["payload"]
    assert isinstance(payload, dict)
    assert payload["reported"] is False
    _assert_common_fields(payload, "context-window")
    assert "conversation_context_peak" not in payload
    cell = origin_cell_from_readable(payload["usage"])
    assert cell == OriginCell()
    assert recorded_subagent_peak(payload).basis == "unavailable"
    assert recorded_subagent_peak(payload).value is None
    assert budgets.open_hold_ids() == ()


def test_tool_call_ceiling_carries_cell_peak_ending_and_duration(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crossing tool-call batch ends ``ceiling`` carrying the fields."""
    step = _agent_step("process", limits={"tool-calls": 2.0})
    budgets = _budgets(step)
    real_note = budgets.note_tool_calls

    def _note(step_id: str, count: int, **kwargs: object) -> LimitFact | None:
        return real_note(step_id, count, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(budgets, "note_tool_calls", _note)
    model = ScriptedModel(
        [
            _ai(
                [_spawn_call("look", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
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
    payload = _ended(outcome)
    assert payload["reported"] is False
    _assert_common_fields(payload, "ceiling")
    _assert_peak_present(payload)
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.tool_calls == 2


def test_refused_carries_zero_cell_no_peak_and_zero_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A refused member never runs: zero cell, no peak, zero duration."""
    from coding_agent.runtime.graph.subagent import (
        SubagentRecord,
        build_subagent_tools,
        estimate_spawn_admission,
    )
    from coding_agent.runtime.result_capping import InMemoryArtifactStore

    instructions = "read the workspace files"
    tools = list(
        build_subagent_tools(
            workspace,
            SubagentRecord(description="probe"),
            artifact_store=InMemoryArtifactStore(),
            base_revision="abc123",
            change_artifact_id="read-change-probe",
        )
    )
    estimate = estimate_spawn_admission(instructions, tools)
    bound_tokens, _ = worst_case_bound(estimate, 0, _prices())
    limit = float(15 + bound_tokens + bound_tokens // 2)
    step = _agent_step("process", limits={"effective-tokens": limit})
    budgets = _budgets(step)
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

    assert outcome.status == "reported"
    payload = _ended(outcome, call_id="spawn-2")
    assert payload["reported"] is False
    assert payload["ending"] == "refused"
    assert payload["duration_seconds"] == 0.0
    assert "conversation_context_peak" not in payload
    cell = origin_cell_from_readable(payload["usage"])
    assert cell == OriginCell()
    assert recorded_subagent_peak(payload).basis == "unavailable"


def test_pin_violation_carries_cell_peak_ending_and_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A pin-violating member's ending carries cell, peak, ending and duration."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai(
                [_spawn_call("look", "read files", "s1")],
                usage=_usage(10, 5),
            ),
            _ai(
                [_report_subagent_call("findings")],
                usage=_usage(100, 10),
            ),
        ],
        response_models=["test-model", "model-other"],
    )
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
        pin=make_pin("test-model"),
    )

    with pytest.raises(PinViolation):
        run_agent_step(workflow.steps[step.id], 1, deps, {})

    stored = [
        record for record in ledger.records(ATTEMPT_ID) if record.kind == SUBAGENT_ENDED_KIND
    ]
    assert len(stored) == 1
    payload = dict(stored[0].payload)
    assert payload["reported"] is False
    _assert_common_fields(payload, "pin-violation")
    _assert_peak_present(payload)
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.responses >= 1


def test_provider_failure_carries_cell_peak_ending_and_duration(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A failing member ends ``provider-failure`` carrying the fields."""
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

    assert outcome.status == "reported"
    payload = _ended(outcome)
    assert payload["reported"] is False
    _assert_common_fields(payload, "provider-failure")
    assert isinstance(payload.get("provider_error"), str)
    assert "ModelInvalidRequestError" in str(payload["provider_error"])
    peak = _assert_peak_present(payload)
    assert peak["basis"] == "estimated"


def test_ended_cells_sum_to_the_step_subagent_cell_including_worst_case(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The ended cells sum to the Step Execution's ``subagent`` cell."""
    step = _agent_step()
    budgets = _budgets(step)
    baseline = budgets.step_by_origin(step.id)["subagent"]
    assert baseline == OriginCell()
    model = _ScriptedOrRaiseModel(
        [
            _ai(
                [
                    _spawn_call("first", "read the workspace files", "spawn-1"),
                    _spawn_call("second", "read the workspace files", "spawn-2"),
                ],
                usage=_usage(10, 5),
            ),
            _ai([_report_subagent_call("first findings")], usage=_usage(100, 10)),
            ModelInvalidRequestError("bad request"),
            _report_ai("done", {}, _usage(10, 5)),
        ]
    )

    outcome = _run(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    first = _ended(outcome, call_id="spawn-1")
    second = _ended(outcome, call_id="spawn-2")
    assert first["ending"] == "reported"
    assert second["ending"] == "provider-failure"
    total = origin_cell_from_readable(first["usage"]) + origin_cell_from_readable(
        second["usage"]
    )
    step_cell = budgets.step_by_origin(step.id)["subagent"]
    assert total.input_tokens == step_cell.input_tokens
    assert total.output_tokens == step_cell.output_tokens
    assert total.cache_read_tokens == step_cell.cache_read_tokens
    assert total.cache_write_tokens == step_cell.cache_write_tokens
    assert total.effective_tokens == step_cell.effective_tokens
    assert total.tool_calls == step_cell.tool_calls
    assert total.responses == step_cell.responses
    assert total.responses_without_usage == step_cell.responses_without_usage
    assert total.total_cost_usd == pytest.approx(step_cell.total_cost_usd)
    assert total.cost_input_usd == pytest.approx(step_cell.cost_input_usd)
    assert total.cost_output_usd == pytest.approx(step_cell.cost_output_usd)
    assert total.cost_cache_read_usd == pytest.approx(step_cell.cost_cache_read_usd)
    assert total.cost_cache_write_usd == pytest.approx(step_cell.cost_cache_write_usd)
    # The failed member's hold charged its worst case, not zero.
    second_usage = second["usage"]
    assert isinstance(second_usage, dict)
    assert second_usage["responses_without_usage"] == 1


def test_archived_ended_without_fields_reads_unavailable_never_zero(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """An old ``subagent_ended`` without the fields reads unavailable, never 0."""
    payload: dict[str, object] = {
        "attempt_id": ATTEMPT_ID,
        "step_id": "process",
        "seq": 1,
        "call_id": "s1",
        "reported": True,
        "answering_model": "test-model",
        "response_models": ["test-model"],
    }
    assert recorded_subagent_usage(payload) is None
    peak = recorded_subagent_peak(payload)
    assert peak.value is None
    assert peak.basis == "unavailable"
