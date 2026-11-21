"""Main-conversation ``cache_hit_rate`` in ``current_attempt``.

Contract §9.2: ``cache_hit_rate`` is ``cache_read_tokens`` over
``input_tokens`` summed over the main conversation's settled responses
only — every work node's own conversation, ``step/<id>`` and its
continuations, across all steps of the Attempt. Sub-agent conversations
and Context-Window Handoff turns are excluded, the same "main
conversation" boundary the context figures use. The counters travel in
the Attempt usage flush payload, so ``status`` serves prepared state;
unknown is never zero; and ledgers without main-conversation counters
(the Step Execution fallback, older flushes) read ``unavailable`` with a
reason instead of an Attempt-wide ratio presented as ``measured``.
"""

from __future__ import annotations

import json
import sqlite3
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
from coding_agent.runtime.graph.agent_handoff import produce_handoff_snapshot
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


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "limits": {
                "wall_clock_minutes": 60,
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
            }
        }
    }


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "run-ledger.sqlite3")


def _open(ledger: RunLedger, attempt_id: str = ATTEMPT_ID) -> None:
    try:
        ledger.open_attempt(attempt_id, effective_configuration=_effective_config())
    except sqlite3.IntegrityError:
        pass


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


def _figures(ledger: RunLedger) -> dict[str, Any]:
    figures = _current(ledger, _live())["figures"]
    assert isinstance(figures, dict)
    return figures


def _context(ledger: RunLedger, attempt_id: str = ATTEMPT_ID) -> dict[str, Any]:
    payload = ledger.get_latest_usage(attempt_id)
    assert payload is not None
    context = payload.get("context")
    assert isinstance(context, dict)
    return context


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


def _budgets(attempt_id: str = ATTEMPT_ID) -> AttemptBudgets:
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
    )
    return build_budgets(workflow, attempt_id=attempt_id)


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
    step: WorkflowStep, deps: AgentStepDeps, slack: int = 400, anchor: int | None = None
) -> ContextWindowConfig:
    """A window whose usable threshold the opening fits but the opening plus
    one padded turn exceeds. With ``anchor`` (the first response reports
    ``anchor`` input tokens) the usable value sits one slack turn above the
    anchor, so the anchored context that follows crosses it while the
    headroom above usable still fits the handoff turn itself — the admitted
    handoff request is not an overflow."""
    tools = build_agent_step_tools(step, deps)
    opening = compose_step_opening(step, deps, {})
    if anchor is None:
        usable = estimate_context_tokens(list(opening), [], list(tools)) + slack
    else:
        usable = anchor + 500
    window = ContextWindowConfig(
        threshold_tokens=usable + 1_000 + 2_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=2_000,
        handoff_token_cap=200,
    )
    assert window.usable_threshold == usable
    return window


def _handoff_response(
    snapshot: ContinuationSnapshot | None = None,
    usage: dict[str, object] | None = None,
) -> AIMessage:
    """One queued handoff-turn response carrying the snapshot JSON (§9.9)."""
    return _ai(None, (snapshot or _handoff_snapshot()).to_json(), usage)


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


def _run_step(
    step: WorkflowStep,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
    *,
    budgets: AttemptBudgets | None = None,
    window: ContextWindowConfig | None = None,
    handoff_producer: Any = None,
    seq: int = 1,
) -> tuple[AgentStepDeps, AttemptBudgets, list[dict[str, Any]]]:
    """Run one Step Execution, capturing every Attempt usage flush payload."""
    _open(ledger)
    resolved = budgets if budgets is not None else _budgets()
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=resolved,
        context_window=window,
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
    outcome = run_agent_step(step, seq, deps, {})
    assert outcome.status == "reported"
    return deps, resolved, captured


# --- Main conversation only --------------------------------------------------


def test_main_responses_measure_cache_hit_rate(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main responses with usage report Σread/Σinput as measured."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage_dict(1000, 100, cache_read=800),
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(500, 50, cache_read=100),
            ),
        ]
    )
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    context = _context(ledger)
    assert context["cache_read"] == 900
    assert context["cache_input"] == 1500
    assert context["responses_without_usage"] == 0
    assert context["conversation"] == "step/process/1"
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["basis"] == "measured"
    assert rate["value"] == pytest.approx(900 / 1500)
    assert rate["unit"] == "ratio"


