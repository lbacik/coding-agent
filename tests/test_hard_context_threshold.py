"""A per-step Hard Context Threshold selecting the Hard Handoff (issue #606).

Contract ``docs/contract/step-workflow-runtime-contract.md`` §§3, 9.9, 11.4:
an agent step may carry its own ``context-thresholds`` policy (required
``hard-tokens``, optional ``soft-tokens`` below it). The next
main-conversation request that would cross the policy's Hard Usable
Threshold is never sent — no Admission hold, no provider call — and the
Step Execution reports ``hard-context-threshold`` for the Workflow's
model-free Hard Handoff. Steps that omit the policy keep the Pinned
Model's Context Threshold guard and existing outcomes.

Soft crossings below hard are ignored by the runtime (no soft report)
until #608: an opted-in step with a soft bound still reports hard on a
hard crossing and otherwise runs normally. The Hard Threshold replaces
(rather than augments) the pin guard on opted-in steps.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent import cli
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    StepAttemptFacts,
    build_agent_step_tools,
    compose_step_opening,
    run_agent_step,
)
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.runtime.step_limits import AttemptBudgets, build_budgets
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
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
from coding_agent.workflow.effective import (
    digest_effective_configuration,
    is_full_effective_configuration,
)


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
) -> WorkflowStep:
    declared = dict(results) if results is not None else {"done": {}}
    targets = dict(routes) if routes is not None else {"done": RouteTarget(end="completed")}
    policy: ContextThresholds | None = None
    if hard is not None:
        policy = ContextThresholds(hard_tokens=hard, soft_tokens=soft)
        targets.setdefault("hard-context-threshold", RouteTarget(end="unfinished"))
        if soft is not None:
            targets.setdefault("soft-context-threshold", RouteTarget(end="unfinished"))
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results=declared),
        max_executions=1,
        context_thresholds=policy,
        routes=targets,
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
            "        context-thresholds: {hard-tokens: 40000}\n"
            "        routes: {done: handoff, hard-context-threshold: handoff}\n"
            "      handoff:\n"
            "        operation: commit-and-push\n"
            "        routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}\n"
        ),
    }
    blocks.update(overrides)
    return "".join(blocks[key] for key in ("version", "target", "models", "workflows"))


def _workflows_block(
    process_extra: str = "",
    process_routes: str = "routes: {done: handoff, hard-context-threshold: handoff}",
    handoff_routes: str = "routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}",
) -> str:
    return (
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
        f"{process_extra}\n"
        f"        {process_routes}\n"
        "      handoff:\n"
        "        operation: commit-and-push\n"
        f"        {handoff_routes}\n"
    )


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


# --- Validation: agent-only, shape, bounds, reserved names, routes --------------


def test_operation_with_thresholds_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "42-context-thresholds-on-operation.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("apply only to agent steps" in str(fault) for fault in faults)


def test_missing_hard_route_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "43-hard-threshold-missing-route.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("needs a step-local route" in str(fault) for fault in faults)


def test_hard_handoff_to_agent_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "44-hard-handoff-to-agent.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("cannot enter agent step" in str(fault) for fault in faults)


def test_hard_handoff_cycle_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "45-hard-handoff-cycle.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("form a cycle" in str(fault) for fault in faults)


def test_declared_hard_result_on_opted_in_step_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "46-hard-result-declared-on-opted-in-step.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("reserved to the runtime" in str(fault) for fault in faults)


def test_hard_below_safety_plus_overhead_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "47-hard-below-safety-plus-overhead.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("must exceed the code-owned safety" in str(fault) for fault in faults)


def test_hard_exceeding_window_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "48-hard-exceeding-window.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("must not exceed 'context-window-tokens'" in str(fault) for fault in faults)


def test_soft_policy_missing_soft_route_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "49-soft-policy-missing-soft-route.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("needs a step-local route" in str(fault) for fault in faults)


def test_hard_handoff_to_completed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "workflows" / "invalid" / "50-hard-handoff-to-completed.yaml"
    _, faults = load_config(fixture)
    assert faults
    assert any("cannot end `completed`" in str(fault) for fault in faults)


def test_legacy_step_may_declare_threshold_spellings_as_business_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps without the policy keep both spellings as ordinary results."""
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


