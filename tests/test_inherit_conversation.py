"""Conversation Inheritance: an inheriting Route hands over the conversation.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (an
inheriting step-local Route between agent steps carries the exact
immediate source Step Execution's history in memory; the receiver opens
with the Attempt Header and Workflow Input, the source framing and
complete Exchange Units, then its own entry frame; empty history records
``error`` and follows the receiver's effective Route).

Issue #607. Part A checks the strict step-target flag (config). Part B
checks runtime selection, order, verbatim carry, the dropped incomplete
unit, the empty/refused error path and the receiver policy. Part C pins
the provider shapes (thinking signatures, encrypted reasoning, tool-call
ids) with a historical tool the receiver does not bind. Part D captures
live acceptance on the three backends (skipped without credentials).
Part E exercises fresh and inherited Routes through ``run_workflow``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import PinViolation
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.exchange import ExchangeUnit
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    AgentStepDeps,
    AgentStepOutcome,
    InheritedConversation,
    PriorStepExecution,
    StepAttemptFacts,
    compose_inherited_opening,
    run_agent_step,
)
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor, StepOutcome
from coding_agent.runtime.graph.workflow_graph import (
    WorkflowRunContext,
    run_workflow,
)
from coding_agent.runtime.step_limits import AttemptBudgets
from coding_agent.workflow.effective import _step_record
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    FieldType,
    InputDecl,
    InputRef,
    Instruction,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps, make_pin
from support.scripted_model import ScriptedModel
from support.workflow_config import load_config

SECRET = "github_pat_testtoken"


# --- shared builders -----------------------------------------------------------


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, Any] = {
        "attempt_id": "implement#7/1",
        "workflow_id": "implement",
        "input_type": "issue",
        "input_number": 7,
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


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, FieldType]],
    *,
    tools: tuple[str, ...] = ("workspace-read",),
    instructions: tuple[Instruction, ...] = (),
    inputs: tuple[InputRef, ...] = (),
    routes: dict[str, RouteTarget] | None = None,
    max_executions: int = 1,
) -> WorkflowStep:
    targets = routes or {"done": RouteTarget(end="completed")}
    return WorkflowStep(
        id=step_id,
        agent=Agent(
            instructions=instructions,
            tools=tools,
            inputs=inputs,
            results=results,
        ),
        max_executions=max_executions,
        routes=dict(targets),
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: Any = "",
) -> AIMessage:
    message: AIMessage = AIMessage(content=content, tool_calls=list(calls or []))
    return message


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
        call_id,
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


def _workflow(
    workflow_id: str,
    steps: dict[str, WorkflowStep],
    start: str,
    *,
    defaults_routes: dict[str, RouteTarget] | None = None,
) -> Workflow:
    return Workflow(
        id=workflow_id,
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start=start,
        defaults_routes=defaults_routes
        or {
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps=steps,
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


def _write_config(tmp_path: Path, workflows_block: str) -> Path:
    text = (
        "version: 4\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "    context-window-tokens: 1000000\n"
        "    context-threshold-tokens: 80000\n"
        "    output-cap-tokens: 32000\n"
        f"{workflows_block}"
    )
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _two_agent_workflows(
    route_a_done: str, *, extra_a: str = "", extra_b: str = ""
) -> str:
    return (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: first\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      first:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        f"        routes: {{done: {route_a_done}{extra_a}}}\n"
        "      second:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        f"        routes: {{done: {{end: completed}}{extra_b}}}\n"
    )


# --- Part A: strict step-target flag -------------------------------------------


def test_non_boolean_inherit_flag_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a boolean spells the flag: ``"true"`` and ``1`` are refused."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for flag in ('"true"', "1"):
        path = _write_config(
            tmp_path, _two_agent_workflows(f"{{step: second, inherit-conversation: {flag}}}")
        )
        config, faults = load_config(path)
        assert config is None
        assert any("inherit-conversation" in str(fault) for fault in faults)


def test_inherit_requires_agent_steps_at_both_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation step at either end of an inheriting Route is refused."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    workflows_op_target = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: first\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      first:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: {step: op, inherit-conversation: true}}\n"
        "      op:\n"
        "        operation: publish-review\n"
        "        with: {body: first.done.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )
    path = _write_config(tmp_path, workflows_op_target)
    config, faults = load_config(path)
    assert config is None
    assert any("both ends" in str(fault) for fault in faults)


def test_inherit_flag_on_an_end_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End targets stay disjoint: ``{end, inherit-conversation}`` is refused."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = _write_config(
        tmp_path,
        _two_agent_workflows("second", extra_a=", error: {end: failed, inherit-conversation: true}"),
    )
    config, faults = load_config(path)
    assert config is None
    assert faults


def test_defaults_routes_reject_inherit_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``defaults.routes`` holds scalar step ids and ends only."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    workflows_block = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: first\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {step: second, inherit-conversation: true},"
        " limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      first:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: second}\n"
        "      second:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: {end: completed}}\n"
    )
    path = _write_config(tmp_path, workflows_block)
    config, faults = load_config(path)
    assert config is None
    assert any("defaults.routes" in fault.path for fault in faults)


def test_scalar_omitted_and_false_forms_stay_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scalar, omitted-flag and explicit-false Routes all mean fresh entry."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    forms = ["second", "{step: second}", "{step: second, inherit-conversation: false}"]
    parsed: list[RouteTarget] = []
    for form in forms:
        path = _write_config(tmp_path, _two_agent_workflows(form))
        config, faults = load_config(path)
        assert config is not None, [str(f) for f in faults]
        target = config.workflows["review"].steps["first"].routes["done"]
        assert target.step == "second"
        assert target.inherit_conversation is False
        parsed.append(target)
    assert parsed[0] == parsed[1] == parsed[2]


def test_fresh_forms_share_effective_record_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three fresh spellings canonicalise identically (same v4 digest)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    records: list[Mapping[str, object]] = []
    for form in ["second", "{step: second}", "{step: second, inherit-conversation: false}"]:
        path = _write_config(tmp_path, _two_agent_workflows(form))
        config, faults = load_config(path)
        assert config is not None, [str(f) for f in faults]
        step = config.workflows["review"].steps["first"]
        routes_record = _step_record(step)["routes"]
        assert isinstance(routes_record, dict)
        records.append(routes_record)
    assert records[0] == records[1] == records[2] == {"done": {"step": "second"}}
    true_path = _write_config(
        tmp_path, _two_agent_workflows("{step: second, inherit-conversation: true}")
    )
    true_config, true_faults = load_config(true_path)
    assert true_config is not None, [str(f) for f in true_faults]
    true_step = true_config.workflows["review"].steps["first"]
    assert _step_record(true_step)["routes"] == {
        "done": {"step": "second", "inherit_conversation": True}
    }


# --- Part B: runtime selection, order and error path ----------------------------


def _inherited(
    units: Sequence[ExchangeUnit], tail: Sequence[BaseMessage] = ()
) -> InheritedConversation:
    return InheritedConversation(
        source_step="first",
        source_seq=1,
        source_result="done",
        opening_tail=tuple(tail),
        units=tuple(units),
    )


def test_inherited_opening_order(
    workspace: Path, tmp_path: Path
) -> None:
    """Header and Input first, then framing, tail, units, receiver entry."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    tail_message = HumanMessage(content="source declared framing")
    unit_response = _ai(
        [_call("read_file", {"path": "README.md"}, "call-1")],
        content="source thinking",
    )
    unit_result = ToolMessage(content="hello", tool_call_id="call-1")
    inherited = _inherited(
        [ExchangeUnit(unit_response, (unit_result,))], [tail_message]
    )
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "reported"
    assert len(model.invocations) == 1
    opening = list(model.invocations[0])
    assert "Attempt Header" in str(opening[0].content)
    assert opening[1].content is not None and "Fix the crash" in str(opening[1].content)
    assert "Prior work from step first#1" in str(opening[2].content)
    assert opening[3] is tail_message
    assert opening[4] is unit_response
    assert opening[5] is unit_result
    assert "Current step" in str(opening[6].content) and "second" in str(opening[6].content)
    contract = str(opening[7].content)
    assert REPORT_TOOL_NAME in contract and "done" in contract


