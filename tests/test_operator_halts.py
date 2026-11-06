"""Startup, recovery and halting observability (issue #250).

Contract: adoption step 4 of the operator contract (#245). An operator can
see a Worker go through startup and recovery, and can learn why a Worker
stopped without reading old stdout: startup order (validate, lock, store
plus listener in ``starting``, startup checks each reported as
``startup_check``, ledger, ``recovering`` while unfinished Attempts settle,
``idle``/``paused`` plus ``ready``), durable Halts (every non-zero exit the
Worker decides on after the volume lock, except an Unsupported
Environment), and the single Contract Event producer (redacted, capped at
300 characters, newest 50 per process in ``status.recent_events``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.errors import CredentialFailure, GitHubFailure
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.events import list_recent_contract_events
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker import supervisor
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_tracker import FakeTracker
from test_mvp_attempt_graph import IDENTITY  # noqa: F401  (fixtures reused)
from test_worker_run import _RUN_CONFIG, _write_run_config

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _pinned() -> dict[str, object]:
    """A minimal pinned Effective Configuration record (contract §2.5)."""
    return {
        "target": {
            "kind": "github",
            "repository": "example-owner/example-repo",
            "base_branch": "main",
            "token_env": "GITHUB_TOKEN",
            "ready_for_human": READY_FOR_HUMAN,
        },
        "workflow": {
            "id": "implement",
            "selector": {"label": SELECTION_LABEL},
        },
    }


def _candidate(number: int) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=True,
        assignees=(),
        labels=(SELECTION_LABEL,),
        created_at="2026-10-01T00:00:00Z",
    )


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        selection_label=SELECTION_LABEL,
        agent_login=IDENTITY.login,
        agent_identity_id=IDENTITY.account_id,
        effective_configuration=_pinned(),
    )


def _loop_config(live: LiveView | None = None) -> WorkerLoopConfig:
    return WorkerLoopConfig(
        claim_config=_claim_config(), poll_interval_seconds=60.0, live=live
    )


def _live() -> LiveView:
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore]:
    """A ledger plus the instance row mutations need for the repo guard."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository="example-owner/example-repo",
        process_started_at=_started_at(),
    )
    return ledger, store


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _status(ledger_path: Path, live: LiveView) -> dict[str, Any]:
    return operator_listener.build_status_result(ledger_path, live)


def _halt_row(ledger: RunLedger) -> dict[str, Any] | None:
    connection = sqlite3.connect(str(ledger.path))
    try:
        try:
            row = connection.execute(
                "SELECT reason, detail, occurred_at FROM operator_halt WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
    finally:
        connection.close()
    if row is None:
        return None
    return {"reason": str(row[0]), "detail": str(row[1]), "time": str(row[2])}


# --- Startup and recovery states --------------------------------------------


def test_status_starting_while_startup_check_held(tmp_path: Path) -> None:
    """While a startup check is held, ``status`` answers ``starting`` (#250)."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        live = LiveView(process_started_at=_started_at())
        live.set_startup_check("skill-bundle")
        result = _status(ledger.path, live)
        assert result["instance"]["state"] == "starting"
        assert result["startup_check"] == "skill-bundle"
        assert result["instance"]["startup_check"] == "skill-bundle"
    finally:
        store.close()
        ledger.close()


def test_status_recovering_while_unfinished_attempt_settled(tmp_path: Path) -> None:
    """An unfinished Attempt from an earlier process reads ``recovering``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_pinned())
        live = LiveView(process_started_at=_started_at())
        live.set_recovering(True)
        result = _status(ledger.path, live)
        assert result["instance"]["state"] == "recovering"
    finally:
        store.close()
        ledger.close()


def test_ready_reached_only_after_recovery_settles(
    ledger: RunLedger, tmp_path: Path
) -> None:
    """Readiness (a state other than ``starting``) waits for recovery (#250)."""
    _ = tmp_path
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)
    live = LiveView(process_started_at=_started_at())
    assert not live.is_ready()

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="delivered", reason="verified-completion"
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id,
            outcome="delivered",
            reason="verified-completion",
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(live),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert live.is_ready()
    assert not live.is_recovering()
    connection = sqlite3.connect(str(ledger.path))
    try:
        rows = connection.execute(
            "SELECT type FROM operator_events WHERE process_started_at = ? ORDER BY id",
            (_started_at().isoformat(),),
        ).fetchall()
    finally:
        connection.close()
    assert [str(row[0]) for row in rows][0] == "ready"


# --- Halts --------------------------------------------------------------------