def test_threshold_shape_faults_name_the_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    bad_thresholds = [
        ("context-thresholds: {hard-tokens: 0}", "hard-tokens"),
        ("context-thresholds: {hard-tokens: -5}", "hard-tokens"),
        ("context-thresholds: {hard-tokens: 'many'}", "hard-tokens"),
        ("context-thresholds: {hard-tokens: 40000, soft-tokens: 0}", "soft-tokens"),
        ("context-thresholds: {hard-tokens: 40000, soft-tokens: 50000}", "soft-tokens"),
        ("context-thresholds: {hard-tokens: 40000, extra: 1}", "context-thresholds"),
        ("context-thresholds: {soft-tokens: 10000}", "hard-tokens"),
    ]
    for policy_text, leaf in bad_thresholds:
        workflows = _workflows_block(
            process_extra=f"        {policy_text}",
            process_routes="routes: {done: handoff, hard-context-threshold: handoff}",
        )
        config = _write_config(tmp_path, _minimal_config(workflows=workflows))
        _, faults = load_config(config)
        assert faults, policy_text
        assert any(leaf in str(fault) for fault in faults), (policy_text, [str(f) for f in faults])


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
    process_routes: str = "routes: {done: handoff, hard-context-threshold: handoff}",
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
        "      handoff:\n"
        "        operation: commit-and-push\n"
        "        routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}\n"
    )
    return _minimal_config(workflows=workflows)


def test_hard_step_floor_is_seventy_thousand_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard 40000 → usable 38000 → token floor 70000, cost floor 0.396."""
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 69999, tool-calls: 300, wall-clock-minutes: 30}",
    )
    config = _write_config(tmp_path, text)
    _, faults = load_config(config)
    assert faults
    assert _faults_matching(
        faults,
        path="workflows.review.limits.effective-tokens",
        fragments=["maximal Admission hold", "minimum 70000", "Usable Threshold 38000"],
    ), [str(f) for f in faults]


def test_hard_step_floor_at_minimum_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 70000, tool-calls: 300, wall-clock-minutes: 30}",
    )
    config = _write_config(tmp_path, text)
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None


def test_subagent_step_is_raised_to_the_pin_default_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subagents step with a looser-or-equal hard still holds the pin floor (110000)."""
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
        "        context-thresholds: {hard-tokens: 40000}\n"
        "        routes: {done: handoff, hard-context-threshold: handoff}\n"
        "      handoff:\n"
        "        operation: commit-and-push\n"
        "        routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}\n"
    )
    config = _write_config(tmp_path, _minimal_config(workflows=workflows))
    _, faults = load_config(config)
    assert faults
    assert _faults_matching(
        faults,
        path="workflows.review.limits.effective-tokens",
        fragments=["maximal Admission hold", "minimum 110000", "Usable Threshold 78000"],
    ), [str(f) for f in faults]


