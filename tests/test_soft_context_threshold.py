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
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from coding_agent import cli
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.exchange import ExchangeUnit
from coding_agent.runtime.graph.agent_step import (
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
    """Soft 20000 → usable 18000 → token floor 50000, cost floor 0.356."""
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 49999, tool-calls: 300, wall-clock-minutes: 30}",
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
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 70000, tool-calls: 300, wall-clock-minutes: 30}",
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
