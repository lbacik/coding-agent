"""A per-step Soft Context Threshold routing into a Reserved receiver (issue #608).

Contract ``docs/contract/step-workflow-runtime-contract.md`` §§3, 9.9, 11.4,
13.2: an agent step whose ``context-thresholds`` policy configures
``soft-tokens`` — strictly between the safety margin plus request overhead
and ``hard-tokens`` — evaluates the hard guard first, then the soft guard,
on the opening and every post-tool main-conversation request. A request
strictly over the Soft Usable Threshold but at or below the hard one is
never sent — no Admission hold, no provider call — and the Step Execution
reports ``soft-context-threshold`` onto the step-local inheriting Route to
an agent Reserved Step carrying an explicit hard-only policy and all four
Step Allowance meters. The receiver writes its handoff note from the
inherited history under its own allowance and the Attempt Ceiling (after
Work Pool exhaustion), keeping the deadline, the full output cap,
cumulative charging and bounded retries; the receiver's hard, ``error``
and ``limit-exceeded`` Routes stay model-free through Runtime Operations to
a Workflow End.

Builds on the Hard Context Threshold (#606) and Conversation Inheritance
(#607): the hard guard keeps precedence and the receiver rides the
inheritance machinery.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from coding_agent import cli
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.exchange import ExchangeUnit
from coding_agent.runtime.graph.agent_step import (
    MAX_REPORT_CORRECTIONS,
    REPORT_TOOL_NAME,
    InheritedConversation,
    StepAttemptFacts,
    build_agent_step_tools,
    compose_step_opening,
    run_agent_step,
)
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.runtime.step_limits import AttemptCeiling, build_budgets
from coding_agent.runtime.step_operations import OperationExecution
from coding_agent.workflow.schema import (
    SOFT_CONTEXT_RESULT,
    Agent,
    ContextThresholds,
    FieldType,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps
from support.effective_config import full_effective_configuration
from support.scripted_executor import ScriptedStepExecutor
from support.scripted_model import ScriptedModel
from support.workflow_config import load_config
from coding_agent.ledger.store import digest_effective_configuration
from coding_agent.workflow.effective import (
    dumps_effective_canonical,
    is_full_effective_configuration,
)


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, object] = {
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


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls or []), usage_metadata=usage)


def _report_call(result: str, payload: dict[str, object], call_id: str = "report-1") -> dict[str, object]:
    return _call(REPORT_TOOL_NAME, {"result": result, "payload_json": json.dumps(payload)}, call_id)


def _usage_dict(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _agent_step(
    step_id: str,
    *,
    hard: int | None = None,
    soft: int | None = None,
    tools: tuple[str, ...] = ("workspace-read",),
    results: dict[str, dict[str, FieldType]] | None = None,
    routes: dict[str, RouteTarget] | None = None,
    reserved: bool = False,
    limits: dict[str, float] | None = None,
) -> WorkflowStep:
    declared = dict(results) if results is not None else {"done": {}}
    targets = dict(routes) if routes is not None else {"done": RouteTarget(end="completed")}
    policy: ContextThresholds | None = None
    if hard is not None:
        policy = ContextThresholds(hard_tokens=hard, soft_tokens=soft)
        targets.setdefault("hard-context-threshold", RouteTarget(end="unfinished"))
        if soft is not None:
            targets.setdefault(SOFT_CONTEXT_RESULT, RouteTarget(end="unfinished"))
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results=declared),
        max_executions=1,
        context_thresholds=policy,
        routes=targets,
        reserved=reserved,
        limits=dict(limits) if limits is not None else {},
    )


def _window(threshold: int = 1_000_000) -> ContextWindowConfig:
    return ContextWindowConfig(
        threshold_tokens=threshold,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
    )


def _opening_estimate(step: WorkflowStep, workspace: Path, tmp_path: Path) -> int:
    probe = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
    )
    tools = build_agent_step_tools(step, probe)
    opening = compose_step_opening(step, probe, {})
    from coding_agent.runtime.conversation_context import estimate_context_tokens

    return estimate_context_tokens(list(opening), [], list(tools))


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _minimal_config(**overrides: str) -> str:
    blocks = {
        "version": "version: 4\n",
        "target": (
            "target:\n"
            "  kind: github\n"
            "  repository: OWNER/REPO\n"
            "  base-branch: main\n"
            "  token-env: GITHUB_TOKEN\n"
        ),
        "models": (
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
        ),
        "workflows": (
            "workflows:\n"
            "  review:\n"
            "    model: sonnet\n"
            "    input: {type: issue, label: agent:review}\n"
            "    limits: {cost-usd: 6, effective-tokens: 1500000,"
            " tool-calls: 300, wall-clock-minutes: 30}\n"
            "    start: process\n"
            "    defaults:\n"
            "      max-executions: 1\n"
            "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
            "    steps:\n"
            "      process:\n"
            "        agent:\n"
            "          tools: [workspace-read]\n"
            "          results:\n"
            "            done: {}\n"
            "        context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}\n"
            "        routes: {done: {end: completed}, hard-context-threshold: {end: unfinished}, soft-context-threshold: {step: scribe, inherit-conversation: true}}\n"
            "      scribe:\n"
            "        agent:\n"
            "          tools: [workspace-read]\n"
            "          results:\n"
            "            note: {}\n"
            "        reserved: true\n"
            "        limits: {cost-usd: 2, effective-tokens: 200000, tool-calls: 100, wall-clock-minutes: 10}\n"
            "        context-thresholds: {hard-tokens: 40000}\n"
            "        routes: {note: {end: completed}, hard-context-threshold: {end: unfinished}}\n"
        ),
    }
    blocks.update(overrides)
    return "".join(blocks[key] for key in ("version", "target", "models", "workflows"))


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    yield root


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


# --- Validation: shape, bounds, reserved names, routes ---------------------------


def test_legacy_step_may_declare_threshold_spellings_as_business_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps without any policy keep both threshold spellings as ordinary results."""
    _without_env(monkeypatch)
    workflows = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: process\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      process:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "            hard-context-threshold: {}\n"
        "            soft-context-threshold: {}\n"
        "        routes: {done: {end: completed}, hard-context-threshold: {end: unfinished}, soft-context-threshold: {end: failed}}\n"
    )
    config = _write_config(tmp_path, _minimal_config(workflows=workflows))
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None


