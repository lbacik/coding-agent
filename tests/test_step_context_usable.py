"""Usable-threshold context decisions (issue #551).

Contract ``docs/contract/step-workflow-runtime-contract.md`` §9.8 / §9.9
and ADR 0013: the main loop's Context-Window Handoff trigger and the
Pinned Prefix refusal compare the next-request estimate against
``ContextWindowConfig.usable_threshold`` — never the raw
``threshold_tokens`` — and every Sub-agent spawned from an agent step
carries the same config, ending ``no-report`` / ``context-window``
before a request over its usable threshold. The published figures keep
reporting the configured ``threshold_tokens``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.context_handoff import (
    ContextWindowConfig,
    ContinuationSnapshot,
    estimate_context_tokens,
)
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    StepAttemptFacts,
    build_agent_step_tools,
    compose_step_opening,
    run_agent_step,
)
from coding_agent.workflow.schema import (
    Agent,
    RouteTarget,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps
from support.scripted_model import ScriptedModel

GATEWAY_MODEL = "group-a"
GATEWAY_HEADERS = {
    "x-litellm-model-group": GATEWAY_MODEL,
    "x-litellm-attempted-fallbacks": "0",
}
BIG = "p" * 4000


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
    step_id: str, *, tools: tuple[str, ...] = ("workspace-read",)
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results={"done": {}}),
        max_executions=1,
        routes={"done": RouteTarget(end="completed")},
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    calls: list[dict[str, object]] | None = None, content: str = ""
) -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls or []))


def _gateway_ai(
    calls: list[dict[str, object]] | None = None, content: str = ""
) -> AIMessage:
    """One scripted gateway turn: identity headers naming the pinned group."""
    return AIMessage(
        content=content,
        tool_calls=list(calls or []),
        response_metadata={"headers": dict(GATEWAY_HEADERS)},
    )


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
        call_id,
    )


def _handoff_snapshot(attempt_id: str = "implement#7/1") -> ContinuationSnapshot:
    return ContinuationSnapshot(
        version=1,
        attempt_id=attempt_id,
        base_revision="abc123",
        branch="agent/implement/7/1-x",
        commit="abc123",
        changed_files=("x.py",),
        commands=({"command": "true", "result": "ok"},),
        validation_evidence=None,
        unresolved_questions=(),
        next_intended_action="continue",
    )


def _gateway_pin(threshold: int) -> PinnedModel:
    """The Attempt's gateway pin carrying ``threshold`` as its own policy."""
    return PinnedModel(
        provider="litellm",
        model=GATEWAY_MODEL,
        endpoint="responses",
        effort="medium",
        gateway_url="http://gateway.test:4000",
        output_cap_tokens=32000,
        context_threshold_tokens=threshold,
    )


def _opening_estimate(step: WorkflowStep, workspace: Path, tmp_path: Path) -> int:
    """The next-request estimate of the step's fresh opening (probe deps)."""
    probe = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
    )
    tools = build_agent_step_tools(step, probe)
    opening = compose_step_opening(step, probe, {})
    return estimate_context_tokens(list(opening), [], list(tools))


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


def test_handoff_fires_between_usable_and_raw_threshold(
    workspace: Path, tmp_path: Path
) -> None:
    """§9.9: the trigger fires while the estimate still fits the raw threshold.

    The window is the production object, not a hand-set int: ``startup``
    stores ``policy_for_pin(pin).context_window`` on ``Ready`` and the
    attempt run passes it through as ``AgentStepDeps.context_window`` (both
    same-type pass-throughs, so a raw-vs-usable mix-up there would be a
    type error). The test feeds that same object into the deps — calibrated
    so the handoff estimate lands in ``(usable_threshold,
    threshold_tokens]``. Against the raw threshold the old code would have
    sent the request; the usable decision hands off.
    """
    step = _agent_step("process")
    usable = _opening_estimate(step, workspace, tmp_path) + 400
    pin = _gateway_pin(usable + 2_000)
    window = policy_for_pin(pin).context_window
    assert window is not None
    assert window.threshold_tokens == usable + 2_000
    assert window.usable_threshold == usable
    model = ScriptedModel(
        [
            _gateway_ai([_call("read_file", {"path": "README.md"}, "big-1")], BIG),
            _gateway_ai([_report_call("done", {})]),
        ]
    )
    seen: list[list[BaseMessage]] = []

    def producer(messages: list[BaseMessage]) -> ContinuationSnapshot:
        seen.append(list(messages))
        return _handoff_snapshot()

    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        pin=pin,
        is_gateway=True,
        context_window=window,
        handoff_producer=producer,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    assert len(model.invocations) == 2
    assert seen
    estimate = estimate_context_tokens(
        seen[0], [], list(build_agent_step_tools(step, deps))
    )
    assert window.usable_threshold < estimate <= window.threshold_tokens


