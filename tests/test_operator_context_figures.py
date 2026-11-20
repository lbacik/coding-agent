"""Main-conversation context figures in ``current_attempt`` (#522).

Contract: while an Attempt runs, ``current_attempt.figures`` reports the
main conversation's ``context_used`` (measured), ``context_peak``
(measured, Attempt-wide) and ``context_window`` (the pinned handoff
threshold, configured), plus ``native.context_estimate_tokens`` (the step
loop's local estimate, estimated). Sub-agent conversations and the bounded
handoff turn never move them; a handoff drops ``used`` while the peak
survives; unknown is never zero; and the values travel in the Attempt usage
flush payload, never through a Transcript read on the ``status`` path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.ledger.store import BudgetSnapshot, LedgerRecord, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.context_handoff import (
    ContextWindowConfig,
    ContinuationSnapshot,
    estimate_context_tokens,
)
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    build_agent_step_tools,
    compose_step_opening,
    run_agent_step,
)
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
    restore_budgets_from_usage,
)
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps
from support.scripted_model import ScriptedModel

ATTEMPT_ID = "implement#7/1"
THRESHOLD = 50_000


def _usage_dict(
    input_tokens: int, output_tokens: int, *, cache_read: int = 0
) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {
            "cache_read": cache_read,
            "cache_creation": 0,
        },
    }


def _effective_config(*, threshold: int | None = THRESHOLD) -> dict[str, Any]:
    config: dict[str, Any] = {
        "workflow": {
            "limits": {
                "wall_clock_minutes": 60,
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
            }
        }
    }
    if threshold is not None:
        config["pinned_model"] = {
            "key": "anthropic:claude-test",
            "provider": "anthropic",
            "context_threshold_tokens": threshold,
            "context_window": {
                "threshold_tokens": threshold,
                "safety_margin_tokens": 1000,
                "request_overhead_tokens": 500,
                "handoff_token_cap": 8000,
            },
        }
    return config


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "run-ledger.sqlite3")


def _open(
    ledger: RunLedger, attempt_id: str = ATTEMPT_ID, *, threshold: int | None = THRESHOLD
) -> None:
    ledger.open_attempt(
        attempt_id, effective_configuration=_effective_config(threshold=threshold)
    )


def _live(
    attempt_id: str = ATTEMPT_ID, issue_number: int = 7, title: str = "Fix the crash"
) -> LiveView:
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    live.set_claim(attempt_id, issue_number, title)
    live.set_phase("process#1")
    return live


def _current(ledger: RunLedger, live: LiveView) -> dict[str, Any]:
    result = operator_listener.build_status_result(ledger.path, live)
    current = result["current_attempt"]
    assert isinstance(current, dict)
    return current


def _context(
    ledger: RunLedger, attempt_id: str = ATTEMPT_ID
) -> dict[str, Any]:
    payload = ledger.get_latest_usage(attempt_id)
    assert payload is not None
    context = payload.get("context")
    assert isinstance(context, dict)
    return context


def _figures_with_context(
    tmp_path: Path,
    context: dict[str, Any] | None,
    *,
    threshold: int | None = THRESHOLD,
) -> dict[str, Any]:
    """One status snapshot over a hand-written usage flush payload."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, threshold=threshold)
        ledger.record_usage(
            ATTEMPT_ID,
            BudgetSnapshot(
                elapsed_seconds=90.0,
                usage=UsageTotals(
                    tokens=1200,
                    effective_tokens=1000,
                    cost_usd=0.02,
                    tool_calls=7,
                    input_tokens=800,
                    output_tokens=200,
                    cache_read_tokens=200,
                ),
                context=context,
            ),
        )
        current = _current(ledger, _live())
        figures: dict[str, Any] = current["figures"]
        return figures
    finally:
        ledger.close()


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content=content, tool_calls=list(calls or []), usage_metadata=usage
    )


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
        call_id,
    )