def test_soft_shape_faults_name_the_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed `soft-tokens` and a soft-only policy name their leaf."""
    _without_env(monkeypatch)
    bad_thresholds = [
        ("context-thresholds: {hard-tokens: 40000, soft-tokens: 0}", "soft-tokens"),
        ("context-thresholds: {hard-tokens: 40000, soft-tokens: -5}", "soft-tokens"),
        ("context-thresholds: {hard-tokens: 40000, soft-tokens: 'many'}", "soft-tokens"),
        ("context-thresholds: {soft-tokens: 10000}", "hard-tokens"),
    ]
    for policy_text, leaf in bad_thresholds:
        workflows = (
            "workflows:\n"
            "  review:\n"
            "    model: sonnet\n"
            "    input: {type: issue, label: agent:review}\n"
            "    limits: {cost-usd: 6, effective-tokens: 1500000,"
            " tool-calls: 300, wall-clock-minutes: 30}\n"
            "    start: process\n"
            "    defaults:\n"
            "      max-executions: 1\n"
            "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
            "    steps:\n"
            "      process:\n"
            "        agent:\n"
            "          tools: [workspace-read]\n"
            "          results:\n"
            "            done: {}\n"
            f"        {policy_text}\n"
            "        routes: {done: {end: completed}, hard-context-threshold: {end: unfinished}, soft-context-threshold: {end: unfinished}}\n"
        )
        config = _write_config(tmp_path, _minimal_config(workflows=workflows))
        _, faults = load_config(config)
        assert faults, policy_text
        assert any(leaf in str(fault) for fault in faults), (policy_text, [str(f) for f in faults])


def test_soft_between_bounds_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A soft bound strictly between margin-plus-overhead and hard loads pinned."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None
    process = cfg.workflows["review"].steps["process"]
    assert process.context_thresholds is not None
    assert process.context_thresholds.hard_tokens == 40000
    assert process.context_thresholds.soft_tokens == 20000
    scribe = cfg.workflows["review"].steps["scribe"]
    assert scribe.context_thresholds is not None
    assert scribe.context_thresholds.hard_tokens == 40000
    assert scribe.context_thresholds.soft_tokens is None
    assert scribe.reserved is True


def test_declared_soft_on_hard_only_step_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declaring `soft-context-threshold` on a hard-only step is refused as reserved."""
    _without_env(monkeypatch)
    workflows = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: process\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      process:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "            soft-context-threshold: {}\n"
        "        context-thresholds: {hard-tokens: 40000}\n"
        "        routes: {done: handoff, hard-context-threshold: handoff, soft-context-threshold: handoff}\n"
        "      handoff:\n"
        "        operation: commit-and-push\n"
        "        routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}\n"
    )
    config = _write_config(tmp_path, _minimal_config(workflows=workflows))
    _, faults = load_config(config)
    assert faults
    assert any("reserved to the runtime" in str(fault) for fault in faults)


def test_soft_route_on_hard_only_step_is_not_a_possible_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soft Route on a hard-only step fails once, as an unknown result — never a soft fault."""
    _without_env(monkeypatch)
    workflows = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: process\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      process:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        context-thresholds: {hard-tokens: 40000}\n"
        "        routes: {done: handoff, hard-context-threshold: handoff, soft-context-threshold: handoff}\n"
        "      handoff:\n"
        "        operation: commit-and-push\n"
        "        routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}\n"
    )
    config = _write_config(tmp_path, _minimal_config(workflows=workflows))
    _, faults = load_config(config)
    assert faults
    assert len(faults) == 1, [str(f) for f in faults]
    assert "not a possible result" in str(faults[0])


# --- Per-step floors ------------------------------------------------------------


def _faults_matching(faults: list[Any], *, path: str, fragments: list[str]) -> bool:
    return any(
        getattr(fault, "path", "") == path and all(fragment in str(fault) for fragment in fragments)
        for fault in faults
    )


def _floor_config_with_process_policy(
    policy_text: str,
    *,
    ceiling: str = "limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}",
    process_routes: str = "routes: {done: {end: completed}, hard-context-threshold: {end: unfinished}, soft-context-threshold: {step: scribe, inherit-conversation: true}}",
) -> str:
    workflows = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        f"    {ceiling}\n"
        "    start: process\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      process:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        f"        {policy_text}\n"
        f"        {process_routes}\n"
        "      scribe:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            note: {}\n"
        "        reserved: true\n"
        "        limits: {cost-usd: 2, effective-tokens: 200000, tool-calls: 100, wall-clock-minutes: 10}\n"
        "        context-thresholds: {hard-tokens: 40000}\n"
        "        routes: {note: {end: completed}, hard-context-threshold: {end: unfinished}}\n"
    )
    return _minimal_config(workflows=workflows)


def test_soft_step_floor_uses_the_soft_usable_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Soft 20000 → usable 18000 → Work Pool floor 50000, cost floor 0.356.

    The receiver's hard floor (70000) already fits the ceiling, so the Work
    Pool shortfall (49999 below the process soft floor 50000) isolates the
    soft Usable Threshold: ceiling 249999 leaves pool 49999 after the
    receiver's 200000 reserve.
    """
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 249999, tool-calls: 300, wall-clock-minutes: 30}",
    )
    config = _write_config(tmp_path, text)
    _, faults = load_config(config)
    assert faults
    assert _faults_matching(
        faults,
        path="workflows.review.limits.effective-tokens",
        fragments=["maximal Admission hold", "minimum 50000", "Usable Threshold 18000"],
    ), [str(f) for f in faults]