def test_prefix_refusal_fires_between_usable_and_raw_threshold(
    workspace: Path, tmp_path: Path
) -> None:
    """§9.9: the prefix refusal fires though the opening fits the threshold.

    Same Worker wiring as the handoff test, calibrated so the opening
    estimate lands in ``(usable_threshold, threshold_tokens]``: no model
    call is sent, and the refusal names the usable context.
    """
    step = _agent_step("s")
    opening = _opening_estimate(step, workspace, tmp_path)
    threshold = max(opening + 1_600, 2_101)
    pin = _gateway_pin(threshold)
    window = policy_for_pin(pin).context_window
    assert window is not None
    assert window.usable_threshold < opening <= window.threshold_tokens
    model = ScriptedModel([_gateway_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        pin=pin,
        is_gateway=True,
        context_window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert "usable" in outcome.detail
    assert model.invocations == []


def test_subagent_ends_context_window_before_over_threshold_request(
    workspace: Path, tmp_path: Path
) -> None:
    """§9.8: the spawned Sub-agent stops before its over-threshold request.

    The spawn goes through the agent step's default Sub-agent path (no
    ``subagent_runner``), so the step's ``ContextWindowConfig`` reaches
    ``run_subagent``. The Sub-agent's first request fits; its second would
    exceed the usable threshold, so it ends ``no-report`` / ``context-window``
    with no provider request sent for that turn — while the step reports.
    """
    (workspace / "big.txt").write_text("0123456789abcdef\n" * 1250, encoding="utf-8")
    step = _agent_step("s", tools=("workspace-read", "subagents"))
    usable = _opening_estimate(step, workspace, tmp_path) + 400
    window = ContextWindowConfig(
        threshold_tokens=usable + 2_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
    )
    assert window.usable_threshold == usable
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {
                            "description": "check diff",
                            "instructions": "read big.txt twice, fully",
                        },
                        "s1",
                    )
                ],
                content="spawning",
            ),
            _ai(
                [
                    _call("read_file", {"path": "big.txt"}, "r1"),
                    _call("read_file", {"path": "big.txt"}, "r2"),
                ]
            ),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        context_window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 0
    assert len(model.invocations) == 3
    second_request = " ".join(
        str(message.content) for message in model.invocations[2]
    )
    assert "context-window" in second_request
    ended = [event for event in outcome.events if event["kind"] == "subagent_ended"]
    assert len(ended) == 1
    ended_payload = ended[0]["payload"]
    assert isinstance(ended_payload, dict)
    assert ended_payload["reported"] is False
    assert deps.archive_dir is not None
    records = [
        json.loads(line)
        for line in (deps.archive_dir / "transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    subagent_requests = [
        record
        for record in records
        if record.get("kind") == "request"
        and record.get("conversation") == "subagent:s1"
    ]
    assert len(subagent_requests) == 1


def test_status_snapshot_reports_the_configured_threshold(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The ``context_window`` figure keeps reporting ``threshold_tokens``.

    The deps carry a ``ContextWindowConfig`` whose usable threshold is 2 000
    below the configured value; the Effective Configuration and the Status
    Snapshot still publish the configured value, never the usable one.
    """
    from datetime import datetime, timezone

    from coding_agent.operator import listener as operator_listener
    from coding_agent.operator.live import LiveView

    threshold = 50_000
    window = ContextWindowConfig(
        threshold_tokens=threshold,
        safety_margin_tokens=1_000,
        request_overhead_tokens=1_000,
    )
    assert window.usable_threshold == threshold - 2_000
    ledger.open_attempt(
        "implement#7/1",
        effective_configuration={
            "pinned_model": {
                "key": "anthropic:claude-test",
                "provider": "anthropic",
                "context_threshold_tokens": threshold,
                "context_window": {
                    "threshold_tokens": threshold,
                    "safety_margin_tokens": 1000,
                    "request_overhead_tokens": 1000,
                    "handoff_token_cap": 8000,
                },
            }
        },
    )
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        context_window=window,
    )
    outcome = run_agent_step(_agent_step("process"), 1, deps, {})
    assert outcome.status == "reported"
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    live.set_claim("implement#7/1", 7, "Fix the crash")
    live.set_phase("process#1")
    current = operator_listener.build_status_result(ledger.path, live)[
        "current_attempt"
    ]
    assert isinstance(current, dict)
    figures = current["figures"]
    assert isinstance(figures, dict)
    assert figures["context_window"]["value"] == threshold