def test_soft_based_floor_uses_the_soft_usable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard 40000 + soft 10000 → usable 8000 → token floor 40000."""
    _without_env(monkeypatch)
    text = _floor_config_with_process_policy(
        "context-thresholds: {hard-tokens: 40000, soft-tokens: 10000}",
        ceiling="limits: {cost-usd: 6, effective-tokens: 39999, tool-calls: 300, wall-clock-minutes: 30}",
    )
    config = _write_config(tmp_path, text)
    _, faults = load_config(config)
    assert faults
    assert _faults_matching(
        faults,
        path="workflows.review.limits.effective-tokens",
        fragments=["maximal Admission hold", "minimum 40000", "Usable Threshold 8000"],
    ), [str(f) for f in faults]


# --- Runtime guard ---------------------------------------------------------------


def test_opening_crossing_reports_hard_with_no_hold_or_call(workspace: Path, tmp_path: Path) -> None:
    step = _agent_step("process", hard=2100)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "hard-context-threshold"
    assert outcome.payload == {}
    assert model.invocations == []
    assert deps.budgets.open_hold_ids() == ()


def test_opening_equality_passes_and_report_ends_the_step(workspace: Path, tmp_path: Path) -> None:
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=opening + 2000)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert len(model.invocations) == 1


def test_loop_crossing_records_event_and_summary(workspace: Path, tmp_path: Path) -> None:
    big = "p" * 4000
    step = _agent_step("process", hard=22000)
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
    assert outcome.payload == {}
    assert len(model.invocations) == 1
    assert outcome.limit_fact is None
    kinds = [event["kind"] for event in outcome.events]
    assert "hard_context_threshold" in kinds
    event = next(event for event in outcome.events if event["kind"] == "hard_context_threshold")
    payload = event["payload"]
    assert isinstance(payload, dict)
    crossing = payload["conversation_context"]
    assert isinstance(crossing, dict)
    assert crossing["value"] > 20000
    assert crossing["basis"] == "anchored"
    assert payload["usable_threshold"] == 20000
    summary = outcome.summary
    assert isinstance(summary, dict)
    limit = summary.get("limit")
    assert isinstance(limit, dict)
    assert limit["scope"] == "step"
    assert limit["reason"] == "hard-context-threshold"
    assert limit["usable_threshold"] == 20000
    assert limit["conversation_context"] == crossing
    assert "would exceed the hard usable threshold" in outcome.detail


def test_accepted_report_ends_the_step_with_one_invocation(workspace: Path, tmp_path: Path) -> None:
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=opening + 2000)
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert outcome.payload == {}
    assert len(model.invocations) == 1


# --- Single Conversation Context value ------------------------------------------


def test_one_compared_value_threads_through_admission_and_event(workspace: Path, tmp_path: Path) -> None:
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=opening + 2000)
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


def test_retry_admissions_reuse_the_compared_value(workspace: Path, tmp_path: Path) -> None:
    from langchain_core.exceptions import ModelError as _ModelError

    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=opening + 2000)

    class _FlakyModel(ScriptedModel):
        def __init__(self, outcomes: list[AIMessage]) -> None:
            super().__init__(outcomes)
            self.calls = 0

        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> BaseMessage:
            self.calls += 1
            if self.calls == 1:
                raise _RetryableError("boom")
            return super().invoke(input, config, **kwargs)

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
    step = _agent_step("process", hard=opening + 200000)
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
    assert not any(event["kind"] == "hard_context_threshold" for event in outcome.events)


def test_provider_overflow_stays_error(workspace: Path, tmp_path: Path) -> None:
    from langchain_core.exceptions import ContextOverflowError as _Overflow

    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    step = _agent_step("process", hard=opening + 200000)

    class _OverflowModel(ScriptedModel):
        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> BaseMessage:
            raise _Overflow("context overflow")

    model = _OverflowModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=_window()
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert not any(event["kind"] == "hard_context_threshold" for event in outcome.events)


def test_subagent_keeps_the_pin_guard_when_hard_is_looser(workspace: Path, tmp_path: Path) -> None:
    (workspace / "big.txt").write_text("0123456789abcdef\n" * 1250, encoding="utf-8")
    step = _agent_step("s", hard=900000, tools=("workspace-read", "subagents"))
    usable = 40000
    window = ContextWindowConfig(threshold_tokens=usable + 2000, safety_margin_tokens=1000, request_overhead_tokens=1000)
    model = ScriptedModel(
        [
            _ai([_call("spawn_subagent", {"description": "check diff", "instructions": "read big twice"}, "s1")]),
            _ai([_call("read_file", {"path": "big.txt"}, "r1"), _call("read_file", {"path": "big.txt"}, "r2")]),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = make_agent_step_deps(model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), context_window=window)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert len(model.invocations) == 3
    ended = [event for event in outcome.events if event["kind"] == "subagent_ended"]
    assert len(ended) == 1
    payload = ended[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["reported"] is False


# --- Graph routing ---------------------------------------------------------------


def _hard_workflow(
    process_routes: dict[str, RouteTarget],
    extra_steps: dict[str, WorkflowStep] | None = None,
) -> Workflow:
    process = WorkflowStep(
        id="process",
        agent=Agent(tools=("workspace-read",), results={"done": {}}),
        max_executions=1,
        context_thresholds=ContextThresholds(hard_tokens=40000),
        routes=dict(process_routes),
    )
    steps: dict[str, WorkflowStep] = {"process": process}
    if extra_steps:
        steps.update(extra_steps)
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(cost_usd=100000.0, effective_tokens=100000.0, tool_calls=10000.0, wall_clock_minutes=600.0),
        start="process",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps=steps,
    )


def _op(step_id: str, operation: str, routes: dict[str, RouteTarget]) -> WorkflowStep:
    return WorkflowStep(id=step_id, operation=operation, routes=dict(routes))


def test_hard_route_through_operations_reaches_unfinished(ledger: RunLedger) -> None:
    workflow = _hard_workflow(
        {"done": RouteTarget(end="completed"), "hard-context-threshold": RouteTarget(step="guard-one")},
        {
            "guard-one": _op(
                "guard-one",
                "commit-and-push",
                {
                    "pushed": RouteTarget(end="unfinished"),
                    "no-changes": RouteTarget(end="unfinished"),
                    "limit-exceeded": RouteTarget(end="unfinished"),
                    "error": RouteTarget(end="failed"),
                },
            )
        },
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=ScriptedStepExecutor({"process": ["hard-context-threshold"], "guard-one": ["pushed"]}),
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "unfinished"
    executions = [(record.step_id, record.result) for record in ledger.list_step_executions("implement#7/1")]
    assert executions == [("process", "hard-context-threshold"), ("guard-one", "pushed")]


def test_hard_route_may_end_directly_at_unfinished(ledger: RunLedger) -> None:
    workflow = _hard_workflow({"done": RouteTarget(end="completed"), "hard-context-threshold": RouteTarget(end="unfinished")})
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=ScriptedStepExecutor({"process": ["hard-context-threshold"]}),
        budgets=build_budgets(workflow, attempt_id="implement#7/1"),
    )
    assert result.outcome == "unfinished"


def test_model_free_fallback_runs_after_the_wall_deadline(ledger: RunLedger) -> None:
    from support.agent_step_deps import FakeCommandRunner

    workflow = _hard_workflow(
        {"done": RouteTarget(end="completed"), "hard-context-threshold": RouteTarget(step="guard-one")},
        {
            "guard-one": _op(
                "guard-one",
                "commit-and-push",
                {
                    "pushed": RouteTarget(end="unfinished"),
                    "no-changes": RouteTarget(end="unfinished"),
                    "limit-exceeded": RouteTarget(end="unfinished"),
                    "error": RouteTarget(end="failed"),
                },
            )
        },
    )
    ceiling = AttemptCeiling(cost_usd=100000.0, effective_tokens=100000.0, tool_calls=10000.0, wall_clock_minutes=0.001)
    budgets = build_budgets(workflow, attempt_id="implement#7/1")
    budgets.clock = lambda: budgets.opened_at + 60.0
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=ScriptedStepExecutor({"process": ["hard-context-threshold"], "guard-one": ["pushed"]}),
        budgets=budgets,
    )
    assert result.outcome == "unfinished"
    _ = FakeCommandRunner()


# --- Production path ---------------------------------------------------------------


def test_production_path_from_yaml_to_hard_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    _without_env(monkeypatch)
    probe_step = _agent_step("process")
    opening = _opening_estimate(probe_step, workspace, tmp_path)
    hard = opening + 2000
    workflows = _workflows_block(
        process_extra=f"        context-thresholds: {{hard-tokens: {hard}}}",
        process_routes="routes: {done: handoff, hard-context-threshold: handoff}",
    )
    config = _write_config(tmp_path, _minimal_config(workflows=workflows))
    cfg, faults = load_config(config)
    assert not faults
    assert cfg is not None
    typed_step = cfg.workflows["review"].steps["process"]
    assert typed_step.context_thresholds is not None
    assert typed_step.context_thresholds.hard_tokens == hard
    assert typed_step.context_thresholds.soft_tokens is None
    decl = cfg.models["sonnet"]
    assert decl.context_threshold_tokens == 80000
    assert decl.context_window_tokens == 1000000
    assert decl.output_cap_tokens == 32000
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
    assert policy.output_cap == 32000
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, attempt=_facts(), pin=pin, context_window=policy.context_window
    )
    outcome = run_agent_step(typed_step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "hard-context-threshold"
    assert cli.main(["config", "validate", "--config", str(config)]) == 0


def test_config_validate_is_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)
    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


# --- Effective Configuration ---------------------------------------------------------


def _workflow_with_thresholds(hard: int, soft: int | None = None) -> Workflow:
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
                    **({"soft-context-threshold": RouteTarget(end="unfinished")} if soft is not None else {}),
                },
            ),
        },
    )


def _workflow_without_thresholds() -> Workflow:
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
                routes={"done": RouteTarget(end="completed")},
            ),
        },
    )


def test_legacy_steps_omit_the_thresholds_key() -> None:
    record = full_effective_configuration(_workflow_without_thresholds())
    assert is_full_effective_configuration(record) is True
    workflow = record["workflow"]
    assert isinstance(workflow, dict)
    steps = workflow.get("steps")
    assert isinstance(steps, dict)
    assert steps["work"].get("context_thresholds") is None


def test_policy_record_shape_and_digest_sensitivity() -> None:
    first = full_effective_configuration(_workflow_with_thresholds(40000))
    assert is_full_effective_configuration(first) is True
    workflow = first["workflow"]
    assert isinstance(workflow, dict)
    steps = workflow["steps"]
    assert isinstance(steps, dict)
    assert steps["work"]["context_thresholds"] == {"hard_tokens": 40000}
    second = full_effective_configuration(_workflow_with_thresholds(40001))
    assert digest_effective_configuration(first) != digest_effective_configuration(second)
    with_soft = full_effective_configuration(_workflow_with_thresholds(40000, 10000))
    assert is_full_effective_configuration(with_soft) is True
    soft_workflow = with_soft["workflow"]
    assert isinstance(soft_workflow, dict)
    soft_steps = soft_workflow["steps"]
    assert isinstance(soft_steps, dict)
    assert soft_steps["work"]["context_thresholds"] == {"hard_tokens": 40000, "soft_tokens": 10000}
    assert digest_effective_configuration(first) != digest_effective_configuration(with_soft)


def test_old_v4_bytes_without_steps_still_recover() -> None:
    record = full_effective_configuration(_workflow_without_thresholds())
    assert isinstance(record["workflow"], dict)
    workflow = dict(record["workflow"])
    assert isinstance(workflow, dict)
    workflow.pop("steps", None)
    legacy = dict(record)
    legacy["workflow"] = workflow
    assert is_full_effective_configuration(legacy) is True