def test_soft_step_floor_at_minimum_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ceiling 250000 covers the 200000 reserve plus the soft pool floor 50000."""
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 250000, tool-calls: 300, wall-clock-minutes: 30}",
    )
    config = _write_config(tmp_path, text)
    cfg, faults = load_config(config)
    assert not faults, [str(f) for f in faults]
    assert cfg is not None


def test_soft_subagent_step_is_raised_to_the_pin_default_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subagents soft step with a looser bound still holds the pin floor (110000)."""
    _without_env(monkeypatch)
    workflows = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:review}\n"
        "    limits: {cost-usd: 6, effective-tokens: 109999, tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: process\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      process:\n"
        "        agent:\n"
        "          tools: [workspace-read, subagents]\n"
        "          results:\n"
        "            done: {}\n"
        "        context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}\n"
        "        routes: {done: {end: completed}, hard-context-threshold: {end: unfinished}, soft-context-threshold: {step: scribe, inherit-conversation: true}}\n"
        "      scribe:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            note: {}\n"
        "        reserved: true\n"
        "        limits: {cost-usd: 2, effective-tokens: 200000, tool-calls: 100, wall-clock-minutes: 10}\n"
        "        context-thresholds: {hard-tokens: 40000}\n"
        "        routes: {note: {end: completed}, hard-context-threshold: {end: unfinished}}\n"
    )
    config = _write_config(tmp_path, _minimal_config(workflows=workflows))
    _, faults = load_config(config)
    assert faults
    assert _faults_matching(
        faults,
        path="workflows.review.limits.effective-tokens",
        fragments=["maximal Admission hold", "minimum 110000", "Usable Threshold 78000"],
    ), [str(f) for f in faults]


# --- Runtime guard: hard first, then soft ----------------------------------------


def test_opening_crossing_reports_soft_with_no_hold_or_call(workspace: Path, tmp_path: Path) -> None:
    step = _agent_step("process", hard=40000, soft=2100)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "soft-context-threshold"
    assert outcome.payload == {}
    assert model.invocations == []
    assert deps.budgets.open_hold_ids() == ()


def test_opening_equality_passes_and_report_ends_the_step(workspace: Path, tmp_path: Path) -> None:
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=900000, soft=opening + 2000)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert len(model.invocations) == 1


def test_post_tool_soft_only_crossing_records_event_and_summary(workspace: Path, tmp_path: Path) -> None:
    big = "p" * 4000
    step = _agent_step("process", hard=40000, soft=20000)
    model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "README.md"}, "big-1")], content=big, usage=_usage_dict(25000, 5000)),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "soft-context-threshold"
    assert outcome.payload == {}
    # The refused post-tool request made no provider call and took no
    # Admission hold: the guard fires before either.
    assert len(model.invocations) == 1
    assert deps.budgets.open_hold_ids() == ()
    assert outcome.limit_fact is None
    kinds = [event["kind"] for event in outcome.events]
    assert "soft_context_threshold" in kinds
    assert "hard_context_threshold" not in kinds
    event = next(event for event in outcome.events if event["kind"] == "soft_context_threshold")
    payload = event["payload"]
    assert isinstance(payload, dict)
    crossing = payload["conversation_context"]
    assert isinstance(crossing, dict)
    assert 18000 < crossing["value"] <= 38000
    assert crossing["basis"] == "anchored"
    assert payload["usable_threshold"] == 18000
    summary = outcome.summary
    assert isinstance(summary, dict)
    limit = summary.get("limit")
    assert isinstance(limit, dict)
    assert limit["scope"] == "step"
    assert limit["reason"] == "soft-context-threshold"
    assert limit["usable_threshold"] == 18000
    assert limit["conversation_context"] == crossing
    assert "would exceed the soft usable threshold" in outcome.detail


def test_request_exceeding_both_thresholds_reports_hard(workspace: Path, tmp_path: Path) -> None:
    """The hard guard is evaluated first: crossing both selects the Hard Handoff."""
    big = "p" * 4000
    step = _agent_step("process", hard=22000, soft=20000)
    model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "README.md"}, "big-1")], content=big, usage=_usage_dict(40000, 5000)),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "hard-context-threshold"
    assert len(model.invocations) == 1
    kinds = [event["kind"] for event in outcome.events]
    assert "hard_context_threshold" in kinds
    assert "soft_context_threshold" not in kinds


