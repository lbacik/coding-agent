"""The Worker's container log: one line per committed event and model turn.

Contract: issue #503. ``stdout``/``stderr`` are not part of the operator
contract, so these lines change no stored row, no ``status`` field, no
schema and no ``events.jsonl`` content — they only let an operator follow
a run from ``docker compose logs``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import archive as attempt_archives
from coding_agent.operator import listener as operator_listener
from coding_agent.operator import runlog
from coding_agent.operator.events import emit_contract_event
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.transcript import TranscriptModel, TranscriptWriter
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel

REPO = "example-owner/example-repo"
ATTEMPT_ID = "implement#7/1"

SECRET_NAME = "WORKER_TOKEN"
SECRET_VALUE = "ghp_testsecretvalue123"


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _enable_log() -> None:
    """Opt this test into the Worker container log (reset after, see conftest)."""
    runlog.set_container_log_enabled(True)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(
        tmp_path / "run-ledger.sqlite3", registry={SECRET_NAME: SECRET_VALUE}
    )
    try:
        yield handle
    finally:
        handle.close()


def _open_attempt(ledger: RunLedger, attempt_id: str = ATTEMPT_ID) -> None:
    ledger.open_attempt(
        attempt_id,
        effective_configuration={"repo": "example/test", "issue": 7},
    )


# --- 1. Every Attempt-scoped event prints exactly one prefixed line ---


def _attempt_types() -> list[str]:
    return sorted(attempt_archives.ATTEMPT_EVENT_TYPES)


@pytest.mark.parametrize("event_type", _attempt_types())
def test_attempt_scoped_event_prints_exactly_one_prefixed_line(
    ledger: RunLedger, capsys: pytest.CaptureFixture[str], event_type: str
) -> None:
    """Every Attempt-scoped type prints once, with the Attempt id prefix."""
    _enable_log()
    _open_attempt(ledger)
    capsys.readouterr()

    ledger.emit_attempt_event(ATTEMPT_ID, event_type, f"probe {event_type}")

    captured = capsys.readouterr()
    assert captured.out == f"run: [{ATTEMPT_ID}] probe {event_type}\n"
    assert captured.err == ""


def test_claim_opened_prints_on_open(
    ledger: RunLedger, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Claim path prints its stored text with the prefix."""
    _enable_log()

    _open_attempt(ledger)

    captured = capsys.readouterr()
    assert captured.out == f"run: [{ATTEMPT_ID}] claim opened {ATTEMPT_ID}\n"
    assert captured.err == ""


# --- 2. Process events print without a prefix ---


@pytest.mark.parametrize(
    ("event_type", "kwargs", "text"),
    [
        ("process_started", {"agent_version": "v1"}, "worker started"),
        ("startup_check", {"check": "skill-bundle", "result": "ok"}, "check ok"),
        ("ready", {}, "ready"),
    ],
)
def test_process_event_prints_one_unprefixed_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    event_type: str,
    kwargs: dict[str, Any],
    text: str,
) -> None:
    """Process events print once as ``run: <text>`` on stdout."""
    _enable_log()
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    try:
        store.write_instance_metadata(
            target_repository=REPO, process_started_at=_started_at()
        )
        capsys.readouterr()

        record = store.emit_startup_event(event_type=event_type, text=text, **kwargs)

        assert record is not None
        captured = capsys.readouterr()
        assert captured.out == f"run: {text}\n"
        assert captured.err == ""
    finally:
        store.close()
        ledger.close()


# --- 3. Command events print on commit through the listener ---