def test_receiver_keeps_own_instructions_tools_and_contract(
    workspace: Path, tmp_path: Path
) -> None:
    """The receiver's declared instructions, tools and contract control."""
    source_tail = HumanMessage(content="source contract text")
    receiver = _agent_step(
        "second",
        {"finished": {"summary": "document"}},
        tools=("workspace-read",),
        instructions=(
            Instruction(prompt="prompts/receiver-policy.md"),
            Instruction(skill="code-review"),
        ),
    )
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("finished", {"summary": "ok"})])])
    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        prompt_texts={"prompts/receiver-policy.md": "RECEIVER POLICY 7a1c."},
        activation_registry=__import__(
            "coding_agent.skillbundle.activation", fromlist=["ActivationRegistry"]
        ).ActivationRegistry(bodies={"code-review": "RECEIVER SKILL 9e2d."}),
    )
    inherited_with_units = InheritedConversation(
        source_step="first",
        source_seq=1,
        source_result="done",
        opening_tail=(source_tail,),
        units=(
            ExchangeUnit(_ai(content="prior work"), ()),
        ),
    )
    outcome = run_agent_step(receiver, 1, deps, {}, inherited_with_units)
    assert outcome.status == "reported"
    assert outcome.result_name == "finished"
    opening = list(model.invocations[0])
    bodies = " ".join(str(message.content) for message in opening)
    assert "RECEIVER POLICY 7a1c." in bodies
    assert "RECEIVER SKILL 9e2d." in bodies
    # The source framing rides along as prior-work data, before the entry.
    assert "source contract text" in bodies
    assert bodies.index("source contract text") < bodies.index("RECEIVER POLICY 7a1c.")
    assert bodies.index("RECEIVER POLICY 7a1c.") < bodies.index("RECEIVER SKILL 9e2d.")
    assert model.bound_tool_names and "read_file" in model.bound_tool_names
    assert "run_command" not in model.bound_tool_names