def test_request_at_hard_equality_reports_soft(workspace: Path, tmp_path: Path) -> None:
    """Equality passes at the hard bound, but the crossed soft bound still selects soft."""
    probe = _agent_step("process", hard=900000)
    probe_deps = make_agent_step_deps(
        model=ScriptedModel(
            [
                _ai(
                    [_call("read_file", {"path": "README.md"}, "big-1")],
                    content="p" * 4000,
                    usage=_usage_dict(40000, 5000),
                ),
                _ai([_report_call("done", {})]),
            ]
        ),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        context_window=_window(),
    )
    seen_probe: list[int] = []
    original_admit = probe_deps.budgets.admit_request

    def _probe_spy(step_id: str, estimate_tokens: int, **kwargs: Any) -> Any:
        seen_probe.append(estimate_tokens)
        return original_admit(step_id, estimate_tokens, **kwargs)

    probe_deps.budgets.admit_request = _probe_spy  # type: ignore[method-assign]
    probe_outcome = run_agent_step(probe, 1, probe_deps, {})
    assert probe_outcome.status == "reported"
    assert probe_outcome.result_name == "done"
    assert len(seen_probe) == 2
    post_tool = seen_probe[1]

    step = _agent_step("process", hard=post_tool + 2000, soft=20000)
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "big-1")],
                content="p" * 4000,
                usage=_usage_dict(40000, 5000),
            ),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "soft-context-threshold"
    assert len(model.invocations) == 1
    assert not any(event["kind"] == "hard_context_threshold" for event in outcome.events)


def _tool_then_report_script() -> list[AIMessage]:
    big = "p" * 4000
    return [
        _ai(
            [_call("read_file", {"path": "README.md"}, "big-1")],
            content=big,
            usage=_usage_dict(25000, 5000),
        ),
        _ai([_report_call("done", {})]),
    ]


def _admission_spy(deps: Any) -> list[int]:
    seen: list[int] = []
    original_admit = deps.budgets.admit_request

    def _spy(step_id: str, estimate_tokens: int, **kwargs: Any) -> Any:
        seen.append(estimate_tokens)
        return original_admit(step_id, estimate_tokens, **kwargs)

    deps.budgets.admit_request = _spy
    return seen


def test_post_tool_equality_passes_and_sends(workspace: Path, tmp_path: Path) -> None:
    """A post-tool request exactly at the Soft Usable Threshold is sent, never routed soft."""
    probe = _agent_step("process", hard=900000)
    probe_deps = make_agent_step_deps(
        model=ScriptedModel(_tool_then_report_script()),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        context_window=_window(),
    )
    seen_probe = _admission_spy(probe_deps)
    probe_outcome = run_agent_step(probe, 1, probe_deps, {})
    assert probe_outcome.status == "reported"
    assert probe_outcome.result_name == "done"
    assert len(seen_probe) == 2
    post_tool = seen_probe[1]
    assert post_tool > seen_probe[0]

    step = _agent_step("process", hard=900000, soft=post_tool + 2000)
    model = ScriptedModel(_tool_then_report_script())
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    seen = _admission_spy(deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert len(model.invocations) == 2
    assert seen == [seen_probe[0], post_tool]
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)
    assert not any(event["kind"] == "hard_context_threshold" for event in outcome.events)


def test_accepted_report_ends_the_step_with_one_invocation(workspace: Path, tmp_path: Path) -> None:
    """An accepted Step Result ends the step without another request, past both thresholds."""
    big = "p" * 4000
    step = _agent_step("process", hard=40000, soft=20000)
    model = ScriptedModel(
        [
            _ai(
                [
                    _call("read_file", {"path": "README.md"}, "big-1"),
                    _report_call("done", {}),
                ],
                content=big,
                usage=_usage_dict(40000, 5000),
            )
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    seen = _admission_spy(deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert outcome.payload == {}
    assert len(model.invocations) == 1
    assert len(seen) == 1
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


def test_one_compared_value_threads_through_admission_and_event(workspace: Path, tmp_path: Path) -> None:
    """The guard, the hold and the `model_response` event share one soft compared value."""
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=900000, soft=opening + 2000)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    seen: list[int] = []
    original_admit = deps.budgets.admit_request

    def _spy(step_id: str, estimate_tokens: int, **kwargs: Any) -> Any:
        seen.append(estimate_tokens)
        return original_admit(step_id, estimate_tokens, **kwargs)

    deps.budgets.admit_request = _spy  # type: ignore[method-assign]
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert len(seen) >= 1
    assert all(value == opening for value in seen)
    recorded = [event for event in outcome.events if event["kind"] == "model_response"]
    assert recorded
    payload = recorded[0]["payload"]
    assert isinstance(payload, dict)
    context = payload["conversation_context"]
    assert isinstance(context, dict)
    assert context["value"] == opening
    # The recorded Usable Threshold is the tighter soft bound, the guard that admits.
    assert payload["usable_threshold"] == opening


def test_retry_admissions_reuse_the_compared_value(workspace: Path, tmp_path: Path) -> None:
    from langchain_core.exceptions import ModelError as _ModelError

    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=900000, soft=opening + 2000)

    class _FlakyModel(ScriptedModel):
        def __init__(self, outcomes: list[AIMessage]) -> None:
            super().__init__(outcomes)
            self.calls = 0

        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            self.calls += 1
            if self.calls == 1:
                raise _RetryableError("boom")
            return super().invoke(input, **kwargs)

    class _RetryableError(_ModelError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.is_retryable = True

    model = _FlakyModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    seen: list[int] = []
    original_admit = deps.budgets.admit_request

    def _spy(step_id: str, estimate_tokens: int, **kwargs: Any) -> Any:
        seen.append(estimate_tokens)
        return original_admit(step_id, estimate_tokens, **kwargs)

    deps.budgets.admit_request = _spy  # type: ignore[method-assign]
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert len(seen) >= 2
    assert all(value == opening for value in seen)


# --- Separation ------------------------------------------------------------------


def test_admission_refusal_stays_limit_exceeded(workspace: Path, tmp_path: Path) -> None:
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=900000, soft=opening + 200000)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(cost_usd=0.000001, effective_tokens=1, tool_calls=10000.0, wall_clock_minutes=600.0),
        start="process",
        steps={"process": step},
    )
    budgets = build_budgets(workflow, prices=None, output_cap=32000, attempt_id="implement#7/1")
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        budgets=budgets,
        context_window=_window(),
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact.get("moment") == "before-request"
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