def test_subagent_response_leaves_cache_hit_rate_unchanged(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Sub-agent turn with a cold cache never moves the main-conversation rate."""
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
                usage=_usage_dict(1000, 100, cache_read=800),
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
                usage=_usage_dict(500, 50),
            ),
        ]
    )
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    context = _context(ledger)
    assert context["cache_read"] == 800
    assert context["cache_input"] == 1500
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["basis"] == "measured"
    assert rate["value"] == pytest.approx(800 / 1500)
    # Budget figures stay Attempt-wide: the Sub-agent's spend counts there.
    budget = _current(ledger, _live())["budget"]
    assert budget["raw_tokens"]["value"] == 1100 + 10000 + 550
    assert budget["effective_tokens"]["value"] == 300 + 10000 + 550


def test_handoff_charge_leaves_cache_counters_unchanged(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handoff charge adds no cache counters; the continuation still counts."""
    big = "p" * 4000
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "big-1")],
                content=big,
                usage=_usage_dict(40000, 5000, cache_read=30000),
            ),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(3000, 200, cache_read=1000),
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
    window = _calibrated_window(step, probe, anchor=40000)

    _, _, captured = _run_step(
        step,
        model,
        workspace,
        tmp_path,
        ledger,
        monkeypatch,
        window=window,
        handoff_producer=produce_handoff_snapshot,
    )
    assert captured, "the run must flush Attempt usage"
    # Only the two settled main responses ever move the counters: the
    # handoff turn's chars/4 charge never appears in them.
    seen: set[tuple[int, int]] = set()
    for payload in captured:
        seen.add(
            (payload["context"]["cache_read"], payload["context"]["cache_input"])
        )
    assert seen == {(30000, 40000), (31000, 43000)}
    context = _context(ledger)
    assert context["conversation"] == "step/process/1/continuation/1"
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["basis"] == "measured"
    assert rate["value"] == pytest.approx(31000 / 43000)
    # The Attempt-wide spend does carry the handoff charge on top.
    latest = ledger.get_latest_usage(ATTEMPT_ID)
    assert latest is not None
    usage = latest["usage"]
    assert isinstance(usage, dict)
    assert usage["input_tokens"] > 43000


def test_second_work_node_counts_toward_same_ratio(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Responses from a later step count toward the same main-conversation ratio."""
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage_dict(1000, 100, cache_read=800),
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(500, 50, cache_read=100),
            ),
            _ai(
                [_call("read_file", {"path": "README.md"}, "r2")],
                usage=_usage_dict(2000, 200, cache_read=500),
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(400, 40, cache_read=400),
            ),
        ]
    )
    shared = _budgets()
    _, _, _ = _run_step(
        _agent_step("process"), model, workspace, tmp_path, ledger, monkeypatch,
        budgets=shared,
    )
    _, _, _ = _run_step(
        _agent_step("verify"), model, workspace, tmp_path, ledger, monkeypatch,
        budgets=shared,
    )
    context = _context(ledger)
    assert context["cache_read"] == 800 + 100 + 500 + 400
    assert context["cache_input"] == 1000 + 500 + 2000 + 400
    assert context["conversation"] == "step/verify/1"
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["basis"] == "measured"
    assert rate["value"] == pytest.approx(1800 / 3900)


# --- Unknown is never zero ----------------------------------------------------


def test_no_main_response_yet_is_unavailable_never_zero(tmp_path: Path) -> None:
    """A flush with no settled main response reads unavailable, never zero."""
    handle = _ledger(tmp_path)
    try:
        _open(handle)
        budgets = _budgets()
        handle.record_usage(
            ATTEMPT_ID,
            BudgetSnapshot(
                elapsed_seconds=1.0,
                usage=budgets.attempt_totals(),
                context=budgets.main_context_snapshot(),
            ),
        )
        rate = _figures(handle)["cache_hit_rate"]
        assert rate["value"] is None
        assert rate["basis"] == "unavailable"
        assert rate["reason"]
    finally:
        handle.close()


def test_legacy_flush_without_counters_is_unavailable(tmp_path: Path) -> None:
    """A flush with no main-conversation counters never reports a ratio."""
    handle = _ledger(tmp_path)
    try:
        _open(handle)
        handle.record_usage(
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
                context={
                    "used": 3500,
                    "peak": 3500,
                    "estimate": 3200,
                    "conversation": "step/process/1",
                },
            ),
        )
        rate = _figures(handle)["cache_hit_rate"]
        assert rate["value"] is None
        assert rate["basis"] == "unavailable"
        assert "not recorded" in str(rate.get("reason"))
    finally:
        handle.close()


def test_main_response_without_usage_is_unavailable_naming_count(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main response without usage makes the rate unavailable with its count."""
    step = _agent_step("process")
    model = ScriptedModel([_ai([_report_call("done", {})], usage=None)])
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    context = _context(ledger)
    assert context["responses_without_usage"] == 1
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["value"] is None
    assert rate["basis"] == "unavailable"
    assert rate["reason"] == "usage metadata missing for 1 response(s)"


def test_partial_data_after_missing_usage_stays_unavailable(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later measured response does not rescue a ratio over partial data."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=None,
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(500, 50, cache_read=400),
            ),
        ]
    )
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["value"] is None
    assert rate["basis"] == "unavailable"
    assert rate["reason"] == "usage metadata missing for 1 response(s)"