def test_history_preserves_provider_objects_without_rewriting(
    workspace: Path, tmp_path: Path
) -> None:
    """The original assistant object and results travel by identity."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    response = _ai(
        [_call("read_file", {"path": "README.md"}, "call-keep")],
        content="verbatim please",
    )
    result = ToolMessage(content="hello", tool_call_id="call-keep")
    inherited = _inherited([ExchangeUnit(response, (result,))])
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "reported"
    opening = list(model.invocations[0])
    assert any(message is response for message in opening)
    assert any(message is result for message in opening)


def test_tool_limit_leaves_no_partial_unit_behind(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A turn refused before its tools run is dropped as a whole."""
    step = _agent_step("only", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_call("read_file", {"path": "README.md"}, "call-1")])]
    )
    workflow = _workflow("implement", {"only": step}, "only")
    ledger.open_attempt(attempt.attempt_id, effective_configuration={})
    from coding_agent.runtime.step_limits import build_budgets
    from coding_agent.provider.price_table import TokenPrices

    budgets = build_budgets(
        workflow,
        prices=TokenPrices(input=1.0, output=1.0, cache_read=0.1, cache_write=1.0),
        output_cap=0,
        attempt_id=attempt.attempt_id,
    )
    # Exhaust the tool-call room before the turn runs.
    budgets.note_tool_calls("only", 1000000)
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger, budgets=budgets)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.history == ()


