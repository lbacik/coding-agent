"""The live view follows a running step-workflow Attempt.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §19 (the
Status Snapshot's ``native_phase`` and ``last_activity_at``): while an
Attempt runs, the production Attempt runner reports every step transition
through the live view's ``set_phase`` and every settled model response
through its ``touch``, so ``status`` names the running step and carries a
fresh activity time. Step-graph ``phase_changed`` Contract Events carry the
step phase in ``phase``. All reporting is best-effort: a failing hook never
fails the Attempt, and with no live view the Attempt runs as before.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.inputs.claim import ConfirmedClaim, sweep_and_claim
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    AgentStepDeps,
    run_agent_step,
)
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.workflow_graph import WorkflowResult, run_workflow
from coding_agent.worker import supervisor
from coding_agent.worker.attempt_run import run_attempt
from coding_agent.worker.startup import Ready
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    WorkflowStep,
    Workflow,
)
from support.agent_step_deps import make_agent_step_deps
from support.fake_step_tracker import FakeStepTracker
from support.scripted_executor import ScriptedStepExecutor
from support.scripted_model import ScriptedModel
from test_worker_run import (
    _bare_remote,
    _tracker_with,
    _write_run_config,
    _write_run_matrix,
)
from worker_fakes import make_worker_ports

ATTEMPT_ID = "implement#40/1"


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake startup credentials the Worker startup requires."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _report(
    result: str = "done", payload_json: str = "{}"
) -> AIMessage:
    """One scripted response reporting the step's result with usage."""
    return AIMessage(
        content="reporting",
        tool_calls=[
            {
                "name": REPORT_TOOL_NAME,
                "args": {"result": result, "payload_json": payload_json},
                "id": "report-1",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 0, "cache_creation": 0},
        },
    )


def _run_production_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: ScriptedModel,
) -> tuple[int, Path]:
    """Run one Attempt through the production Attempt runner (issue #501).

    The real Workflow runner drives the real graph against the scripted
    model; the Worker performs its real Claim, checkout and finalisation.
    Returns the exit code and the state directory.
    """
    _with_env(monkeypatch)
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))
    remote = _bare_remote(tmp_path)
    fakes = make_worker_ports(
        model=model, tracker=tracker, repository_url=str(remote)
    )
    exit_code = supervisor.run_worker(
        config=_write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_attempts=1,
    )
    return exit_code, tmp_path / "state"


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Script ``LiveView._now_iso`` to tick one second per call, recording each."""
    base = datetime(2026, 11, 19, 8, 11, 21, tzinfo=timezone.utc)
    stamps: list[str] = []

    def _now_iso() -> str:
        stamp = (base + timedelta(seconds=len(stamps) + 1)).isoformat().replace(
            "+00:00", "Z"
        )
        stamps.append(stamp)
        return stamp

    monkeypatch.setattr(LiveView, "_now_iso", staticmethod(_now_iso))
    return stamps


def test_production_attempt_reports_step_phase_and_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live view follows the running step through the production runner.

    While the ``work`` step runs, the Status Snapshot's ``native_phase``
    is ``work#1`` — not ``claim`` — and ``last_activity_at`` moves past
    the Claim time at step open and again when the model response settles.
    """
    _fake_clock(monkeypatch)
    phases: list[str] = []
    currents: list[dict[str, Any] | None] = []
    touches = 0
    statuses: list[dict[str, Any]] = []
    real_set_phase = LiveView.set_phase
    real_touch = LiveView.touch
    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"

    def _recording_set_phase(self: LiveView, phase: str) -> None:
        phases.append(phase)
        real_set_phase(self, phase)
        currents.append(self.get_current())
        statuses.append(operator_listener.build_status_result(ledger_path, self))

    def _recording_touch(self: LiveView) -> None:
        nonlocal touches
        touches += 1
        real_touch(self)

    monkeypatch.setattr(LiveView, "set_phase", _recording_set_phase)
    monkeypatch.setattr(LiveView, "touch", _recording_touch)
    exit_code, _ = _run_production_attempt(
        tmp_path, monkeypatch, ScriptedModel([_report()])
    )

    assert exit_code == 0
    # The graph reports the step phase at open and at end — never ``claim``.
    assert phases == ["work#1", "work#1"]
    assert currents[0] is not None
    assert currents[0]["phase"] == "work#1"
    # The first step transition moves the activity time past the Claim time.
    assert currents[0]["last_activity_at"] > currents[0]["started_at"]
    # The settled model response touched the live view, moving activity again.
    assert touches >= 1
    assert currents[1] is not None
    assert currents[1]["last_activity_at"] >= currents[0]["last_activity_at"]
    # ``status`` itself names the running step while the step runs.
    assert statuses[0]["current_attempt"] is not None
    assert statuses[0]["current_attempt"]["native_phase"] == "work#1"