def test_stop_command_prints_accepted_and_completed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed command prints its accepted and resolved lines, once each."""
    _enable_log()
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    try:
        store.write_instance_metadata(
            target_repository=REPO, process_started_at=_started_at()
        )
        live = LiveView(process_started_at=_started_at())
        live.set_ready()
        capsys.readouterr()

        raw = (
            json.dumps(
                {"op": "stop", "request_id": "req-log-1", "expect_repo": REPO}
            )
            + "\n"
        ).encode("utf-8")
        reply = operator_listener._handle_one(
            raw, ledger_path=tmp_path / "run-ledger.sqlite3", live=live
        )

        assert reply is not None and "command" in reply
        captured = capsys.readouterr()
        assert captured.out.splitlines() == [
            "run: stop req-log-1 accepted",
            "run: stop req-log-1 completed",
        ]
        assert captured.err == ""
    finally:
        store.close()
        ledger.close()


# --- 4. Rolled-back events print nothing ---


def test_rolled_back_event_prints_nothing_and_stores_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An event queued then discarded (the rollback protocol) stays silent."""
    _enable_log()
    path = tmp_path / "run-ledger.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        record = emit_contract_event(
            connection,
            process_started_at="2026-11-06T08:00:00Z",
            event_type="ready",
            text="ready",
        )
        runlog.queue_pending_event(connection, record)
        connection.rollback()
        runlog.discard_pending_events(connection)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        count = connection.execute(
            "SELECT COUNT(*) FROM operator_events"
        ).fetchone()
        assert count is not None and int(count[0]) == 0
    finally:
        connection.close()


# --- 5. Registered secrets never reach the log ---


def test_secret_in_event_text_prints_only_as_marker(
    ledger: RunLedger, capsys: pytest.CaptureFixture[str]
) -> None:
    """A secret in stored event text prints only as its redaction marker."""
    _enable_log()
    _open_attempt(ledger)
    capsys.readouterr()

    ledger.emit_attempt_event(
        ATTEMPT_ID, "phase_changed", f"moving with {SECRET_VALUE} inside"
    )

    captured = capsys.readouterr()
    assert SECRET_VALUE not in captured.out
    assert f"«redacted:{SECRET_NAME}»" in captured.out
    assert captured.out.startswith(f"run: [{ATTEMPT_ID}] ")


def test_secret_in_tool_name_and_conversation_prints_only_as_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Secrets in tool names and conversation labels print only as markers."""
    _enable_log()
    writer = TranscriptWriter(
        tmp_path / "transcript.jsonl", {SECRET_NAME: SECRET_VALUE}
    )
    reply = AIMessage(
        content="calling",
        tool_calls=[
            {
                "name": f"read_{SECRET_VALUE}_file",
                "args": {"path": "notes.txt"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    model = TranscriptModel(
        ScriptedModel([reply]), writer, conversation=f"step/{SECRET_VALUE}"
    )

    model.invoke([HumanMessage(content="brief")])

    captured = capsys.readouterr()
    assert SECRET_VALUE not in captured.out
    assert f"«redacted:{SECRET_NAME}»" in captured.out


# --- 6. Model turns: names and usage, never arguments ---


def test_turn_line_lists_tool_names_and_usage_but_not_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A turn line names tool calls and usage, never their arguments."""
    _enable_log()
    writer = TranscriptWriter(tmp_path / "transcript.jsonl")
    reply = AIMessage(
        content="calling",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"path": "hunter2-secret-arg"},
                "id": "call_1",
                "type": "tool_call",
            },
            {
                "name": "run_command",
                "args": {"command": "hunter2-secret-arg"},
                "id": "call_2",
                "type": "tool_call",
            },
        ],
        usage_metadata={
            "input_tokens": 41000,
            "output_tokens": 1200,
            "total_tokens": 42200,
        },
    )
    model = TranscriptModel(
        ScriptedModel([reply]), writer, conversation="step/process#1"
    )

    model.invoke([HumanMessage(content="brief")])

    captured = capsys.readouterr()
    assert captured.out == (
        "run: [step/process#1] turn 1: read_file, run_command (in 41k / out 1.2k)\n"
    )
    assert "hunter2-secret-arg" not in captured.out