def _agent_step(
    step_id: str, *, tools: tuple[str, ...] = ("workspace-read",)
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results={"done": {}}),
        routes={"done": RouteTarget(end="completed")},
    )


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, Any] = {
        "attempt_id": ATTEMPT_ID,
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


def _handoff_snapshot(attempt_id: str = ATTEMPT_ID) -> ContinuationSnapshot:
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


def _calibrated_window(
    step: WorkflowStep, deps: AgentStepDeps, slack: int = 400
) -> ContextWindowConfig:
    """A window whose usable threshold the opening fits but the opening plus
    one padded turn exceeds. The usable value equals the old calibrated
    threshold, so the trigger point is unchanged."""
    from coding_agent.runtime.context_handoff import ContextWindowConfig

    tools = build_agent_step_tools(step, deps)
    opening = compose_step_opening(step, deps, {})
    usable = estimate_context_tokens(list(opening), [], list(tools)) + slack
    return ContextWindowConfig(
        threshold_tokens=usable + 1000 + 500,
        safety_margin_tokens=1000,
        request_overhead_tokens=500,
    )


def _main_estimate_of_last_request(
    step: WorkflowStep, deps: AgentStepDeps, model: ScriptedModel
) -> int:
    """The local estimate of the last request the step loop handed to the model."""
    tools = build_agent_step_tools(step, deps)
    assert model.invocations
    return estimate_context_tokens(list(model.invocations[-1]), [], list(tools))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = _ledger(tmp_path)
    yield handle
    handle.close()


# --- Snapshot builder over the flush payload ---------------------------------


def test_measured_context_from_flush(tmp_path: Path) -> None:
    """A flush carrying context reports measured used/peak and the estimate."""
    figures = _figures_with_context(
        tmp_path,
        {
            "used": 41230,
            "peak": 48711,
            "estimate": 39000,
            "conversation": "step/process",
        },
    )
    assert figures["context_used"] == {
        "value": 41230,
        "unit": "tokens",
        "basis": "measured",
    }
    assert figures["context_peak"] == {
        "value": 48711,
        "unit": "tokens",
        "basis": "measured",
    }
    assert figures["context_window"]["value"] == THRESHOLD
    assert figures["context_window"]["unit"] == "tokens"
    assert figures["context_window"]["basis"] == "configured"
    assert figures["context_window"]["reason"]
    assert figures["native.context_estimate_tokens"] == {
        "value": 39000,
        "unit": "tokens",
        "basis": "estimated",
        "reason": (
            "the step loop's local estimate of the main conversation's "
            "next request, the value the handoff check compares "
            "against the threshold"
        ),
    }


def test_legacy_flush_without_context_is_unavailable(tmp_path: Path) -> None:
    """A flush with no context key reads unavailable, never not_applicable."""
    figures = _figures_with_context(tmp_path, None)
    for key in ("context_used", "context_peak"):
        assert figures[key]["value"] is None
        assert figures[key]["basis"] == "unavailable"
        assert figures[key]["reason"]
    estimate = figures["native.context_estimate_tokens"]
    assert estimate["value"] is None
    assert estimate["basis"] == "unavailable"
    assert estimate["reason"]
    assert figures["turns"]["basis"] == "not_applicable"


def test_no_figure_is_not_applicable_except_turns(tmp_path: Path) -> None:
    """Only ``turns`` keeps the not_applicable basis once context is tracked."""
    figures = _figures_with_context(
        tmp_path,
        {
            "used": 100,
            "peak": 100,
            "estimate": 90,
            "conversation": "step/process",
        },
    )
    for key, value in figures.items():
        assert isinstance(value, dict)
        if key != "turns":
            assert value["basis"] != "not_applicable", key
        if value["value"] is None:
            assert value["basis"] in ("unavailable", "not_applicable"), key


def test_context_window_unavailable_without_pin(tmp_path: Path) -> None:
    """No pinned context window reads unavailable, never zero."""
    figures = _figures_with_context(
        tmp_path,
        {
            "used": 100,
            "peak": 100,
            "estimate": 90,
            "conversation": "step/process",
        },
        threshold=None,
    )
    window = figures["context_window"]
    assert window["value"] is None
    assert window["basis"] == "unavailable"


def test_every_figure_honours_the_null_value_rule(tmp_path: Path) -> None:
    """Null values pair only with null bases, across figures and native."""
    figures = _figures_with_context(
        tmp_path,
        {
            "used": 100,
            "peak": 100,
            "estimate": 90,
            "conversation": "step/process",
        },
    )
    collected: list[dict[str, Any]] = []
    for value in figures.values():
        assert isinstance(value, dict)
        collected.append(value)
    assert len(collected) >= 10
    for fig in collected:
        if fig["value"] is None:
            assert fig["basis"] in ("unavailable", "not_applicable")
        else:
            assert fig["basis"] not in ("unavailable", "not_applicable")


# --- AttemptBudgets context tracking ------------------------------------------


def _budgets(attempt_id: str = ATTEMPT_ID) -> AttemptBudgets:
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
    )
    return build_budgets(workflow, attempt_id=attempt_id)