def test_subagent_response_without_usage_leaves_cache_measured(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Sub-agent response without usage never touches the main counters."""
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
                usage=_usage_dict(1000, 100, cache_read=800),
            ),
            _ai(
                [
                    _call(
                        "report_subagent",
                        {"report": "nothing to report"},
                        "sub-1",
                    )
                ],
                usage=None,
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(500, 50, cache_read=100),
            ),
        ]
    )
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    context = _context(ledger)
    assert context["responses_without_usage"] == 0
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["basis"] == "measured"
    assert rate["value"] == pytest.approx(900 / 1500)


# --- Restart and prepared state ------------------------------------------------


def test_counters_survive_reset_and_restore() -> None:
    """Handoff/step resets keep the cache counters; a restart restores them."""
    budgets = _budgets()
    budgets.note_main_cache_hit(800, 1000, "step/process/1")
    budgets.reset_main_context("step/process/1/continuation/1")
    snapshot = budgets.main_context_snapshot()
    assert snapshot["cache_read"] == 800
    assert snapshot["cache_input"] == 1000
    assert snapshot["responses_without_usage"] == 0
    budgets.note_main_cache_hit(100, 500, "step/process/1/continuation/1")
    budgets.note_main_cache_missing("step/process/1/continuation/1")
    snapshot = budgets.main_context_snapshot()
    assert snapshot["cache_read"] == 900
    assert snapshot["cache_input"] == 1500
    assert snapshot["responses_without_usage"] == 1
    payload = {
        "usage": budgets.attempt_totals().as_readable(),
        "elapsed_seconds": 1.0,
        "context": snapshot,
    }
    restarted = _budgets()
    restore_budgets_from_usage(restarted, payload)
    assert restarted.main_context_snapshot() == snapshot


def test_restart_continues_counters_from_last_flush(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a restart the next flush keeps accumulating the same ratio."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_call("read_file", {"path": "README.md"}, "r1")],
                usage=_usage_dict(1000, 100, cache_read=800),
            ),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(500, 50, cache_read=100),
            ),
        ]
    )
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    flushed = ledger.get_latest_usage(ATTEMPT_ID)
    assert flushed is not None
    restarted = _budgets()
    restore_budgets_from_usage(restarted, flushed)
    restarted.note_main_cache_hit(500, 2000, "step/verify/1")
    ledger.record_usage(
        ATTEMPT_ID,
        BudgetSnapshot(
            elapsed_seconds=restarted.elapsed_seconds(),
            usage=restarted.attempt_totals(),
            context=restarted.main_context_snapshot(),
        ),
    )
    context = _context(ledger)
    assert context["cache_read"] == 900 + 500
    assert context["cache_input"] == 1500 + 2000
    rate = _figures(ledger)["cache_hit_rate"]
    assert rate["basis"] == "measured"
    assert rate["value"] == pytest.approx(1400 / 3500)


def test_status_needs_no_transcript(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rate comes from the flush alone: deleting other files changes nothing."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(1000, 100, cache_read=800),
            )
        ]
    )
    _, _, _ = _run_step(step, model, workspace, tmp_path, ledger, monkeypatch)
    before = _figures(ledger)
    assert before["cache_hit_rate"]["basis"] == "measured"
    # Every ledger file (including -wal/-shm) stays: only Transcripts,
    # archives and evidence go, so equal figures prove the status path
    # never read them.
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file() and not path.name.startswith(ledger.path.name):
            path.unlink()
    after = _figures(ledger)
    assert after == before