def test_credential_failure_during_selection_halts(tmp_path: Path) -> None:
    """A revoked credential at selection records a Halt and ``halted`` (#250)."""

    class RevokedTracker(FakeTracker):
        def list_candidates(self, selection_label: str) -> list[CandidateIssue]:
            raise CredentialFailure("GET", "/repos/o/r/issues")

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        live = LiveView(process_started_at=_started_at())
        exit_code = run_worker_loop(
            tracker=RevokedTracker(agent_login=IDENTITY.login),
            ledger=ledger,
            loop_config=_loop_config(live),
            run_one_attempt=_never_runs(ledger),
            sleep=lambda seconds: None,
            max_sweeps=5,
        )

        assert exit_code == 1
        halt = _halt_row(ledger)
        assert halt is not None
        assert halt["reason"] == "credential-failure"
        assert halt["time"]
        events = _recent_event_types(ledger, live)
        assert "halted" in events
    finally:
        store.close()
        ledger.close()


def test_recovery_refused_halts(ledger: RunLedger) -> None:
    """An unrecoverable Attempt halts with ``recovery-refused`` (#250)."""
    ledger.open_attempt("#7/1", effective_configuration={})
    exit_code = run_worker_loop(
        tracker=FakeTracker(agent_login=IDENTITY.login),
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_never_runs(ledger),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    halt = _halt_row(ledger)
    assert halt is not None
    assert halt["reason"] == "recovery-refused"


def test_three_recovery_write_failures_halt(ledger: RunLedger) -> None:
    """Three consecutive recovery write failures halt the Worker (#250)."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)

    class FailingRelease(FakeTracker):
        def release_assignment(self, issue_number: int) -> None:
            raise GitHubFailure(
                "POST",
                f"/repos/o/r/issues/{issue_number}",
                500,
                reason="server-error",
                failure_class="github",
            )

    tracker = FailingRelease(agent_login=IDENTITY.login)
    tracker.candidates[7] = CandidateIssue(
        number=7,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=True,
        assignees=(IDENTITY.login,),
        labels=(SELECTION_LABEL,),
        created_at="2026-10-01T00:00:00Z",
    )
    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_never_runs(ledger),
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    halt = _halt_row(ledger)
    assert halt is not None
    assert halt["reason"] == "recovery-write-failure"


def test_claim_interrupted_after_attempt_opened_halts(ledger: RunLedger) -> None:
    """A Claim failure after the Attempt opened halts, leaving recovery (#250)."""

    class FailingAssign(FakeTracker):
        def assign_issue(self, issue_number: int) -> None:
            raise GitHubFailure(
                "PUT",
                f"/repos/o/r/issues/{issue_number}/assignees",
                500,
                reason="server-error",
                failure_class="github",
            )

    tracker = FailingAssign(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)
    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_never_runs(ledger),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    halt = _halt_row(ledger)
    assert halt is not None
    assert halt["reason"] == "claim-interrupted"


def test_three_infrastructure_failures_halt(ledger: RunLedger) -> None:
    """Three consecutive infrastructure-class failures halt after the third."""
    tracker = FakeTracker(agent_login=IDENTITY.login)
    for number in (7, 8, 9):
        tracker.candidates[number] = _candidate(number)

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ledger.record_terminal_outcome(
            claimed.attempt_id,
            outcome="failed",
            reason="infrastructure-failure: provider gateway timeout",
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id,
            outcome="failed",
            reason="infrastructure-failure: provider gateway timeout",
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    halt = _halt_row(ledger)
    assert halt is not None
    assert halt["reason"] == "infrastructure-failure"


def test_recorded_credential_failure_halts(ledger: RunLedger) -> None:
    """A recorded Credential Failure halts at once with a Halt (#250)."""
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="failed", reason="credential-failure"
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id,
            outcome="failed",
            reason="credential-failure",
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=5,
    )

    assert exit_code == 1
    halt = _halt_row(ledger)
    assert halt is not None
    assert halt["reason"] == "credential-failure"


def test_unsupported_environment_fails_attempt_without_halt(
    ledger: RunLedger,
) -> None:
    """An Unsupported Environment fails the Attempt and is not a Halt (#250)."""
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ledger.record_terminal_outcome(
            claimed.attempt_id,
            outcome="failed",
            reason="unsupported-environment: toolchain-mismatch",
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id,
            outcome="failed",
            reason="unsupported-environment: toolchain-mismatch",
        )

    live = LiveView(process_started_at=_started_at())
    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(live),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert _halt_row(ledger) is None
    assert "halted" not in _recent_event_types(ledger, live)
    assert live.is_ready()


def test_restart_reports_last_halt(tmp_path: Path) -> None:
    """A restart reports the previous process's Halt as ``last_halt`` (#250)."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        recorded = ledger.record_halt(
            reason="credential-failure", detail="credential failure; halting"
        )
        assert recorded["reason"] == "credential-failure"
    finally:
        store.close()
        ledger.close()
    # A restart: a new store on the same file, a new process start.
    restarted = OperatorStore(tmp_path / "run-ledger.sqlite3")
    try:
        restarted.write_instance_metadata(
            target_repository="example-owner/example-repo",
            process_started_at=datetime(2026, 11, 6, 9, 0, 0, tzinfo=timezone.utc),
        )
        result = _status(
            tmp_path / "run-ledger.sqlite3",
            LiveView(
                process_started_at=datetime(
                    2026, 11, 6, 9, 0, 0, tzinfo=timezone.utc
                )
            ),
        )
        last_halt = result["instance"]["last_halt"]
        assert last_halt is not None
        assert last_halt["reason"] == "credential-failure"
        assert last_halt["time"]
    finally:
        restarted.close()


# --- Contract Events ------------------------------------------------------------


def test_recent_events_capped_redacted(tmp_path: Path) -> None:
    """``recent_events`` holds at most 50 events, redacted and capped (#250)."""
    secret = "token-secret-value"
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(
        ledger_path, registry=SecretRegistry({"GITHUB_TOKEN": secret})
    )
    store = OperatorStore(ledger_path)
    try:
        store.write_instance_metadata(
            target_repository="example-owner/example-repo",
            process_started_at=_started_at(),
        )
        live = _live()
        assert live.process_started_at_iso() == _started_at().isoformat()
        for number in range(60):
            ledger.emit_contract_event(
                process_started_at=_started_at().isoformat(),
                event_type="probe",
                text=f"event {number} carries {secret} " + "x" * 500,
            )
        result = _status(ledger_path, live)
        recent = result["recent_events"]
        assert len(recent) == 50
        for entry in recent:
            assert len(entry["text"]) <= 300
            assert secret not in entry["text"]
            assert "«redacted:GITHUB_TOKEN»" in entry["text"]
        texts = [entry["text"] for entry in recent]
        assert not any(text.startswith("event 0 ") for text in texts)
        assert any(text.startswith("event 59 ") for text in texts)
        # Contract order: newest last.
        assert texts == sorted(
            texts, key=lambda text: int(text.split(" ")[1])
        )
    finally:
        store.close()
        ledger.close()


def test_command_commit_and_resolve_emit_events(tmp_path: Path) -> None:
    """Committing and resolving a command emits both Contract Events (#250)."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        live = _live()
        raw = json.dumps(
            {
                "op": "stop",
                "request_id": "req-halt-1",
                "expect_repo": "example-owner/example-repo",
            }
        ).encode()
        reply = operator_listener._handle_one(raw, ledger_path=ledger.path, live=live)
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "completed"
        result = _status(ledger.path, live)
        types = [entry["type"] for entry in result["recent_events"]]
        assert "command_accepted" in types
        assert "command_resolved" in types
    finally:
        store.close()
        ledger.close()


def test_command_events_redacted_through_registry(tmp_path: Path) -> None:
    """Command events on the listener path pass through the registry (#250)."""
    secret = "token-secret-value"
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        live = _live()
        registry = SecretRegistry({"GITHUB_TOKEN": secret})
        raw = json.dumps(
            {
                "op": "stop",
                "request_id": f"req-{secret}",
                "expect_repo": "example-owner/example-repo",
            }
        ).encode()
        reply = operator_listener._handle_one(
            raw, ledger_path=ledger.path, live=live, registry=registry
        )
        assert reply is not None
        result = _status(ledger.path, live)
        assert result["recent_events"]
        for entry in result["recent_events"]:
            assert secret not in entry["text"]
        assert any(
            "«redacted:GITHUB_TOKEN»" in entry["text"]
            for entry in result["recent_events"]
        )
    finally:
        store.close()
        ledger.close()


def test_invalid_configuration_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid configuration exits before the lock with nothing recorded."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    bad = _write_run_config(tmp_path, _RUN_CONFIG.replace("skill: implement", "skill: tdd"))
    exit_code = supervisor.run_worker(
        config=bad,
        workflow="implement",
        state_dir=tmp_path / "state",
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    assert not (tmp_path / "state" / "run-ledger.sqlite3").exists()


# --- Small helpers -----------------------------------------------------------------


def _never_runs(
    ledger: RunLedger,
) -> Any:
    """An Attempt runner that fails the test where the loop runs an Attempt."""

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        raise AssertionError(f"no Attempt should run (got {claimed.attempt_id})")

    _ = ledger
    return run_one


def _recent_event_types(ledger: RunLedger, live: LiveView) -> list[str]:
    connection = sqlite3.connect(str(ledger.path))
    try:
        events = list_recent_contract_events(
            connection, process_started_at=live.process_started_at_iso()
        )
    finally:
        connection.close()
    return [str(event["type"]) for event in events]
