from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coding_agent.runtime.result_capping import InMemoryArtifactStore
from coding_agent.profile.schema import Evidence, ProjectProfile, Toolchain
from coding_agent.validate.diagnostics import TargetedTestAdapter
from coding_agent.validate.harness import CommandContext, run_validation_contract
from coding_agent.validate.results import classify
from coding_agent.validate.runner import (
    COMMAND_TIMEOUT_EXIT_CODE,
    CredentialStrippedCommandRunner,
    ExecutedCommand,
    SubprocessCommandRunner,
    kill_process_group,
    run_command_with_timeout,
)


def _profile_for(command: str) -> ProjectProfile:
    return ProjectProfile(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="true",
        test_all="true",
        test_targeted=command,
        evidence=Evidence(
            format="junit-xml",
            test_all="{evidence_dir}/test_all.xml",
            test_targeted="{evidence_dir}/test_targeted.xml",
        ),
        checks=(),
        services=(),
    )


def _profile_test_all(command: str) -> ProjectProfile:
    return dataclasses.replace(_profile_for("true"), test_all=command)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_fast_command_does_not_time_out(tmp_path: Path) -> None:
    runner = SubprocessCommandRunner(timeout_seconds=10.0)
    result = runner.run("echo hello", cwd=tmp_path)
    assert result.timed_out is False
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_timeout_kills_shell_child_and_reports_result(tmp_path: Path) -> None:
    runner = SubprocessCommandRunner(timeout_seconds=0.3)
    started = time.monotonic()
    result = runner.run("sleep 30", cwd=tmp_path)
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert result.exit_code == COMMAND_TIMEOUT_EXIT_CODE
    assert "timed out" in (result.stdout + result.stderr).lower()
    assert elapsed < 10.0


def test_timeout_kills_grandchild_with_whole_process_group(tmp_path: Path) -> None:
    """P-CMD-1: a hanging grandchild dies with its process group at the timeout."""
    # A generous timeout: the parent must have started (and recorded the
    # grandchild pid) before the kill, so the timeout must comfortably exceed
    # interpreter startup even on a loaded machine. The pid goes to a file
    # rather than stdout so the assertion never depends on pipe timing.
    runner = SubprocessCommandRunner(timeout_seconds=5.0)
    command = (
        "python3 -c \"import subprocess, time; "
        "p = subprocess.Popen(['sleep', '60']); "
        "open('grandchild.pid', 'w').write(str(p.pid)); time.sleep(60)\""
    )
    started = time.monotonic()
    result = runner.run(command, cwd=tmp_path)
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 20.0
    pid_file = tmp_path / "grandchild.pid"
    assert pid_file.is_file()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    # Give the group kill a moment to reap, then the grandchild must be gone.
    for _ in range(50):
        if not _process_alive(grandchild_pid):
            break
        time.sleep(0.05)
    assert not _process_alive(grandchild_pid)


def test_credential_stripped_runner_times_out_and_strips_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secret")
    runner = CredentialStrippedCommandRunner(["GITHUB_TOKEN"], timeout_seconds=0.3)
    leaked = runner.run(
        "python3 -c \"import os; print('leaked' if 'GITHUB_TOKEN' in os.environ else 'clean')\"",
        cwd=tmp_path,
    )
    assert leaked.timed_out is False
    assert "clean" in leaked.stdout
    hung = runner.run("sleep 30", cwd=tmp_path)
    assert hung.timed_out is True
    assert hung.exit_code == COMMAND_TIMEOUT_EXIT_CODE