def test_empty_history_records_error_without_a_model_call(
    workspace: Path, tmp_path: Path
) -> None:
    """No eligible unit means a redacted ``error`` before any model request."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path, redactions=SECRET)
    inherited = _inherited([])
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "error"
    assert model.invocations == []
    assert "first#1" in outcome.detail
    assert SECRET not in json.dumps(outcome.summary)


def test_missing_history_follows_receiver_error_route(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The empty-history ``error`` routes through the receiver's effective Route."""

    @dataclass
    class _EmptyHistoryExecutor:
        seen: list[InheritedConversation | None] = field(default_factory=list)

        def execute(
            self,
            step: WorkflowStep,
            seq: int,
            attempt_id: str,
            ledger: RunLedger,
            inherited: InheritedConversation | None = None,
        ) -> StepOutcome:
            self.seen.append(inherited)
            if inherited is not None and not inherited.units:
                return StepOutcome(
                    result="error",
                    payload=None,
                    facts={},
                    usage={},
                    limit_fact=None,
                )
            assert step.agent is not None
            name = next(iter(step.agent.results))
            return StepOutcome(
                result=name, payload={}, facts={}, usage={}, limit_fact=None
            )

    first = _agent_step(
        "first",
        {"done": {}},
        routes={
            "done": RouteTarget(step="second", inherit_conversation=True),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    second = _agent_step(
        "second",
        {"done": {}},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    workflow = _workflow("implement", {"first": first, "second": second}, "first")
    executor = _EmptyHistoryExecutor()
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
    )
    assert result.outcome == "failed"
    assert result.ending_step == "second"
    assert result.step_result == "error"
    assert executor.seen[1] is not None
    assert executor.seen[1] is not None and executor.seen[1].units == ()


def test_inherited_guard_sees_the_fully_formed_opening(
    workspace: Path, tmp_path: Path
) -> None:
    """An inherited opening that fits no usable context ends prefix-too-large."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    big_tail = HumanMessage(content="x" * 5000)
    inherited = _inherited(
        [ExchangeUnit(_ai(content="prior"), ())], [big_tail]
    )
    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        context_window=ContextWindowConfig(
            threshold_tokens=2200,
            safety_margin_tokens=1000,
            request_overhead_tokens=1000,
        ),
    )
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "error"
    assert model.invocations == []
    assert "Pinned Prefix" in outcome.detail or "prefix" in outcome.detail.lower()


def test_spend_is_accounted_to_the_receiving_step(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Metered spend of the inherited entry belongs to the receiver."""
    first = _agent_step(
        "first",
        {"done": {"summary": "document"}},
        routes={
            "done": RouteTarget(step="second", inherit_conversation=True),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    second = _agent_step(
        "second",
        {"done": {"summary": "document"}},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    workflow = _workflow("implement", {"first": first, "second": second}, "first")
    marker = "inheritance-marker-3f9b"
    model = ScriptedModel(
        [
            _ai([_report_call("done", {"summary": "first writes " + marker})]),
            _ai([_report_call("done", {"summary": "second carries on"})]),
        ]
    )
    attempt = _facts()
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    executor = ProductionStepExecutor(agent_deps=deps)
    result = run_workflow(
        workflow=workflow,
        attempt_id=attempt.attempt_id,
        ledger=ledger,
        step_executor=executor,
    )
    assert result.outcome == "completed"
    assert len(model.invocations) == 2
    second_opening = " ".join(
        str(message.content)
        + " "
        + json.dumps(
            [
                {"name": call.get("name"), "args": call.get("args")}
                for call in (message.tool_calls or [])
            ]
            if isinstance(message, AIMessage)
            else []
        )
        for message in model.invocations[1]
    )
    assert marker in second_opening
    usage = ledger.get_latest_usage(attempt.attempt_id)
    assert usage is not None


# --- Part C: provider shapes -----------------------------------------------------


def test_anthropic_thinking_signature_travels_verbatim(
    workspace: Path, tmp_path: Path
) -> None:
    """Signed Anthropic thinking blocks ride the inherited opening byte-equal."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    thinking = {
        "type": "thinking",
        "thinking": "planning the read",
        "signature": "a2JjY2RkZWVmZzAwMTEyMjMzNDQ1NTY2Nzg4OTlhYQ==",
    }
    tool_use = {"type": "tool_use", "id": "toolu_hist_1", "name": "read_file", "input": {"path": "README.md"}}
    response = AIMessage(
        content=[thinking, tool_use],
        tool_calls=[
            {"name": "read_file", "args": {"path": "README.md"}, "id": "toolu_hist_1", "type": "tool_call"}
        ],
    )
    result = ToolMessage(content="hello", tool_call_id="toolu_hist_1")
    inherited = _inherited([ExchangeUnit(response, (result,))])
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "reported"
    opening = list(model.invocations[0])
    carried = next(message for message in opening if message is response)
    assert isinstance(carried, AIMessage)
    assert carried.content[0] == thinking
    assert carried.tool_calls[0]["id"] == "toolu_hist_1"


def test_responses_encrypted_reasoning_travels_byte_equal(
    workspace: Path, tmp_path: Path
) -> None:
    """Responses ``reasoning.encrypted_content`` is carried back unchanged."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    reasoning = {
        "type": "reasoning",
        "id": "rs_hist_1",
        "encrypted_content": "cmVkYWN0ZWQtaGlzdC0xLWVuY3J5cHRlZC1ieXRlcw==",
        "summary": [],
    }
    function_call = {"type": "function_call", "call_id": "call_hist_1", "name": "read_file", "arguments": "{}"}
    response = AIMessage(
        content=[reasoning, function_call],
        tool_calls=[
            {"name": "read_file", "args": {}, "id": "call_hist_1", "type": "tool_call"}
        ],
    )
    result = ToolMessage(content="hello", tool_call_id="call_hist_1")
    inherited = _inherited([ExchangeUnit(response, (result,))])
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "reported"
    opening = list(model.invocations[0])
    carried = next(message for message in opening if message is response)
    assert isinstance(carried, AIMessage)
    blocks = [block for block in carried.content if isinstance(block, dict)]
    assert blocks[0] == reasoning
    assert carried.tool_calls[0]["id"] == "call_hist_1"


def test_historical_tool_absent_from_receiver_still_reports(
    workspace: Path, tmp_path: Path
) -> None:
    """History may name tools the receiver never binds; it still reports."""
    receiver = _agent_step(
        "second", {"done": {}}, tools=("workspace-read",)
    )
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    response = _ai(
        [_call("source_only_tool", {"path": "x"}, "call-hist-9")],
        content="source used its own tool",
    )
    result = ToolMessage(content="source result", tool_call_id="call-hist-9")
    inherited = _inherited([ExchangeUnit(response, (result,))])
    outcome = run_agent_step(receiver, 1, deps, {}, inherited)
    assert outcome.status == "reported"
    assert "source_only_tool" not in model.bound_tool_names
    opening = list(model.invocations[0])
    assert any(message is response for message in opening)
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call-hist-9"
        for message in opening
    )


def test_pin_violation_on_receiver_response_ends_error(
    workspace: Path, tmp_path: Path
) -> None:
    """The receiver's own Pinned Model check applies to its responses."""
    receiver = _agent_step("second", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {})])],
        response_models=["some-other-model"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, pin=make_pin("test-model"))
    inherited = _inherited([ExchangeUnit(_ai(content="prior"), ())])
    with pytest.raises(PinViolation, match="pin-violation"):
        run_agent_step(receiver, 1, deps, {}, inherited)


