"""Startup refusal resource cleanup (issue #453, slice B of #434).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4. Startup
owns every resource it opens on an ``ExitStack``: a refusal unwinds every
open resource in reverse order, checkpointing the ledger before closing it.
A refusal at ``run-ledger`` leaves the volume lock released, the listener
stopped and the ledger checkpointed and closed; a refusal at
``operator-listener`` leaves the lock released and the store closed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from coding_agent.identity.startup import Identity, StartupCheckResult
from coding_agent.ledger.store import RunLedger as RealRunLedger
from coding_agent.operator.listener import OperatorListener
from coding_agent.operator.paths import worker_socket_path
from coding_agent.operator.store import OperatorStore as RealOperatorStore
from coding_agent.worker import startup as startup_module
from coding_agent.worker import supervisor
from coding_agent.worker.volume import acquire_volume_lock
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration
from support.fake_step_tracker import FakeStepTracker
from support.run_inputs import make_run_inputs
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

_RUN_CONFIG = """\
version: 3

target:
  kind: github
  repository: example-owner/example-repo
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: sonnet
    input: {type: issue, label: agent:implement}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      work:
        agent:
          tools: [workspace-read]
          results:
            done: {}
        routes: {done: {end: completed}}
"""


def _write_run_config(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(_RUN_CONFIG, encoding="utf-8")
    return path


def _write_run_matrix(tmp_path: Path) -> Path:
    path = tmp_path / "toolchain-matrix.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_manager": {"name": "uv", "version": "0.12.8"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _stub_startup_collaborators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the live startup checks the way the other startup tests do."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def _bundle(**kwargs: object) -> object:
        loaded = kwargs["config"]
        assert isinstance(loaded, LoadedWorkflowConfiguration)
        return make_run_inputs(loaded)

    def _github(*args: object, **kwargs: object) -> StartupCheckResult:
        return StartupCheckResult(
            identity=IDENTITY,
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        )

    monkeypatch.setattr(supervisor, "verify_run_inputs", _bundle)
    monkeypatch.setattr(supervisor, "run_startup_checks", _github)
    monkeypatch.setattr(
        supervisor, "build_chat_model", lambda pin, **kwargs: ScriptedModel([])
    )
    monkeypatch.setattr(
        supervisor, "assert_provider_capability", lambda *args, **kwargs: None
    )


def _tracker_factory() -> FakeStepTracker:
    return FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )


def _halt_row(state_dir: Path) -> tuple[str, str]:
    connection = sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        row = connection.execute(
            "SELECT reason, detail FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0]), str(row[1])


class _RecordingLedger(RealRunLedger):
    """The real ledger, recording its checkpoint and close calls."""

    checkpoint_calls: ClassVar[list[str]] = []
    close_calls: ClassVar[list[str]] = []


class _RecordingStore(RealOperatorStore):
    """The real operator store, recording its close calls."""

    close_calls: ClassVar[list[str]] = []


def _reset_recording() -> None:
    _RecordingLedger.checkpoint_calls.clear()
    _RecordingLedger.close_calls.clear()
    _RecordingStore.close_calls.clear()


def test_refusal_at_run_ledger_releases_lock_stops_listener_and_closes_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run-ledger refusal unwinds everything in reverse order (§4, §17)."""
    _stub_startup_collaborators(monkeypatch)
    _reset_recording()

    def _checkpoint(self: _RecordingLedger) -> None:
        _RecordingLedger.checkpoint_calls.append("checkpoint")
        super(_RecordingLedger, self).checkpoint()

    def _close_ledger(self: _RecordingLedger) -> None:
        _RecordingLedger.close_calls.append("close")
        super(_RecordingLedger, self).close()

    def _close_store(self: _RecordingStore) -> None:
        _RecordingStore.close_calls.append("close")
        super(_RecordingStore, self).close()

    monkeypatch.setattr(_RecordingLedger, "checkpoint", _checkpoint)
    monkeypatch.setattr(_RecordingLedger, "close", _close_ledger)
    monkeypatch.setattr(_RecordingStore, "close", _close_store)
    monkeypatch.setattr(supervisor, "RunLedger", _RecordingLedger)
    monkeypatch.setattr(startup_module, "OperatorStore", _RecordingStore)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    seed = RealRunLedger(state_dir / "run-ledger.sqlite3")
    try:
        seed.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    finally:
        seed.close()
    connection = sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        connection.execute(
            "UPDATE attempts SET effective_configuration = 'not-json'"
            " WHERE attempt_id = 'implement#7/1'"
        )
        connection.commit()
    finally:
        connection.close()

    exit_code = supervisor.run_worker(
        config=_write_run_config(tmp_path),
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        tracker_factory=lambda **kwargs: _tracker_factory(),
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "run: startup refused at run-ledger after " in err
    # The listener stopped: its socket is gone.
    assert not worker_socket_path(state_dir).exists()
    # The ledger checkpointed before it closed, exactly once each.
    assert _RecordingLedger.checkpoint_calls == ["checkpoint"]
    assert _RecordingLedger.close_calls == ["close"]
    # The store closed too, so the process released everything it opened.
    assert _RecordingStore.close_calls == ["close"]
    # The volume lock released: a fresh acquire succeeds.
    released = acquire_volume_lock(state_dir)
    released.release()
    # The Halt is durable with the run-ledger reason.
    reason, detail = _halt_row(state_dir)
    assert reason == "recovery-refused"
    assert "cannot read the Run Ledger" in detail


class _FailingListener(OperatorListener):
    """A listener whose bind fails, refusing startup at operator-listener."""

    def start(self) -> None:
        raise OSError("cannot bind in test")


def test_refusal_at_operator_listener_releases_lock_and_closes_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator-listener refusal leaves the lock released and closed (§4)."""
    _stub_startup_collaborators(monkeypatch)
    _reset_recording()

    def _close_store(self: _RecordingStore) -> None:
        _RecordingStore.close_calls.append("close")
        super(_RecordingStore, self).close()

    monkeypatch.setattr(_RecordingStore, "close", _close_store)
    monkeypatch.setattr(startup_module, "OperatorStore", _RecordingStore)
    monkeypatch.setattr(startup_module, "OperatorListener", _FailingListener)

    def _no_ledger(*args: object, **kwargs: object) -> RealRunLedger:
        raise AssertionError("the Run Ledger must never open")

    monkeypatch.setattr(supervisor, "RunLedger", _no_ledger)

    state_dir = tmp_path / "state"

    exit_code = supervisor.run_worker(
        config=_write_run_config(tmp_path),
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        tracker_factory=lambda **kwargs: _tracker_factory(),
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "run: startup refused at operator-listener after " in err
    assert "cannot bind the operator socket" in err
    # The listener never bound, so no socket lingers.
    assert not worker_socket_path(state_dir).exists()
    # The store opened before the listener, so it closed on unwind.
    assert _RecordingStore.close_calls == ["close"]
    # The volume lock released: a fresh acquire succeeds.
    released = acquire_volume_lock(state_dir)
    released.release()
    # The Halt is durable with the startup-check reason.
    reason, detail = _halt_row(state_dir)
    assert reason == "startup-check-refused"
    assert "cannot bind the operator socket" in detail
