"""One spawn-group member owns admission, run, stop/ceiling and ending.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8
(Sub-agents: one per ``spawn_subagent`` call), §9.8 (queued/started/ended
ledger events), §11 (meters, admission with holds) and §12 (the stop takes
precedence over a ready report; a queued member never starts once the
signal is observed); ADR 0020 (consecutive calls form one concurrent
group — members tolerate concurrency, ordering is by slot) and ADR 0024
(a stopped Sub-agent discards its in-flight request at the next stop
point, metered spend stays metered).

The spawn group only orders members: each member admits in call order
through :meth:`SubagentMember.admit` and runs to its ending through the
same instance's :meth:`SubagentMember.run` — no positional tuple crosses
the boundary, and the ``subagent_ended`` payload is derived from the rich
result by the pure :func:`subagent_ended_payload`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import OriginCell, origin_cell_from_readable
from coding_agent.runtime.conversation_context import ConversationContext
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    SPAWN_TOOL_NAME,
    ShutdownRequested,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    SubagentResult,
)
from coding_agent.runtime.graph.subagent_member import (
    SubagentMember,
    pin_violation_subagent_ended_payload,
    refused_subagent_ended_payload,
    subagent_ended_payload,
)
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
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
    return build_budgets(_workflow(step), prices=_prices(), output_cap=0, attempt_id=ATTEMPT_ID)


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
    return AIMessage(content="", tool_calls=list(calls or []), usage_metadata=usage)


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _spawn_call(description: str, instructions: str, call_id: str) -> dict[str, object]:
    return _call(
        SPAWN_TOOL_NAME,
        {"description": description, "instructions": instructions},
        call_id,
    )


def _report_ai(result: str, payload: dict[str, object], usage: dict[str, object]) -> AIMessage:
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


def _open_attempt(ledger: RunLedger) -> None:
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass


def _member_deps(
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
    *,
    is_stopped: Callable[[], bool] | None = None,
) -> Any:
    """Deps driving one member directly, never the main conversation."""
    _open_attempt(ledger)
    return make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
        is_stopped=is_stopped,
    )


def _events() -> tuple[list[dict[str, object]], Callable[[str, str, dict[str, object]], None]]:
    """An in-memory event sink with its recorder."""
    seen: list[dict[str, object]] = []

    def record_event(kind: str, detail: str, payload: dict[str, object]) -> None:
        seen.append({"kind": kind, "detail": detail, "payload": payload})

    return seen, record_event


def _scripted_request(
    script: list[AIMessage],
    *,
    observed: str | None = "test-model",
) -> Callable[[object, list[BaseMessage], list[BaseTool]], BaseMessage]:
    """An injected request model popping queued responses, appended live."""
    queued = list(script)

    def _request(
        bound_model: object,
        request_messages: list[BaseMessage],
        request_tools: list[BaseTool],
    ) -> BaseMessage:
        _ = (bound_model, request_tools)
        response = queued.pop(0)
        if observed is not None:
            # The provider reports the answering identity on the response
            # metadata, like ScriptedModel stamps its queued responses.
            metadata = dict(response.response_metadata or {})
            metadata["model"] = observed
            response = response.model_copy(update={"response_metadata": metadata})
        # The seam contract: the accepted response joins the messages before
        # returning, exactly like the shared lifecycle's own send.
        request_messages.append(response)
        return response

    return _request


def _run_member(
    step: WorkflowStep,
    deps: Any,
    record_event: Callable[[str, str, dict[str, object]], None],
    script: list[AIMessage],
) -> Any:
    """Admit one member and run it to its ending through the single entry."""
    member = SubagentMember.admit(deps, step, 1, "s1", "look", "read files", record_event)
    assert member.early_outcome is None
    return member.run(deps, step, record_event, request_model=_scripted_request(script))


def _payload_of(event: dict[str, object]) -> dict[str, object]:
    """The event's payload, narrowed from ``object`` for the assertions."""
    payload = event.get("payload")
    assert isinstance(payload, dict)
    return dict(payload)


def _ended(events: list[dict[str, object]], call_id: str = "s1") -> dict[str, object]:
    """The one ``subagent_ended`` payload for ``call_id``."""
    ended: list[dict[str, object]] = []
    for event in events:
        if event["kind"] != SUBAGENT_ENDED_KIND:
            continue
        payload = _payload_of(event)
        if payload.get("call_id") != call_id:
            continue
        ended.append(payload)
    assert len(ended) == 1
    return ended[0]