def test_peak_tracks_max_and_survives_restore() -> None:
    """The peak is Attempt-wide and comes back from the last flush."""
    budgets = _budgets()
    budgets.note_main_context_estimate(40000, "step/process")
    budgets.note_main_context_used(41230, "step/process")
    budgets.reset_main_context("step/process/continuation/1")
    budgets.note_main_context_estimate(8000, "step/process/continuation/1")
    budgets.note_main_context_used(8100, "step/process/continuation/1")
    snapshot = budgets.main_context_snapshot()
    assert snapshot == {
        "used": 8100,
        "peak": 41230,
        "estimate": 8000,
        "conversation": "step/process/continuation/1",
        "cache_read": 0,
        "cache_input": 0,
        "responses_without_usage": 0,
    }
    payload = {
        "usage": budgets.attempt_totals().as_readable(),
        "elapsed_seconds": 1.0,
        "context": snapshot,
    }
    restarted = _budgets()
    restore_budgets_from_usage(restarted, payload)
    assert restarted.main_context_snapshot() == snapshot


def test_context_restored_without_spend_payload() -> None:
    """The peak comes back even where the flush carries no spend mapping."""
    snapshot: dict[str, Any] = {
        "used": 8100,
        "peak": 41230,
        "estimate": 8000,
        "conversation": "step/process/continuation/1",
        "cache_read": 0,
        "cache_input": 0,
        "responses_without_usage": 0,
    }
    restarted = _budgets()
    restore_budgets_from_usage(restarted, {"context": snapshot})
    assert restarted.main_context_snapshot() == snapshot


def test_unknown_used_is_unavailable_never_zero() -> None:
    """A response without usage clears used; the peak keeps its maximum."""
    budgets = _budgets()
    budgets.note_main_context_used(41230, "step/process")
    budgets.note_main_context_used(None, "step/process")
    snapshot = budgets.main_context_snapshot()
    assert snapshot["used"] is None
    assert snapshot["peak"] == 41230


# --- Step loop: main conversation only ----------------------------------------


def _run_step(
    step: WorkflowStep,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
    *,
    threshold: int | None = THRESHOLD,
    handoff_producer: Any = None,
) -> tuple[AgentStepDeps, list[dict[str, Any]]]:
    """Run one Step Execution, capturing every Attempt usage flush payload."""
    from coding_agent.runtime.context_handoff import ContextWindowConfig

    _open(ledger, threshold=threshold)
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        context_window=(
            ContextWindowConfig(
                threshold_tokens=threshold,
                safety_margin_tokens=1000,
                request_overhead_tokens=500,
            )
            if threshold is not None
            else None
        ),
        handoff_producer=handoff_producer,
    )
    captured: list[dict[str, Any]] = []
    real_record_usage = ledger.record_usage

    def _capture(
        attempt_id: str, snapshot: BudgetSnapshot, *, detail: str | None = None
    ) -> LedgerRecord:
        captured.append(
            {
                "usage": snapshot.usage.as_readable(),
                "context": dict(snapshot.context or {}),
            }
        )
        return real_record_usage(attempt_id, snapshot, detail=detail)

    monkeypatch.setattr(ledger, "record_usage", _capture)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    return deps, captured