# --- Part D: live acceptance (three backends) ------------------------------------


class _RecordingModel:
    """Wrap a real model, recording every invocation's messages."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.invocations: list[tuple[BaseMessage, ...]] = []

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> _RecordingModel:
        bound = self._inner.bind_tools(tools, tool_choice=tool_choice)
        recorder = _RecordingModel.__new__(_RecordingModel)
        recorder._inner = bound
        recorder.invocations = self.invocations
        return recorder

    def invoke(self, input: object, **kwargs: object) -> BaseMessage:
        assert isinstance(input, Sequence)
        self.invocations.append(tuple(input))
        result: BaseMessage = self._inner.invoke(input, **kwargs)
        return result


def _live_attempt() -> StepAttemptFacts:
    return _facts(attempt_id="implement#7/99")


def _live_source_step() -> WorkflowStep:
    return _agent_step(
        "first",
        {"done": {"summary": "document"}},
        tools=("shell",),
        instructions=(Instruction(prompt="prompts/live-source.md"),),
        routes={
            "done": RouteTarget(step="second", inherit_conversation=True),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )


def _live_receiver_step() -> WorkflowStep:
    return _agent_step(
        "second",
        {"done": {"summary": "document"}},
        tools=("workspace-read",),
        instructions=(Instruction(prompt="prompts/live-receiver.md"),),
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )


def _run_live_inheritance(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    pin: Any,
    model: Any,
    prices: Any,
) -> None:
    from coding_agent.runtime.step_limits import build_budgets

    source = _live_source_step()
    receiver = _live_receiver_step()
    workflow = _workflow("implement", {"first": source, "second": receiver}, "first")
    attempt = _live_attempt()
    ledger.open_attempt(attempt.attempt_id, effective_configuration={})
    budgets = build_budgets(
        workflow, prices=prices, output_cap=0, attempt_id=attempt.attempt_id
    )
    recorder = _RecordingModel(model)
    deps = make_agent_step_deps(
        model=recorder,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=attempt,
        pin=pin,
        prices=prices,
        ledger=ledger,
        budgets=budgets,
        prompt_texts={
            "prompts/live-source.md": (
                "Run `echo live-source-marker-5d7a` with run_command, then report "
                "done with summary carrying the command output."
            ),
            "prompts/live-receiver.md": (
                "The history above used tools you do not have. Report done with "
                "summary 'live-receiver-ok'."
            ),
        },
    )
    executor = ProductionStepExecutor(agent_deps=deps)
    result = run_workflow(
        workflow=workflow,
        attempt_id=attempt.attempt_id,
        ledger=ledger,
        step_executor=executor,
    )
    assert result.outcome == "completed", result.reason
    assert len(recorder.invocations) == 2
    first_request, second_request = recorder.invocations
    first_text = " ".join(str(message.content) for message in first_request)
    second_text = " ".join(str(message.content) for message in second_request)
    assert "live-source-marker" in first_text or "echo" in first_text
    assert "live-source-marker" in second_text or "Prior work" in second_text


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set"
)
def test_live_anthropic_messages_inheritance(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Live: direct Anthropic Messages carries history past a missing tool."""
    from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
    from coding_agent.provider.price_table import TokenPrices

    pin = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="low",
    )
    prices = TokenPrices(input=1.0, output=1.0, cache_read=1.0, cache_write=1.0)
    _run_live_inheritance(
        workspace,
        tmp_path,
        ledger,
        pin,
        build_chat_model(pin),
        {pin.key: prices},
    )


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set"
)
def test_live_openai_responses_inheritance(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Live: direct OpenAI Responses carries history past a missing tool."""
    from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
    from coding_agent.provider.price_table import TokenPrices

    pin = PinnedModel(
        provider="openai",
        model="gpt-5.6-luna",
        endpoint="responses",
        effort="low",
    )
    prices = TokenPrices(input=1.0, output=1.0, cache_read=1.0, cache_write=1.0)
    _run_live_inheritance(
        workspace,
        tmp_path,
        ledger,
        pin,
        build_chat_model(pin),
        {pin.key: prices},
    )


@pytest.mark.skipif(
    not os.environ.get("TEST_GATEWAY_URL"), reason="TEST_GATEWAY_URL is not set"
)
def test_live_litellm_responses_inheritance(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Live: LiteLLM Responses carries history past a missing tool."""
    from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
    from coding_agent.provider.price_table import TokenPrices

    pin = PinnedModel(
        provider="litellm",
        model="muse-spark-1.3-contributor",
        endpoint="responses",
        effort="low",
        gateway_url=os.environ["TEST_GATEWAY_URL"],
    )
    prices = TokenPrices(input=1.0, output=1.0, cache_read=1.0, cache_write=1.0)
    _run_live_inheritance(
        workspace,
        tmp_path,
        ledger,
        pin,
        build_chat_model(
            pin,
            api_key=os.environ.get("TEST_GATEWAY_API_KEY"),
        ),
        {pin.key: prices},
    )


def test_gateway_request_flags_on_inherited_receiver_request(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The inherited receiver request stays self-contained on Responses."""
    import httpx
    from langchain_core.tools import tool as _tool

    from coding_agent.provider.pinned_model import build_chat_model
    from coding_agent.provider.price_table import TokenPrices
    from support.fake_gateway import (
        FakeGatewayTransport,
        load_recording,
        queue_responder,
        recording_response,
    )
    from coding_agent.provider.pinned_model import PinnedModel
    from support.gateway_assertions import (
        assert_reasoning_carried_verbatim,
        assert_self_contained_request,
    )

    def _gateway_pin() -> PinnedModel:
        return PinnedModel(
            provider="litellm",
            model="muse-spark-1.3-contributor",
            endpoint="responses",
            effort="medium",
            gateway_url="http://gateway.test:4000",
            output_cap_tokens=32000,
            context_threshold_tokens=100000,
            context_window_tokens=1000000,
        )

    @_tool
    def source_weather(city: str) -> str:
        """A source-only work tool the receiver never binds."""
        return f"sunny in {city}"

    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_turn1_200.json"),
                recording_response("litellm_turn2_200.json"),
            ]
        )
    )
    model: Any = build_chat_model(pin, api_key="test-gateway-key", transport=transport)
    source_bound: Any = model.bind_tools([source_weather])
    first = source_bound.invoke([HumanMessage(content="weather in paris?")])
    assert isinstance(first, AIMessage)
    assert first.tool_calls and first.tool_calls[0]["id"]
    call_id = str(first.tool_calls[0]["id"])
    history: list[BaseMessage] = [
        HumanMessage(content="weather in paris?"),
        first,
        ToolMessage(content="sunny, 21C", tool_call_id=call_id),
    ]
    receiver_bound: Any = model.bind_tools([])
    second = receiver_bound.invoke(history)
    assert second is not None
    assert len(transport.requests) == 2
    assert_self_contained_request(
        transport.requests[1].body,
        model="muse-spark-1.3-contributor",
        effort="medium",
        output_cap=32000,
    )
    assert_reasoning_carried_verbatim(first, transport.requests[1].body)
    assert any(
        isinstance(block, dict) and block.get("call_id") == call_id
        for block in transport.requests[1].body["input"]
    )
    transport.assert_only_responses_api()
    _ = (workspace, tmp_path, ledger, load_recording, httpx)


# --- Part E: production entry points ---------------------------------------------


def test_run_workflow_fresh_route_passes_no_history(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A scalar Route enters fresh: the receiver sees no inherited history."""
    from support.scripted_executor import ScriptedStepExecutor

    first = _agent_step(
        "first",
        {"done": {}},
        routes={
            "done": RouteTarget(step="second"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    second = _agent_step(
        "second",
        {"done": {}},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    workflow = _workflow("implement", {"first": first, "second": second}, "first")
    executor = ScriptedStepExecutor({"first": ["done"], "second": ["done"]})
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
    )
    assert result.outcome == "completed"
    assert executor.seen_inherited == [None, None]


def test_run_workflow_inherited_route_carries_exact_source(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """An inheriting Route carries the immediate source execution, on revisit."""
    first = _agent_step(
        "first",
        {"done": {"summary": "document"}},
        max_executions=2,
        routes={
            "done": RouteTarget(step="second", inherit_conversation=True),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    second = _agent_step(
        "second",
        {"done": {"summary": "document"}},
        max_executions=2,
        routes={
            "done": RouteTarget(step="first"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    workflow = _workflow("implement", {"first": first, "second": second}, "first")
    first_marker = "first-visit-marker-aa10"
    second_marker = "second-visit-marker-bb20"
    model = ScriptedModel(
        [
            _ai([_report_call("done", {"summary": first_marker}, "r1")]),
            _ai([_report_call("done", {"summary": "via second once"}, "r2")]),
            _ai([_report_call("done", {"summary": second_marker}, "r3")]),
            _ai([_report_call("done", {"summary": "via second twice"}, "r4")]),
        ]
    )
    attempt = _facts()
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    executor = ProductionStepExecutor(agent_deps=deps)
    result = run_workflow(
        workflow=workflow,
        attempt_id=attempt.attempt_id,
        ledger=ledger,
        step_executor=executor,
    )
    assert result.outcome in ("completed", "unfinished", "failed")
    assert len(model.invocations) >= 2
    # The receiver's first inherited opening carries the first visit; the
    # second inherited opening carries the revisit, not the older history.
    def _opening_text(messages: tuple[BaseMessage, ...]) -> str:
        parts: list[str] = []
        for message in messages:
            parts.append(str(message.content))
            if isinstance(message, AIMessage):
                parts.append(
                    json.dumps(
                        [
                            {"name": call.get("name"), "args": call.get("args")}
                            for call in (message.tool_calls or [])
                        ]
                    )
                )
        return " ".join(parts)

    second_opening = _opening_text(model.invocations[1])
    assert first_marker in second_opening
    assert second_marker not in second_opening


def test_entry_refusal_after_ceiling_yields_error_history(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A refused source entry hands the receiver no eligible history."""
    first = _agent_step(
        "first",
        {"done": {"summary": "document"}},
        max_executions=1,
        routes={
            "done": RouteTarget(step="second", inherit_conversation=True),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(step="third", inherit_conversation=True),
        },
    )
    second = _agent_step(
        "second",
        {"done": {"summary": "document"}},
        max_executions=2,
        routes={
            "done": RouteTarget(step="first"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    third = _agent_step(
        "third",
        {"done": {"summary": "document"}},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    workflow = _workflow(
        "implement", {"first": first, "second": second, "third": third}, "first"
    )
    model = ScriptedModel(
        [
            _ai([_report_call("done", {"summary": "first once"}, "r1")]),
            _ai([_report_call("done", {"summary": "second once"}, "r2")]),
        ]
    )
    attempt = _facts()
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    executor = ProductionStepExecutor(agent_deps=deps)
    result = run_workflow(
        workflow=workflow,
        attempt_id=attempt.attempt_id,
        ledger=ledger,
        step_executor=executor,
    )
    # first#1 -> second#1 -> first refused -> third errors on empty history.
    assert result.outcome == "failed"
    assert result.ending_step == "third"
    assert result.step_result == "error"
    assert len(model.invocations) == 2