def test_phase_changed_events_carry_the_step_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archived ``phase_changed`` records name the step phase (#501).

    Every ``phase_changed`` line the step graph emits carries the step
    phase in ``phase`` — ``work#1`` — instead of the lifecycle state's
    ``claim-confirmed``.
    """
    exit_code, state_dir = _run_production_attempt(
        tmp_path, monkeypatch, ScriptedModel([_report()])
    )

    assert exit_code == 0
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        archive = ledger.archive_dir_for(ATTEMPT_ID)
    finally:
        ledger.close()
    assert archive is not None
    records = [
        json.loads(line)
        for line in (archive / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    changed = [record for record in records if record.get("event") == "phase_changed"]
    step_changed = [
        record for record in changed if str(record.get("text")) == "phase work#1"
    ]
    # The step graph emits one ``phase_changed`` at step open and one at
    # step end; lifecycle transitions (the Claim, finalisation) keep their
    # own resolved phases — out of scope here.
    assert len(step_changed) == 2
    assert all(record.get("phase") == "work#1" for record in step_changed)


def _workflow() -> Workflow:
    """One agent step routed straight to ``delivered``."""
    return Workflow(
        id="w",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0, effective_tokens=100000.0, tool_calls=10000.0, wall_clock_minutes=600.0
        ),
        start="process",
        defaults_routes={"error": RouteTarget(end="failed")},
        steps={
            "process": WorkflowStep(
                id="process",
                agent=Agent(tools=("workspace-read",), results={"done": {}}),
                routes={
                    "done": RouteTarget(end="delivered"),
                    "error": RouteTarget(end="failed"),
                    "limit-exceeded": RouteTarget(end="unfinished"),
                },
            )
        },
    )


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "ledger" / "run-ledger.sqlite3")


def test_raising_phase_hook_does_not_change_the_outcome(
    tmp_path: Path,
) -> None:
    """A failing ``phase_hook`` never fails the Attempt it reports on."""

    def _boom(phase: str) -> None:
        raise AssertionError(f"hook must not fail the Attempt ({phase})")

    ledger = _ledger(tmp_path)
    try:
        result = run_workflow(
            workflow=_workflow(),
            attempt_id="w#7/1",
            ledger=ledger,
            step_executor=ScriptedStepExecutor({"process": ["done"]}),
            phase_hook=_boom,
        )
    finally:
        ledger.close()
    assert result.outcome == "delivered"


def _step_deps(
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    **overrides: Any,
) -> AgentStepDeps:
    return make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, **overrides
    )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("with_usage", [True, False])
def test_raising_activity_callback_does_not_change_the_outcome(
    tmp_path: Path, with_usage: bool
) -> None:
    """A failing activity callback never fails the Step Execution.

    Covers both settle paths: a response carrying usage metadata and one
    settling empty.
    """

    def _boom() -> None:
        raise AssertionError("activity must not fail the step")

    response = _report()
    if not with_usage:
        response = AIMessage(
            content="reporting",
            tool_calls=list(response.tool_calls),
        )
    step = _workflow().steps["process"]
    deps = _step_deps(
        ScriptedModel([response]),
        _workspace(tmp_path),
        tmp_path,
        on_activity=_boom,
    )
    outcome = run_agent_step(step, 1, deps, {})

    assert outcome.status == "reported"
    assert outcome.result_name == "done"


def test_settled_responses_touch_the_live_view(
    tmp_path: Path,
) -> None:
    """Every settled model response moves ``last_activity_at`` (#501)."""
    live = LiveView(process_started_at=datetime.now(timezone.utc))
    before = live.get_current()
    assert before is None
    step = _workflow().steps["process"]
    deps = _step_deps(
        ScriptedModel([_report()]),
        _workspace(tmp_path),
        tmp_path,
        on_activity=live.touch,
    )
    live.set_claim("w#7/1", 7, "Fix the crash")
    claimed_at = live.get_current()
    assert claimed_at is not None
    outcome = run_agent_step(step, 1, deps, {})

    assert outcome.status == "reported"
    settled_at = live.get_current()
    assert settled_at is not None
    assert settled_at["last_activity_at"] > claimed_at["last_activity_at"]


def test_no_live_view_runs_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``ready.live is None`` the Attempt runner passes no hooks (#501).

    The Workflow runner receives ``phase_hook=None`` and the step executor
    runs with no activity callback — the Attempt runs as it did before
    live reporting.
    """
    _with_env(monkeypatch)
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))
    remote = _bare_remote(tmp_path)
    fakes = make_worker_ports(
        model=ScriptedModel([]), tracker=tracker, repository_url=str(remote)
    )
    from coding_agent.worker import startup as startup_module

    started = startup_module.start(
        config=_write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=fakes.ports,
    )
    assert isinstance(started, Ready)
    captured: dict[str, Any] = {}

    def _stub_runner(**kwargs: Any) -> WorkflowResult:
        captured.update(kwargs)
        attempt_id = str(kwargs["attempt_id"])
        ledger_arg: RunLedger = kwargs["ledger"]
        ledger_arg.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
            ended=True,
        )

    with started as ready:
        ready.live = None
        claimed = sweep_and_claim(tracker, ready.ledger, ready.claim_config)
        assert isinstance(claimed, ConfirmedClaim)
        result = run_attempt(ready, claimed, workflow_runner=_stub_runner)

    assert result.outcome == "completed"
    assert captured["phase_hook"] is None
    executor = captured["step_executor"]
    assert isinstance(executor, ProductionStepExecutor)
    assert executor.agent_deps is not None
    assert executor.agent_deps.on_activity is None


def test_no_activity_callback_runs_as_before(tmp_path: Path) -> None:
    """Without an activity callback the step loop settles as it always did."""
    step = _workflow().steps["process"]
    deps = _step_deps(ScriptedModel([_report()]), _workspace(tmp_path), tmp_path)
    assert deps.on_activity is None
    outcome = run_agent_step(step, 1, deps, {})

    assert outcome.status == "reported"
    assert outcome.result_name == "done"


def test_run_without_phase_hook_runs_as_before(tmp_path: Path) -> None:
    """Without a ``phase_hook`` the graph runs as it did before (#501)."""
    ledger = _ledger(tmp_path)
    try:
        result = run_workflow(
            workflow=_workflow(),
            attempt_id="w#7/1",
            ledger=ledger,
            step_executor=ScriptedStepExecutor({"process": ["done"]}),
        )
    finally:
        ledger.close()
    assert result.outcome == "delivered"


def test_production_attempt_writes_live_transcript_into_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§13.3: the Attempt Archive carries the live, contract-shaped Transcript.

    The production Attempt runner writes the Transcript record by record
    into the Attempt Archive — not the MVP evidence layout — with one
    ``step/<step-id>/<n>`` conversation. Nothing lands at the MVP
    ``evidence/<owner>/<repo>/<N>/<n>/`` path, and a sealed export of the
    Attempt carries the live file with the manifest listing it.
    """
    import hashlib
    import io
    import tarfile

    from coding_agent.operator import attempts as attempt_archives

    exit_code, state_dir = _run_production_attempt(
        tmp_path, monkeypatch, ScriptedModel([_report()])
    )
    assert exit_code == 0
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        archive = ledger.archive_dir_for(ATTEMPT_ID)
    finally:
        ledger.close()
    assert archive is not None
    lines = (archive / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    assert records
    assert {record["conversation"] for record in records} == {"step/work/1"}
    assert not any(record["conversation"] == "caller" for record in records)
    seqs = [record["seq"] for record in records]
    assert all(isinstance(seq, int) for seq in seqs)
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(record["at"] for record in records)
    assert any(record["kind"] == "request" for record in records)
    responses = [record for record in records if record["kind"] == "response"]
    assert responses
    assert any(
        any(
            call.get("name") == REPORT_TOOL_NAME
            and call.get("id") == "report-1"
            and isinstance(call.get("args"), dict)
            for call in record["message"].get("tool_calls", [])
        )
        for record in responses
    )
    # No transcript at the MVP evidence layout for this archived Attempt:
    # the archive path carries the extra ``<workflow>`` segment the MVP
    # path lacks, so strip it to name the path that must stay absent.
    relative = archive.relative_to(state_dir / "evidence")
    assert len(relative.parts) == 5
    owner, repo, _workflow, number, n = relative.parts
    mvp_transcript = (
        state_dir / "evidence" / owner / repo / number / n / "transcript.jsonl"
    )
    assert not mvp_transcript.is_file()
    # The end-of-Attempt line names the archive path actually used.
    assert f"run: transcript {archive / 'transcript.jsonl'}" in capsys.readouterr().out
    # A sealed export of the Attempt carries the live file; the manifest
    # lists it with the live bytes' digest.
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        ledger.set_attempt_state(ATTEMPT_ID, "finished")
        shown = attempt_archives.show_attempt(
            ledger_path=state_dir / "run-ledger.sqlite3",
            state_dir=state_dir,
            attempt_id=ATTEMPT_ID,
        )
    finally:
        ledger.close()
    assert shown is not None
    assert shown["attempt"]["sealed"] is True
    by_path = {entry["path"]: entry for entry in shown["manifest"]}
    assert "transcript.jsonl" in by_path
    live_bytes = (archive / "transcript.jsonl").read_bytes()
    assert by_path["transcript.jsonl"]["sha256"] == hashlib.sha256(live_bytes).hexdigest()
    document = attempt_archives.export_manifest_document(
        attempt=dict(shown["attempt"]),
        files=list(shown["manifest"]),
    )
    buffer = io.BytesIO()
    attempt_archives.write_export_tar(
        archive_dir=archive,
        document=document,
        entries=list(shown["manifest"]),
        fileobj=buffer,
    )
    reader = tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r")
    try:
        member = reader.getmember("transcript.jsonl")
        data = reader.extractfile(member)
        assert data is not None
        assert data.read() == live_bytes
    finally:
        reader.close()


def test_production_attempt_without_archive_still_writes_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the archive cannot resolve the Attempt still runs (#521).

    The Transcript falls back to the MVP evidence layout instead of
    failing the Attempt.
    """
    from langchain_core.messages import HumanMessage

    _with_env(monkeypatch)
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))
    remote = _bare_remote(tmp_path)
    model = ScriptedModel([_report()])
    fakes = make_worker_ports(
        model=model, tracker=tracker, repository_url=str(remote)
    )
    monkeypatch.setattr(
        RunLedger,
        "archive_dir_for",
        lambda self, attempt_id: None,
    )
    from coding_agent.worker import startup as startup_module

    started = startup_module.start(
        config=_write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=fakes.ports,
    )
    assert isinstance(started, Ready)

    def _stub_runner(**kwargs: Any) -> WorkflowResult:
        executor: ProductionStepExecutor = kwargs["step_executor"]
        assert executor.agent_deps is not None
        executor.agent_deps.model.invoke([HumanMessage(content="hello")])
        attempt_id = str(kwargs["attempt_id"])
        ledger_arg: RunLedger = kwargs["ledger"]
        ledger_arg.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
            ended=True,
        )

    with started as ready:
        claimed = sweep_and_claim(tracker, ready.ledger, ready.claim_config)
        assert isinstance(claimed, ConfirmedClaim)
        result = run_attempt(ready, claimed, workflow_runner=_stub_runner)
    assert result.outcome == "completed"
    found = list((tmp_path / "state" / "evidence").rglob("transcript.jsonl"))
    assert len(found) == 1
    records = [
        json.loads(line)
        for line in found[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["kind"] for record in records] == ["request", "response"]