def test_runner_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        SubprocessCommandRunner(timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        CredentialStrippedCommandRunner(["X"], timeout_seconds=-1.0)


def test_kill_process_group_on_dead_pid_is_a_no_op() -> None:
    kill_process_group(1 << 30)


def test_run_command_with_timeout_helper_reports_timeout(tmp_path: Path) -> None:
    result = run_command_with_timeout("sleep 30", cwd=tmp_path, env=None, timeout_seconds=0.2)
    assert result.timed_out is True
    assert result.exit_code == COMMAND_TIMEOUT_EXIT_CODE


def test_harness_propagates_timeout_as_red_without_evidence(tmp_path: Path) -> None:
    """P-CMD-1 for runtime commands: the timeout is classified by existing rules."""

    class _HangingRunner:
        def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
            real = SubprocessCommandRunner(timeout_seconds=0.2)
            return real.run("sleep 30", cwd=cwd)

    profile = _profile_test_all("sleep 30")
    context = CommandContext(_HangingRunner(), tmp_path, tmp_path / "evidence")
    (result,) = run_validation_contract(profile, context)
    assert result.timed_out is True
    assert result.exit_code == COMMAND_TIMEOUT_EXIT_CODE
    assert classify(result) in {"missing-evidence", "unnamed-failure"}


def _adapter_with_runner(
    tmp_path: Path, runner: object, *, limit: int = 4000
) -> TargetedTestAdapter:
    context = CommandContext(runner, tmp_path, tmp_path, workspace_root=tmp_path)  # type: ignore[arg-type]
    return TargetedTestAdapter(
        _profile_for("sleep 30"),
        context,
        artifact_store=InMemoryArtifactStore(),
        inline_limit=limit,
        attempt_id="99/1",
    )


def test_timeout_is_a_tool_result_with_timeout_classification(tmp_path: Path) -> None:
    """L3-ERR-14: a hung model command returns a timeout tool result, not a raise."""

    class _TimeoutRunner:
        def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
            return ExecutedCommand(
                command=command,
                exit_code=COMMAND_TIMEOUT_EXIT_CODE,
                stdout=f"{command}\ncommand timed out after 0.2s; killed the whole process group\n",
                stderr="",
                timed_out=True,
            )

    payload = json.loads(_adapter_with_runner(tmp_path, _TimeoutRunner()).run("tests/hung.py"))
    assert payload["classification"] == "timeout"
    assert payload["suppressed"] is False
    assert payload["stop_loop"] is False
    assert payload["junit"] is None
    assert "timed out" in (
        payload["stdout"]["excerpt"] + payload["stderr"]["excerpt"]
    ).lower()


def test_timeout_tool_result_end_to_end_through_real_runner(tmp_path: Path) -> None:
    """L3-ERR-14 over a real process group: the tool returns, so the next turn is sent."""
    runner = SubprocessCommandRunner(timeout_seconds=0.3)
    adapter = _adapter_with_runner(tmp_path, runner)
    payload = json.loads(adapter.run("tests/hung.py"))
    assert payload["classification"] == "timeout"
    assert payload["exit_status"] == COMMAND_TIMEOUT_EXIT_CODE
    # The model reacts on the next turn: a second call still runs and returns.
    second = json.loads(adapter.run("tests/hung.py"))
    assert second["classification"] in {"timeout", "infrastructure_failure"}


def test_repeated_timeout_suppresses_then_stops(tmp_path: Path) -> None:
    class _TimeoutRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
            self.calls += 1
            return ExecutedCommand(
                command=command,
                exit_code=COMMAND_TIMEOUT_EXIT_CODE,
                stdout="command timed out after 0.2s; killed the whole process group\n",
                stderr="",
                timed_out=True,
            )

    runner = _TimeoutRunner()
    adapter = _adapter_with_runner(tmp_path, runner)
    first = json.loads(adapter.run("tests/hung.py"))
    second = json.loads(adapter.run("tests/hung.py"))
    assert first["suppressed"] is False
    assert second["suppressed"] is True
    assert runner.calls == 1


def test_no_sleep_process_survives_the_group_kill() -> None:
    before = subprocess.run(
        ["bash", "-c", "ps -eo args | grep '[s]leep 60' || true"],
        capture_output=True,
        text=True,
    )
    assert "sleep 60" not in before.stdout