def test_main_response_sets_used_peak_and_estimate(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last settled main response sizes the conversation (measured)."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(3000, 500),
            )
        ]
    )
    deps, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    context = _context(ledger)
    assert context["used"] == 3500
    assert context["peak"] == 3500
    assert context["conversation"] == "step/process/1"
    assert context["estimate"] == _main_estimate_of_last_request(step, deps, model)
    figures = _current(ledger, _live())["figures"]
    assert figures["context_used"] == {
        "value": 3500,
        "unit": "tokens",
        "basis": "measured",
    }
    assert figures["context_peak"]["value"] == 3500
    assert figures["context_peak"]["basis"] == "measured"
    native = figures["native.context_estimate_tokens"]
    assert native["basis"] == "estimated"
    assert native["value"] == context["estimate"]


def test_subagent_response_leaves_context_unchanged(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Sub-agent turn never overwrites the main conversation's figures."""
    step = _agent_step("process", tools=("workspace-read", "subagents"))
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {
                            "description": "look",
                            "instructions": "read the workspace files",
                        },
                        "s1",
                    )
                ],
                usage=_usage_dict(3000, 500),
            ),
            _ai(
                [
                    _call(
                        "report_subagent",
                        {"report": "nothing to report"},
                        "sub-1",
                    )
                ],
                usage=_usage_dict(9000, 1000),
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(3500, 200),
            ),
        ]
    )
    _, captured = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    assert captured, "the run must flush Attempt usage"
    for payload in captured:
        assert payload["context"]["used"] != 10000
        assert payload["context"]["conversation"] == "step/process/1"
    assert captured[-1]["context"]["used"] == 3700
    assert captured[-1]["context"]["peak"] == 3700
    figures = _current(ledger, _live())["figures"]
    assert figures["context_used"]["value"] == 3700
    assert figures["context_peak"]["value"] == 3700


def test_handoff_drops_used_and_keeps_peak(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a handoff, used follows the continuation while the peak survives."""
    big = "p" * 4000
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "big-1")],
                content=big,
                usage=_usage_dict(40000, 5000),
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(3000, 200),
            ),
        ]
    )
    probe = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
    )
    window = _calibrated_window(step, probe)

    def producer(messages: list[BaseMessage]) -> ContinuationSnapshot:
        return _handoff_snapshot()

    _open(ledger, threshold=window.threshold_tokens)
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        context_window=window,
        handoff_producer=producer,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    context = _context(ledger)
    assert context["used"] == 3200
    assert context["peak"] == 45000
    assert context["conversation"] == "step/process/1/continuation/1"
    figures = _current(ledger, _live())["figures"]
    assert figures["context_used"] == {
        "value": 3200,
        "unit": "tokens",
        "basis": "measured",
    }
    assert figures["context_peak"]["value"] == 45000
    assert figures["context_window"]["value"] == window.threshold_tokens
    assert figures["context_window"]["basis"] == "configured"


def test_missing_usage_makes_used_unavailable(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main response without usage clears used: unavailable, never estimated."""
    step = _agent_step("process")
    model = ScriptedModel([_ai([_report_call("done", {})], usage=None)])
    _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    figures = _current(ledger, _live())["figures"]
    used = figures["context_used"]
    assert used["value"] is None
    assert used["basis"] == "unavailable"
    assert used["reason"]
    peak = figures["context_peak"]
    assert peak["value"] is None
    assert peak["basis"] == "unavailable"
    native = figures["native.context_estimate_tokens"]
    assert native["basis"] == "estimated"
    assert isinstance(native["value"], int)


def test_status_needs_no_transcript(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Figures come from the flush alone: deleting every other file changes nothing."""
    step = _agent_step("process")
    model = ScriptedModel(
        [_ai([_report_call("done", {})], usage=_usage_dict(3000, 500))]
    )
    _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    before = _current(ledger, _live())["figures"]
    assert before["context_used"]["value"] == 3500
    # Every ledger file (including -wal/-shm) stays: only Transcripts,
    # archives and evidence go, so equal figures prove the status path
    # never read them.
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file() and not path.name.startswith(ledger.path.name):
            path.unlink()
    after = _current(ledger, _live())["figures"]
    assert after == before