def test_turn_without_usage_prints_without_suffix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A response without usage metadata prints no usage part."""
    _enable_log()
    writer = TranscriptWriter(tmp_path / "transcript.jsonl")
    model = TranscriptModel(
        ScriptedModel([AIMessage(content="done")]),
        writer,
        conversation="step/process#1",
    )

    model.invoke([HumanMessage(content="brief")])

    captured = capsys.readouterr()
    assert captured.out == "run: [step/process#1] turn 1: no tool calls\n"


def test_failed_model_call_prints_conversation_and_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed model call prints one line naming the conversation and error."""
    _enable_log()
    writer = TranscriptWriter(tmp_path / "transcript.jsonl")

    class Failing(ScriptedModel):
        def invoke(self, input: Any) -> Any:
            raise RuntimeError("provider down")

    model = TranscriptModel(Failing([]), writer, conversation="step/process#1")

    with pytest.raises(RuntimeError, match="provider down"):
        model.invoke([HumanMessage(content="brief")])

    captured = capsys.readouterr()
    assert captured.out == "run: [step/process#1] turn 1 failed: RuntimeError\n"


def test_log_turns_zero_suppresses_turns_but_not_events(
    tmp_path: Path,
    ledger: RunLedger,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CODING_AGENT_LOG_TURNS=0`` silences turns; events still print."""
    _enable_log()
    monkeypatch.setenv("CODING_AGENT_LOG_TURNS", "0")
    writer = TranscriptWriter(tmp_path / "transcript.jsonl")
    model = TranscriptModel(
        ScriptedModel([AIMessage(content="done")]), writer
    )

    model.invoke([HumanMessage(content="brief")])

    assert capsys.readouterr().out == ""

    _open_attempt(ledger)
    captured = capsys.readouterr()
    assert captured.out == f"run: [{ATTEMPT_ID}] claim opened {ATTEMPT_ID}\n"


# --- 7. halted and error-level go to stderr ---


def test_ledger_halt_prints_to_stderr(
    ledger: RunLedger, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ledger's ``halted`` event goes to stderr, naming reason and detail."""
    _enable_log()

    ledger.record_halt(reason="credential-failure", detail="token expired")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "run: halted credential-failure: token expired\n"


def test_store_halt_prints_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator store's ``halted`` event goes to stderr."""
    _enable_log()
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    try:
        store.write_instance_metadata(
            target_repository=REPO, process_started_at=_started_at()
        )
        capsys.readouterr()

        store.record_halt(
            reason="credential-failure",
            detail="token expired",
            registry={SECRET_NAME: SECRET_VALUE},
        )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "run: halted credential-failure: token expired\n"
    finally:
        store.close()
        ledger.close()


def test_error_level_event_prints_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Any error-level record prints to stderr, info to stdout."""
    _enable_log()

    runlog.print_contract_event(
        {"event": "ready", "text": "bad", "level": "error", "attempt_id": ATTEMPT_ID}
    )
    runlog.print_contract_event(
        {"event": "ready", "text": "good", "level": "info", "attempt_id": ATTEMPT_ID}
    )

    captured = capsys.readouterr()
    assert captured.out == f"run: [{ATTEMPT_ID}] good\n"
    assert captured.err == f"run: [{ATTEMPT_ID}] bad\n"


# --- 8. Idle polls and selection ---

_POLL_LABEL = "agent:implement"


def _idle_workflow() -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=_POLL_LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="work",
        outcomes={},
        defaults_routes={"error": RouteTarget(end="failed")},
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": RouteTarget(end="delivered")},
            ),
        },
    )


def _idle_tracker(*numbers: int) -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    for number in numbers:
        tracker.inputs[number] = TrackedInput(
            number=number,
            input_type="issue",
            title=f"Add a greeting {number}",
            body="## Acceptance criteria\n- [ ] greet\n",
            is_open=True,
            labels=(_POLL_LABEL,),
            assignees=(),
            created_at="2026-10-01T00:00:00Z",
            author_login="human",
            author_id=7,
        )
        tracker.labeled_events[(number, _POLL_LABEL)] = LabeledEvent(
            label=_POLL_LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at="2026-10-01T00:00:00Z",
        )
    return tracker


def _idle_config(workflow: Workflow) -> ClaimConfig:
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=_POLL_LABEL,
        base_branch="main",
        agent_login="coding-agent",
        agent_id=999,
        effective_configuration=full_effective_configuration(workflow),
    )


def _no_sleep(seconds: float) -> None:
    _ = seconds


def _stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_consecutive_idle_polls_print_no_ready_issue_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Idle sweeps print ``no ready issue`` once, not once per poll."""
    _enable_log()
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        workflow = _idle_workflow()
        config = WorkerLoopConfig(
            claim_config=_idle_config(workflow),
            workflow=workflow,
            poll_interval_seconds=60.0,
        )

        def _never_run(claimed: ConfirmedClaim) -> WorkflowResult:
            raise AssertionError(f"no Attempt should run: {claimed!r}")

        assert run_worker_loop(
            tracker=_idle_tracker(),
            ledger=ledger,
            loop_config=config,
            run_one_attempt=_never_run,
            sleep=_no_sleep,
            max_sweeps=3,
        ) == 0
        assert _stdout_lines(capsys) == ["run: no ready issue"]

        # Going idle again (a later loop) prints it once more.
        assert run_worker_loop(
            tracker=_idle_tracker(),
            ledger=ledger,
            loop_config=config,
            run_one_attempt=_never_run,
            sleep=_no_sleep,
            max_sweeps=2,
        ) == 0
        assert _stdout_lines(capsys) == ["run: no ready issue"]
    finally:
        ledger.close()


def test_selection_prints_issue_number_then_idle_prints_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A selection prints its issue number; the following idle prints once."""
    _enable_log()
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        workflow = _idle_workflow()
        config = WorkerLoopConfig(
            claim_config=_idle_config(workflow),
            workflow=workflow,
            poll_interval_seconds=60.0,
        )
        tracker = _idle_tracker(31)

        def _finish(claimed: ConfirmedClaim) -> WorkflowResult:
            ledger.record_terminal_outcome(
                claimed.attempt_id,
                outcome="delivered",
                reason="verified-completion",
            )
            tracker.remove_label(claimed.input_number, "issue", _POLL_LABEL)
            tracker.unassign_input(claimed.input_number, "issue")
            return WorkflowResult(
                attempt_id=claimed.attempt_id,
                outcome="delivered",
                reason="verified-completion",
            )

        assert run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=config,
            run_one_attempt=_finish,
            sleep=_no_sleep,
            max_sweeps=3,
        ) == 0

        lines = _stdout_lines(capsys)
        assert "run: selected issue 31" in lines
        assert [line for line in lines if "no ready issue" in line] == [
            "run: no ready issue"
        ]
        assert lines.index("run: selected issue 31") < lines.index(
            "run: no ready issue"
        )
    finally:
        ledger.close()


# --- 9. End-of-Attempt spend ---


def test_spend_suffix_carries_tokens_cost_tool_calls_and_elapsed(
    ledger: RunLedger,
) -> None:
    """The end-of-Attempt suffix reuses the status spend projection."""
    from coding_agent.worker.attempt_run import _spend_suffix

    _open_attempt(ledger, "implement#40/1")
    ledger.open_step_execution("implement#40/1", "process", 1)
    ledger.end_step_execution(
        "implement#40/1",
        "process",
        1,
        result="done",
        usage={
            "tokens": 1200,
            "effective_tokens": 1000,
            "cost_usd": 0.02,
            "tool_calls": 7,
        },
    )

    suffix = _spend_suffix(ledger, "implement#40/1")

    assert "tokens=1000" in suffix
    assert "cost_usd=0.0200" in suffix
    assert "tool_calls=7" in suffix
    assert "elapsed=" in suffix and suffix.endswith("s")