def test_provider_overflow_stays_error(workspace: Path, tmp_path: Path) -> None:
    from langchain_core.exceptions import ContextOverflowError as _Overflow

    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=900000, soft=opening + 200000)

    class _OverflowModel(ScriptedModel):
        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            raise _Overflow("context overflow")

    model = _OverflowModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


# --- Receiver: the inherited request against its own allowance --------------------


def _receiver(
    step_id: str = "scribe",
    *,
    hard: int = 40000,
    results: dict[str, dict[str, FieldType]] | None = None,
    routes: dict[str, RouteTarget] | None = None,
    limits: dict[str, float] | None = None,
) -> WorkflowStep:
    """A Reserved hard-only receiver with all four Step Allowance meters."""
    targets = (
        dict(routes)
        if routes is not None
        else {
            "note": RouteTarget(end="completed"),
            "hard-context-threshold": RouteTarget(end="unfinished"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        }
    )
    return _agent_step(
        step_id,
        hard=hard,
        results=dict(results) if results is not None else {"note": {}},
        routes=targets,
        reserved=True,
        limits=(
            dict(limits)
            if limits is not None
            else {
                "cost-usd": 2.0,
                "effective-tokens": 200000.0,
                "tool-calls": 100.0,
                "wall-clock-minutes": 10.0,
            }
        ),
    )


def _inherited(
    units: list[ExchangeUnit],
    tail: tuple[BaseMessage, ...] = (),
    *,
    source_result: str = "soft-context-threshold",
) -> InheritedConversation:
    return InheritedConversation(
        source_step="process",
        source_seq=1,
        source_result=source_result,
        opening_tail=tail,
        units=tuple(units),
    )


def _unit(text: str = "prior work") -> ExchangeUnit:
    return ExchangeUnit(_ai(content=text), ())


def test_receiver_guard_crossing_reports_own_hard_without_call(
    workspace: Path, tmp_path: Path
) -> None:
    """An inherited opening over the receiver's own hard bound reports hard, unsent."""
    receiver = _receiver(hard=2100)
    model = ScriptedModel([_ai([_report_call("note", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "reported"
    assert outcome.result_name == "hard-context-threshold"
    assert model.invocations == []
    assert deps.budgets.open_hold_ids() == ()
    kinds = [event["kind"] for event in outcome.events]
    assert "hard_context_threshold" in kinds
    assert "soft_context_threshold" not in kinds


def test_receiver_empty_history_records_error_without_a_model_call(
    workspace: Path, tmp_path: Path
) -> None:
    """No eligible unit means a redacted `error` before any receiver request."""
    receiver = _receiver()
    model = ScriptedModel([_ai([_report_call("note", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([]))
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert model.invocations == []
    assert "process#1" in outcome.detail
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


def test_receiver_admission_refusal_stays_limit_exceeded(
    workspace: Path, tmp_path: Path
) -> None:
    """A receiver the Attempt Ceiling cannot admit ends `limit-exceeded`."""
    receiver = _receiver()
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(cost_usd=0.000001, effective_tokens=1, tool_calls=10000.0, wall_clock_minutes=600.0),
        start="scribe",
        steps={"scribe": receiver},
    )
    budgets = build_budgets(workflow, prices=None, output_cap=32000, attempt_id="implement#7/1")
    model = ScriptedModel([_ai([_report_call("note", {})])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        budgets=budgets,
        context_window=_window(),
    )
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact.get("moment") == "before-request"
    assert model.invocations == []
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


def test_receiver_provider_overflow_stays_error(workspace: Path, tmp_path: Path) -> None:
    """Provider Context Overflow on the receiver ends `error`, never soft."""
    from langchain_core.exceptions import ContextOverflowError as _Overflow

    receiver = _receiver()

    class _OverflowModel(ScriptedModel):
        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            raise _Overflow("context overflow")

    model = _OverflowModel([_ai([_report_call("note", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


def test_receiver_invalid_reports_end_error(workspace: Path, tmp_path: Path) -> None:
    """Bounded report correction applies on the receiver, then `error`."""
    receiver = _receiver()
    model = ScriptedModel([_ai([_report_call("bogus", {})]) for _ in range(MAX_REPORT_CORRECTIONS + 1)])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert outcome.payload is None
    assert outcome.corrections == MAX_REPORT_CORRECTIONS
    assert len(model.invocations) == MAX_REPORT_CORRECTIONS + 1


def test_receiver_retry_admissions_reuse_the_compared_value(
    workspace: Path, tmp_path: Path
) -> None:
    """A retryable receiver failure re-admits the same inherited compared value."""
    from langchain_core.exceptions import ModelError as _ModelError

    receiver = _receiver()

    class _RetryableError(_ModelError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.is_retryable = True

    class _FlakyModel(ScriptedModel):
        def __init__(self, outcomes: list[AIMessage]) -> None:
            super().__init__(outcomes)
            self.calls = 0

        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            self.calls += 1
            if self.calls == 1:
                raise _RetryableError("boom")
            return super().invoke(input, **kwargs)

    model = _FlakyModel([_ai([_report_call("note", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    seen: list[int] = []
    original_admit = deps.budgets.admit_request

    def _spy(step_id: str, estimate_tokens: int, **kwargs: Any) -> Any:
        seen.append(estimate_tokens)
        return original_admit(step_id, estimate_tokens, **kwargs)

    deps.budgets.admit_request = _spy  # type: ignore[method-assign]
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "reported"
    assert outcome.result_name == "note"
    assert len(seen) >= 2
    assert all(value == seen[0] for value in seen)


def test_receiver_after_deadline_makes_no_model_call(
    workspace: Path, tmp_path: Path
) -> None:
    """Past the shared deadline the receiver ends without a model request."""

    class _ManualClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    receiver = _receiver()
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0, effective_tokens=100000.0, tool_calls=10000.0, wall_clock_minutes=1.0
        ),
        start="scribe",
        steps={"scribe": receiver},
    )
    clock = _ManualClock()
    budgets = build_budgets(
        workflow, prices=None, output_cap=32000, attempt_id="implement#7/1", clock=clock
    )
    model = ScriptedModel([_ai([_report_call("note", {})])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        budgets=budgets,
        context_window=_window(),
    )
    clock.now += 120.0
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "limit-exceeded"
    assert model.invocations == []
    assert not any(event["kind"] == "soft_context_threshold" for event in outcome.events)


def test_receiver_reserve_admission_after_work_pool_exhaustion(
    workspace: Path, tmp_path: Path
) -> None:
    """The receiver draws its reserve when the Work Pool holds one token.

    The ceiling exceeds the 200000 reserve by a single token, so the pool
    cannot admit any real inherited opening — yet the Reserved receiver is
    admitted from its own allowance, keeps the shared deadline and the full
    output cap, and charges its own execution.
    """

    class _ManualClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    class _CapSpyModel(ScriptedModel):
        def __init__(self, outcomes: list[AIMessage]) -> None:
            super().__init__(outcomes)
            self.caps: list[Any] = []

        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            self.caps.append(kwargs.get("max_tokens"))
            return super().invoke(input, **kwargs)

    receiver = _receiver(results={"note": {"summary": "document"}})
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=6.0, effective_tokens=200001.0, tool_calls=300.0, wall_clock_minutes=30.0
        ),
        start="scribe",
        steps={"scribe": receiver},
    )
    clock = _ManualClock()
    budgets = build_budgets(
        workflow, prices=None, output_cap=32000, attempt_id="implement#7/1", clock=clock
    )
    model = _CapSpyModel([_ai([_report_call("note", {"summary": "carried on"})])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        budgets=budgets,
        output_cap=32000,
        context_window=_window(),
    )
    clock.now += 60.0
    outcome = run_agent_step(receiver, 1, deps, {}, _inherited([_unit()]))
    assert outcome.status == "reported"
    assert outcome.result_name == "note"
    assert outcome.payload == {"summary": "carried on"}
    assert len(model.invocations) == 1
    assert model.caps == [32000]
    assert outcome.usage != {}
    assert deps.budgets.open_hold_ids() == ()


# --- Graph: soft routes to the receiver, failures stay model-free ------------------


def _soft_graph_workflow(
    process_routes: dict[str, RouteTarget],
    scribe_routes: dict[str, RouteTarget],
    extra_steps: dict[str, WorkflowStep] | None = None,
) -> Workflow:
    process = _agent_step(
        "process",
        hard=40000,
        soft=20000,
        results={"done": {}},
        routes=process_routes,
    )
    steps: dict[str, WorkflowStep] = {"process": process, "scribe": _receiver(routes=scribe_routes)}
    if extra_steps:
        steps.update(extra_steps)
    # The ceiling clears the scribe's 200000 reserve with room for the
    # process Work Pool: entry checks refuse steps where the pool is empty.
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0, effective_tokens=1500000.0, tool_calls=10000.0, wall_clock_minutes=600.0
        ),
        start="process",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps=steps,
    )


def _soft_process_routes(scribe_step: str = "scribe") -> dict[str, RouteTarget]:
    return {
        "done": RouteTarget(end="completed"),
        "hard-context-threshold": RouteTarget(end="unfinished"),
        "soft-context-threshold": RouteTarget(step=scribe_step, inherit_conversation=True),
    }


def test_soft_route_to_receiver_reaches_note(ledger: RunLedger) -> None:
    """The soft Route carries the source history into the receiver's note."""
    workflow = _soft_graph_workflow(
        _soft_process_routes(),
        {"note": RouteTarget(end="completed")},
    )
    executor = ScriptedStepExecutor({"process": ["soft-context-threshold"], "scribe": ["note"]})
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "completed"
    assert result.step_result == "note"
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("implement#7/1")]
    assert executions == [("process", "soft-context-threshold"), ("scribe", "note")]
    assert executor.seen_inherited[0] is None
    received = executor.seen_inherited[1]
    assert received is not None
    assert received.source_step == "process"
    assert received.source_result == "soft-context-threshold"


def test_source_hard_crossing_takes_the_hard_route(ledger: RunLedger) -> None:
    """A hard crossing never enters the Reserved receiver."""
    workflow = _soft_graph_workflow(
        {
            "done": RouteTarget(end="completed"),
            "hard-context-threshold": RouteTarget(end="unfinished"),
            "soft-context-threshold": RouteTarget(step="scribe", inherit_conversation=True),
        },
        {"note": RouteTarget(end="completed")},
    )
    executor = ScriptedStepExecutor({"process": ["hard-context-threshold"]})
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "unfinished"
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("implement#7/1")]
    assert executions == [("process", "hard-context-threshold")]
    assert len(executor.seen_inherited) == 1


def test_receiver_error_route_reaches_failed(ledger: RunLedger) -> None:
    """The receiver's effective `error` Route ends the Attempt model-free."""
    workflow = _soft_graph_workflow(
        _soft_process_routes(),
        {"note": RouteTarget(end="completed")},
    )
    executor = ScriptedStepExecutor({"process": ["soft-context-threshold"], "scribe": ["error"]})
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "failed"
    assert result.ending_step == "scribe"
    assert result.step_result == "error"
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("implement#7/1")]
    assert executions == [("process", "soft-context-threshold"), ("scribe", "error")]


def test_receiver_limit_route_reaches_unfinished(ledger: RunLedger) -> None:
    """The receiver's effective `limit-exceeded` Route ends the Attempt model-free."""
    workflow = _soft_graph_workflow(
        _soft_process_routes(),
        {"note": RouteTarget(end="completed")},
    )
    executor = ScriptedStepExecutor({"process": ["soft-context-threshold"], "scribe": ["limit-exceeded"]})
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "unfinished"
    assert result.ending_step == "scribe"
    assert result.step_result == "limit-exceeded"


def test_receiver_error_through_operations_reaches_unfinished(ledger: RunLedger) -> None:
    """A receiver `error` Route may run operations before the model-free end."""
    workflow = _soft_graph_workflow(
        _soft_process_routes(),
        {"note": RouteTarget(end="completed"), "error": RouteTarget(step="guard-one")},
        {
            "guard-one": WorkflowStep(
                id="guard-one",
                operation="commit-and-push",
                routes={
                    "pushed": RouteTarget(end="unfinished"),
                    "no-changes": RouteTarget(end="unfinished"),
                    "limit-exceeded": RouteTarget(end="unfinished"),
                    "error": RouteTarget(end="failed"),
                },
            )
        },
    )
    executor = ScriptedStepExecutor(
        {"process": ["soft-context-threshold"], "scribe": ["error"], "guard-one": ["pushed"]}
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=executor,
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "unfinished"
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("implement#7/1")]
    assert executions == [
        ("process", "soft-context-threshold"),
        ("scribe", "error"),
        ("guard-one", "pushed"),
    ]


# --- Production path: YAML to the receiver's note ------------------------------------


def test_production_path_from_yaml_to_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path, ledger: RunLedger
) -> None:
    """YAML loads, validates offline and runs to the receiver's declared note.

    The source's post-tool request crosses only the soft bound with one
    provider call; the Reserved receiver writes its note from the inherited
    history under its own Step Execution.
    """
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None
    workflow = cfg.workflows["review"]
    process = workflow.steps["process"]
    assert process.context_thresholds is not None
    assert process.context_thresholds.hard_tokens == 40000
    assert process.context_thresholds.soft_tokens == 20000
    scribe = workflow.steps["scribe"]
    assert scribe.reserved is True
    assert scribe.context_thresholds is not None
    assert scribe.context_thresholds.hard_tokens == 40000
    assert scribe.context_thresholds.soft_tokens is None
    decl = cfg.models["sonnet"]
    pin = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
        output_cap_tokens=decl.output_cap_tokens,
        context_threshold_tokens=decl.context_threshold_tokens,
        context_window_tokens=decl.context_window_tokens,
    )
    policy = policy_for_pin(pin)
    assert policy.context_window is not None
    big = "p" * 4000
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "big-1")],
                content=big,
                usage=_usage_dict(25000, 5000),
            ),
            _ai([_report_call("note", {"summary": "soft handoff note"})]),
        ]
    )
    budgets = build_budgets(workflow, attempt_id="review#1/1")
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(attempt_id="review#1/1", workflow_id="review", input_number=1),
        pin=pin,
        context_window=policy.context_window,
        ledger=ledger,
        budgets=budgets,
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="review#1/1",
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=budgets,
    )
    assert result.outcome == "completed"
    assert result.step_result == "note"
    assert len(model.invocations) == 2
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("review#1/1")]
    assert executions == [("process", "soft-context-threshold"), ("scribe", "note")]
    kinds = [record.kind for record in ledger.records("review#1/1")]
    assert "soft_context_threshold" in kinds
    assert "hard_context_threshold" not in kinds
    scribe_record = ledger.list_step_executions("review#1/1")[1]
    assert scribe_record.step_id == "scribe"
    assert scribe_record.result == "note"
    assert cli.main(["config", "validate", "--config", str(config)]) == 0


def test_production_receiver_failure_records_distinct_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path, ledger: RunLedger
) -> None:
    """A failing receiver leaves source-crossing and receiver-failure evidence apart.

    The source crosses soft once; the receiver exhausts bounded correction
    and ends `error`, which routes model-free to `failed` with no further
    model requests after the receiver fails.
    """
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None
    workflow = cfg.workflows["review"]
    decl = cfg.models["sonnet"]
    pin = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
        output_cap_tokens=decl.output_cap_tokens,
        context_threshold_tokens=decl.context_threshold_tokens,
        context_window_tokens=decl.context_window_tokens,
    )
    policy = policy_for_pin(pin)
    assert policy.context_window is not None
    big = "p" * 4000
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "big-1")],
                content=big,
                usage=_usage_dict(25000, 5000),
            ),
            *[_ai([_report_call("bogus", {})]) for _ in range(MAX_REPORT_CORRECTIONS + 1)],
        ]
    )
    budgets = build_budgets(workflow, attempt_id="review#1/1")
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(attempt_id="review#1/1", workflow_id="review", input_number=1),
        pin=pin,
        context_window=policy.context_window,
        ledger=ledger,
        budgets=budgets,
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="review#1/1",
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=budgets,
    )
    assert result.outcome == "failed"
    assert result.ending_step == "scribe"
    assert result.step_result == "error"
    assert len(model.invocations) == 1 + MAX_REPORT_CORRECTIONS + 1
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("review#1/1")]
    assert executions == [("process", "soft-context-threshold"), ("scribe", "error")]
    kinds = [record.kind for record in ledger.records("review#1/1")]
    assert "soft_context_threshold" in kinds