def _run_step(
    step: WorkflowStep,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
    *,
    is_stopped: Callable[[], bool] | None = None,
) -> Any:
    _open_attempt(ledger)
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
        is_stopped=is_stopped,
    )
    return run_agent_step(_workflow(step).steps[step.id], 1, deps, {})


# --- Member single entry ------------------------------------------------------


def test_member_reported_run_through_single_entry(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Admit + run returns the rich outcome and emits queued/started/ended."""
    step = _agent_step()
    budgets = _budgets(step)
    deps = _member_deps(ScriptedModel([]), workspace, tmp_path, ledger, budgets)
    events, record_event = _events()

    outcome = _run_member(
        step,
        deps,
        record_event,
        [_ai([_report_subagent_call("findings")], usage=_usage(100, 10))],
    )

    assert outcome.ok is True
    assert outcome.report == "findings"
    assert outcome.billable_tool_calls == 0
    assert outcome.limit_fact is None
    assert "findings" in outcome.text
    assert [event["kind"] for event in events] == [
        "subagent_queued",
        "subagent_started",
        "subagent_ended",
    ]
    payload = _ended(events)
    assert payload["reported"] is True
    assert payload["ending"] == "reported"
    assert payload["answering_model"] == "test-model"
    assert payload["response_models"] == ["test-model"]
    for event in events:
        payload = _payload_of(event)
        assert payload["attempt_id"] == ATTEMPT_ID
        assert payload["step_id"] == "process"
        assert payload["call_id"] == "s1"
    # The member settles its own holds: nothing stays open.
    assert budgets.open_hold_ids() == ()
    assert ledger.list_open_admission_holds(ATTEMPT_ID) == ()


def test_member_toolset_built_once(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission builds the toolset once; the run reuses it, never rebuilds."""
    from coding_agent.runtime.graph.subagent import (
        build_subagent_tools as real_build,
    )

    builds = [0]

    def _counting(*args: Any, **kwargs: Any) -> Any:
        builds[0] += 1
        return real_build(*args, **kwargs)

    # The member owns the single toolset build: patch where it looks it up.
    monkeypatch.setattr(
        "coding_agent.runtime.graph.subagent_member.build_subagent_tools",
        _counting,
    )
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai([_report_subagent_call("nothing to report")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )

    outcome = _run_step(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    assert builds[0] == 1


def test_member_stopped_takes_precedence_over_ready_report(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The stop observed before accepting ends the member stopped, no report."""
    step = _agent_step()
    budgets = _budgets(step)
    # Loop top, after response, after tool result pass; before-accept stops.
    flags = [False, False, False, True]

    def _is_stopped() -> bool:
        return flags.pop(0) if flags else True

    deps = _member_deps(
        ScriptedModel([]), workspace, tmp_path, ledger, budgets, is_stopped=_is_stopped
    )
    events, record_event = _events()

    outcome = _run_member(
        step,
        deps,
        record_event,
        [_ai([_report_subagent_call("findings")], usage=_usage(100, 10))],
    )

    assert outcome.ok is False
    assert outcome.report is None
    assert outcome.billable_tool_calls == 0
    assert outcome.limit_fact is None
    assert "stopped" in outcome.text
    payload = _ended(events)
    assert payload["reported"] is False
    assert payload["ending"] == "stopped"
    # The ready report is discarded, but the received response's identity
    # was already reported: the answering model still travels.
    assert payload["answering_model"] == "test-model"
    assert payload["response_models"] == ["test-model"]
    assert budgets.open_hold_ids() == ()


def test_member_discarded_in_flight_response_stays_metered(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A response already received is metered, then discarded at the stop."""

    class _StopAfterFirst(ScriptedModel):
        """Flip the stop once the first response has been delivered."""

        def __init__(self, outcomes: list[AIMessage]) -> None:
            super().__init__(outcomes, response_models=["test-model"] * len(outcomes))
            self.stopped = False

        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            response = super().invoke(input, **kwargs)
            self.stopped = True
            return response

    step = _agent_step()
    budgets = _budgets(step)
    model = _StopAfterFirst(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage(100, 10),
            ),
            _ai([_report_subagent_call("late findings")], usage=_usage(100, 10)),
        ]
    )
    deps = _member_deps(
        model, workspace, tmp_path, ledger, budgets, is_stopped=lambda: model.stopped
    )
    events, record_event = _events()

    member = SubagentMember.admit(deps, step, 1, "s1", "look", "read files", record_event)
    assert member.early_outcome is None
    outcome = member.run(deps, step, record_event)

    assert outcome.ok is False
    assert outcome.report is None
    assert "stopped" in outcome.text
    # Only the first request ran: the ready-later report never sends.
    assert len(model.invocations) == 1
    payload = _ended(events)
    assert payload["reported"] is False
    assert payload["ending"] == "stopped"
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.responses == 1
    assert cell.input_tokens == 100
    assert budgets.open_hold_ids() == ()


def test_member_tool_call_ceiling(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A crossing tool-call batch ends the member ``no-report`` / ``ceiling``."""
    step = _agent_step(limits={"tool-calls": 1.0})
    budgets = _budgets(step)
    deps = _member_deps(ScriptedModel([]), workspace, tmp_path, ledger, budgets)
    events, record_event = _events()

    outcome = _run_member(
        step,
        deps,
        record_event,
        [
            _ai(
                [
                    _call("read_file", {"path": "README.md"}, "r1"),
                    _call("read_file", {"path": "README.md"}, "r2"),
                ],
                usage=_usage(100, 10),
            )
        ],
    )

    assert outcome.ok is False
    assert outcome.report is None
    assert outcome.billable_tool_calls == 2
    assert outcome.limit_fact is not None
    assert "ceiling" in outcome.text
    payload = _ended(events)
    assert payload["reported"] is False
    assert payload["ending"] == "ceiling"
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.tool_calls == 2
    assert budgets.open_hold_ids() == ()


# --- Ended-payload derivation (pure) ------------------------------------------


def test_ended_payload_reported() -> None:
    """A reported result derives answering model, peak, duration and usage."""
    result = SubagentResult(
        call_id="s1",
        description="look",
        status="reported",
        reason="reported",
        report="findings",
        messages=[],
        billable_tool_calls=0,
        peak_context=ConversationContext(
            value=500,
            basis="anchored",
            anchor_input_tokens=400,
            anchor_request_ref="subagent:s1#response-1",
            appended_estimate_tokens=100,
        ),
        duration_seconds=1.5,
    )
    cell = OriginCell(input_tokens=100, output_tokens=10, responses=1)

    payload = subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        result=result,
        response_models=["test-model"],
        member_cell=cell,
        usable_threshold=1000,
    )

    assert payload["reported"] is True
    assert payload["answering_model"] == "test-model"
    assert payload["response_models"] == ["test-model"]
    assert payload["ending"] == "reported"
    assert payload["duration_seconds"] == 1.5
    assert payload["usage"] == cell.as_readable()
    peak = payload.get("conversation_context_peak")
    assert isinstance(peak, dict)
    assert peak["value"] == 500
    assert peak["basis"] == "anchored"
    assert "provider_error" not in payload
    assert "conversation_context" not in payload
    assert "context_cause" not in payload


def test_ended_payload_provider_failure() -> None:
    """A provider failure carries the redacted error beside the answering model."""
    result = SubagentResult(
        call_id="s1",
        description="look",
        status="no-report",
        reason="provider-failure",
        report=None,
        messages=[],
        provider_error="ModelError 500",
        duration_seconds=2.0,
    )

    payload = subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        result=result,
        response_models=[None, "test-model"],
        member_cell=OriginCell(responses=1),
        usable_threshold=None,
    )

    assert payload["reported"] is False
    assert payload["ending"] == "provider-failure"
    assert payload["provider_error"] == "ModelError 500"
    assert payload["answering_model"] == "test-model"
    assert payload["response_models"] == [None, "test-model"]
    assert "usable_threshold" not in payload


def test_ended_payload_context_window_guard() -> None:
    """A guard ending carries the compared context, cause and threshold."""
    result = SubagentResult(
        call_id="s1",
        description="look",
        status="no-report",
        reason="context-window",
        report=None,
        messages=[],
        end_context=ConversationContext(value=1200, basis="estimated"),
        context_cause="guard",
        duration_seconds=0.5,
    )

    payload = subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        result=result,
        response_models=[],
        member_cell=OriginCell(),
        usable_threshold=1000,
    )

    assert payload["ending"] == "context-window"
    assert payload["context_cause"] == "guard"
    assert "overflow_signal" not in payload
    assert payload["usable_threshold"] == 1000
    context = payload.get("conversation_context")
    assert isinstance(context, dict)
    assert context["value"] == 1200
    assert "conversation_context_peak" not in payload


def test_ended_payload_context_overflow() -> None:
    """An overflow ending carries both the cause and the refused signal."""
    result = SubagentResult(
        call_id="s1",
        description="look",
        status="no-report",
        reason="context-window",
        report=None,
        messages=[],
        end_context=ConversationContext(value=1300, basis="estimated"),
        context_cause="context-overflow",
        overflow_signal="refused",
        peak_context=ConversationContext(value=1300, basis="estimated"),
        duration_seconds=0.5,
    )

    payload = subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        result=result,
        response_models=["test-model"],
        member_cell=OriginCell(responses=1),
        usable_threshold=1000,
    )

    assert payload["context_cause"] == "context-overflow"
    assert payload["overflow_signal"] == "refused"
    assert payload["answering_model"] == "test-model"


def test_ended_payload_stopped() -> None:
    """A stopped result reports no model and no report."""
    result = SubagentResult(
        call_id="s1",
        description="look",
        status="no-report",
        reason="stopped",
        report=None,
        messages=[],
        duration_seconds=0.25,
    )

    payload = subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        result=result,
        response_models=[],
        member_cell=OriginCell(),
        usable_threshold=None,
    )

    assert payload["reported"] is False
    assert payload["ending"] == "stopped"
    assert payload["answering_model"] is None
    assert payload["response_models"] == []


def test_ended_payload_refused() -> None:
    """A refused member never runs: zero duration, an all-zero cell, no peak."""
    payload = refused_subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=2,
        call_id="spawn-2",
    )

    assert payload["reported"] is False
    assert payload["ending"] == "refused"
    assert payload["answering_model"] is None
    assert payload["response_models"] == []
    assert payload["duration_seconds"] == 0.0
    assert origin_cell_from_readable(payload["usage"]) == OriginCell()
    assert "conversation_context_peak" not in payload


def test_ended_payload_pin_violation() -> None:
    """A pin violation carries the observed identity with the member's cell."""
    cell = OriginCell(input_tokens=100, output_tokens=10, responses=1)
    peak = ConversationContext(
        value=500,
        basis="anchored",
        anchor_input_tokens=400,
        anchor_request_ref="subagent:s1#response-1",
        appended_estimate_tokens=100,
    )

    payload = pin_violation_subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        observed="model-other",
        member_cell=cell,
        duration_seconds=1.0,
        peak=peak,
    )

    assert payload["reported"] is False
    assert payload["ending"] == "pin-violation"
    assert payload["answering_model"] == "model-other"
    assert payload["response_models"] == ["model-other"]
    assert payload["usage"] == cell.as_readable()
    assert payload["duration_seconds"] == 1.0
    assert payload["conversation_context_peak"] == peak.to_record()

    without_peak = pin_violation_subagent_ended_payload(
        attempt_id=ATTEMPT_ID,
        step_id="process",
        seq=1,
        call_id="s1",
        observed=None,
        member_cell=OriginCell(),
        duration_seconds=0.0,
        peak=None,
    )
    assert without_peak["answering_model"] is None
    assert "conversation_context_peak" not in without_peak


# --- Spawn group: ordering, facts, aggregation --------------------------------


def test_group_orders_outcomes_and_totals_billable(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Two reporting members join the caller in call order; spend is metered."""
    step = _agent_step()
    budgets = _budgets(step)
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
            _ai([_report_subagent_call("second findings")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 6,
    )

    outcome = _run_step(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    ended = {
        event["payload"]["call_id"]: event["payload"]
        for event in outcome.events
        if event["kind"] == SUBAGENT_ENDED_KIND
    }
    assert set(ended) == {"spawn-1", "spawn-2"}
    assert ended["spawn-1"]["reported"] is True
    assert ended["spawn-2"]["reported"] is True
    assert ended["spawn-1"]["ending"] == "reported"
    assert ended["spawn-2"]["ending"] == "reported"
    # Two spawns plus one read per member; neither report counts.
    assert budgets.step_totals("process").tool_calls == 4
    assert budgets.open_hold_ids() == ()


def test_group_primary_fact_is_first_crossing(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The first crossing batch is the primary fact; every member still ends."""
    step = _agent_step(limits={"tool-calls": 3.0})
    budgets = _budgets(step)
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
                [
                    _call("read_file", {"path": "README.md"}, "r1"),
                    _call("read_file", {"path": "README.md"}, "r2"),
                ],
                usage=_usage(10, 5),
            ),
            _ai(
                [
                    _call("read_file", {"path": "README.md"}, "r3"),
                    _call("read_file", {"path": "README.md"}, "r4"),
                ],
                usage=_usage(10, 5),
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 4,
    )

    outcome = _run_step(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "limit-exceeded"
    fact = outcome.limit_fact
    assert fact is not None
    assert fact["scope"] == "step"
    assert fact["moment"] == "tool-call"
    # The first crossing batch is the primary fact: two spawns plus the
    # first member's batch of two, spent to 4.0 against the 3.0 limit.
    spend = fact.get("spend")
    assert isinstance(spend, dict)
    assert spend.get("tool-calls") == 4.0
    ended = {
        event["payload"]["call_id"]: event["payload"]
        for event in outcome.events
        if event["kind"] == SUBAGENT_ENDED_KIND
    }
    assert set(ended) == {"spawn-1", "spawn-2"}
    assert ended["spawn-1"]["ending"] == "ceiling"
    assert ended["spawn-2"]["ending"] == "ceiling"
    # Two spawns plus the first member's crossing batch of two: the second
    # member's request settles across the already-crossed limit, so its
    # batch never charges and the meter stops at 4.
    assert budgets.step_totals("process").tool_calls == 4
    assert budgets.open_hold_ids() == ()


def test_group_queued_never_started_under_stop(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A stop with holds open settles every member unseen: no member events."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai([_spawn_call("look", "read the workspace files", "s1")], usage=_usage(10, 5)),
            _ai([_report_subagent_call("unseen")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )
    _open_attempt(ledger)
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=_prices(),
        pin=make_pin(),
        # The member's spawn-time hold is open before any member runs.
        is_stopped=lambda: len(deps.budgets.open_hold_ids()) > 0,
    )

    with pytest.raises(ShutdownRequested):
        run_agent_step(_workflow(step).steps[step.id], 1, deps, {})

    # The member never ran: only the main spawn response was consumed.
    assert len(model.invocations) == 1
    kinds = [record.kind for record in ledger.records(ATTEMPT_ID)]
    assert not [kind for kind in kinds if kind.startswith("subagent_")]
    assert budgets.open_hold_ids() == ()
    assert ledger.list_open_admission_holds(ATTEMPT_ID) == ()


def test_group_invalid_spawn_admission_starts_nothing(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """An invalid spawn call returns its error; no member starts, no event."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [
            _ai(
                [_spawn_call("", "read the workspace files", "s1")],
                usage=_usage(10, 5),
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 2,
    )

    outcome = _run_step(step, model, workspace, tmp_path, ledger, budgets)

    assert outcome.status == "reported"
    kinds = [event["kind"] for event in outcome.events]
    assert not [kind for kind in kinds if kind.startswith("subagent_")]
    assert budgets.open_hold_ids() == ()


def test_member_pin_violation_ends_with_no_report_and_reraises(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A response from another model ends pin-violation and halts the worker."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [_ai([_report_subagent_call("findings")], usage=_usage(100, 10))],
        response_models=["model-other"],
    )
    deps = _member_deps(model, workspace, tmp_path, ledger, budgets)
    events, record_event = _events()

    member = SubagentMember.admit(deps, step, 1, "s1", "look", "read files", record_event)
    assert member.early_outcome is None
    with pytest.raises(PinViolation):
        member.run(deps, step, record_event)

    payload = _ended(events)
    assert payload["reported"] is False
    assert payload["ending"] == "pin-violation"
    assert payload["answering_model"] == "model-other"
    assert payload["response_models"] == ["model-other"]
    assert budgets.open_hold_ids() == ()
    assert ledger.list_open_admission_holds(ATTEMPT_ID) == ()


def test_request_model_seam_appends_before_returning() -> None:
    """The injected seam's contract, pinned: append, then return the response."""
    messages: list[BaseMessage] = [_ai(None, usage=_usage(10, 5))]
    tools: list[BaseTool] = []
    response = _ai([_report_subagent_call("findings")], usage=_usage(100, 10))

    returned = _scripted_request([response], observed=None)(object(), messages, tools)

    assert returned is response
    assert messages[-1] is response


def test_member_run_without_request_model_uses_shared_lifecycle(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Production passes no seam: the default request runs through the ledger."""
    step = _agent_step()
    budgets = _budgets(step)
    model = ScriptedModel(
        [_ai([_report_subagent_call("findings")], usage=_usage(100, 10))],
        response_models=["test-model"],
    )
    deps = _member_deps(model, workspace, tmp_path, ledger, budgets)
    events, record_event = _events()

    member = SubagentMember.admit(deps, step, 1, "s1", "look", "read files", record_event)
    outcome = member.run(deps, step, record_event)

    assert outcome.ok is True
    assert outcome.report == "findings"
    payload = _ended(events)
    assert payload["reported"] is True
    cell = origin_cell_from_readable(payload["usage"])
    assert cell.responses == 1
    assert cell.input_tokens == 100
    assert budgets.open_hold_ids() == ()