def test_deadline_expired_receiver_makes_no_further_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path, ledger: RunLedger
) -> None:
    """After the Attempt deadline the receiver fails model-free with one source call."""

    class _ManualClock:
        def __init__(self, start: float = 1000.0) -> None:
            self.now = start

        def __call__(self) -> float:
            return self.now

    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None
    workflow = cfg.workflows["review"]
    decl = cfg.models["sonnet"]
    pin = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
        output_cap_tokens=decl.output_cap_tokens,
        context_threshold_tokens=decl.context_threshold_tokens,
        context_window_tokens=decl.context_window_tokens,
    )
    policy = policy_for_pin(pin)
    assert policy.context_window is not None
    big = "p" * 4000
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "big-1")],
                content=big,
                usage=_usage_dict(25000, 5000),
            ),
            _ai([_report_call("note", {"summary": "too late"})]),
        ]
    )
    clock = _ManualClock()
    budgets = build_budgets(workflow, attempt_id="review#1/1", clock=clock)
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(attempt_id="review#1/1", workflow_id="review", input_number=1),
        pin=pin,
        context_window=policy.context_window,
        ledger=ledger,
        budgets=budgets,
    )

    def _expire_before_receiver(phase: str) -> None:
        if phase == "scribe#1":
            clock.now += 3600.0

    result = run_workflow(
        workflow=workflow,
        attempt_id="review#1/1",
        ledger=ledger,
        step_executor=ProductionStepExecutor(agent_deps=deps),
        budgets=budgets,
        phase_hook=_expire_before_receiver,
    )
    assert result.outcome == "unfinished"
    assert len(model.invocations) == 1
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("review#1/1")]
    assert executions[0] == ("process", "soft-context-threshold")
    assert deps.budgets.open_hold_ids() == ()


# --- Effective Configuration ---------------------------------------------------------


def _workflow_with_soft(hard: int, soft: int) -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5),
        outcomes={"completed": ("done",)},
        start="work",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(tools=("workspace-read",), results={"done": {}}),
                max_executions=1,
                context_thresholds=ContextThresholds(hard_tokens=hard, soft_tokens=soft),
                routes={
                    "done": RouteTarget(end="completed"),
                    "hard-context-threshold": RouteTarget(end="unfinished"),
                    "soft-context-threshold": RouteTarget(end="unfinished"),
                },
            ),
        },
    )


def test_soft_policy_record_shape_and_digest_sensitivity() -> None:
    """The effective record pins both bounds; either bound moves the digest."""
    first = full_effective_configuration(_workflow_with_soft(40000, 20000))
    assert is_full_effective_configuration(first) is True
    workflow = first["workflow"]
    assert isinstance(workflow, dict)
    steps = workflow.get("steps")
    assert isinstance(steps, dict)
    assert steps["work"]["context_thresholds"] == {"hard_tokens": 40000, "soft_tokens": 20000}
    for other in (
        _workflow_with_soft(40001, 20000),
        _workflow_with_soft(40000, 20001),
    ):
        record = full_effective_configuration(other)
        assert digest_effective_configuration(first) != digest_effective_configuration(record)


def test_hard_only_record_omits_soft_tokens() -> None:
    """A hard-only policy keeps the legacy record shape without `soft_tokens`."""
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5),
        outcomes={"completed": ("done",)},
        start="work",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(tools=("workspace-read",), results={"done": {}}),
                max_executions=1,
                context_thresholds=ContextThresholds(hard_tokens=40000),
                routes={
                    "done": RouteTarget(end="completed"),
                    "hard-context-threshold": RouteTarget(end="unfinished"),
                },
            ),
        },
    )
    record = full_effective_configuration(workflow)
    assert is_full_effective_configuration(record) is True
    steps = record["workflow"]
    assert isinstance(steps, dict)
    inner = steps.get("steps")
    assert isinstance(inner, dict)
    assert inner["work"]["context_thresholds"] == {"hard_tokens": 40000}
