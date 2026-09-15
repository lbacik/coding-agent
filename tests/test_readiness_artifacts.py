from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.implement.readiness import prepare_environment
from coding_agent.profile.readiness import evaluate_readiness, ProfileSources
from coding_agent.profile.services import ServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix, load_toolchain_matrix
from coding_agent.validate.runner import CommandRunner, ExecutedCommand


class FakeProber:
    """A `ServiceProber` fake: all services reachable."""

    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        return True


class FakeRunner:
    """A `CommandRunner` fake for testing artifact persistence."""

    def __init__(
        self,
        bootstrap_result: ExecutedCommand,
        test_all_result: ExecutedCommand,
        junit_content: str = "",
        evidence_dir: Path | None = None,
    ) -> None:
        self._bootstrap = bootstrap_result
        self._test_all = test_all_result
        self._junit_content = junit_content
        self._evidence_dir = evidence_dir
        self._call_count = 0

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        self._call_count += 1
        if self._call_count == 1:  # bootstrap
            return self._bootstrap
        # test_all - create JUnit file if junit_content provided
        if self._junit_content and self._evidence_dir:
            junit_path = self._evidence_dir / "readiness" / "junit.xml"
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            junit_path.write_text(self._junit_content, encoding="utf-8")
        return self._test_all


MINIMAL_PROFILE = """schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "python write_junit.py {evidence_dir}/junit.xml"
  test_targeted: "python write_junit.py {evidence_dir}/junit.xml"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/junit.xml"
  test_targeted: "{evidence_dir}/junit.xml"
checks: none
"""

TOOLCHAIN_MATRIX = {
    "schema": 1,
    "toolchains": {
        "python": {"version": "3.13.9", "package_manager": {"name": "uv", "version": "0.6.1"}},
    },
}


def test_persists_bootstrap_stdout_and_stderr_to_artifact_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_dir = workspace / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text(MINIMAL_PROFILE, encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    
    bootstrap_result = ExecutedCommand(
        command="true",
        exit_code=0,
        stdout="bootstrap stdout content",
        stderr="bootstrap stderr content",
    )
    test_all_result = ExecutedCommand(
        command="python write_junit.py evidence/readiness/junit.xml",
        exit_code=0,
        stdout="test_all stdout content",
        stderr="",
    )
    
    runner = FakeRunner(
        bootstrap_result,
        test_all_result,
        junit_content='<testsuite><testcase classname="pkg" name="passing"/></testsuite>',
        evidence_dir=evidence_dir,
    )
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    
    report = prepare_environment(
        workspace_path=workspace,
        evidence_dir=evidence_dir,
        matrix=matrix,
        service_env={},
        service_prober=FakeProber(),
        runner=runner,
    )
    
    assert report.classification == "ready"
    assert report.bootstrap_stdout_path is not None
    assert report.bootstrap_stderr_path is not None
    assert report.bootstrap_stdout_path.exists()
    assert report.bootstrap_stderr_path.exists()
    assert report.bootstrap_stdout_path.read_text(encoding="utf-8") == "bootstrap stdout content"
    assert report.bootstrap_stderr_path.read_text(encoding="utf-8") == "bootstrap stderr content"


def test_persists_test_all_stdout_and_stderr_to_artifact_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_dir = workspace / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text(MINIMAL_PROFILE, encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    
    bootstrap_result = ExecutedCommand(
        command="true",
        exit_code=0,
        stdout="",
        stderr="",
    )
    test_all_result = ExecutedCommand(
        command="python write_junit.py evidence/readiness/junit.xml",
        exit_code=0,
        stdout="test_all stdout content\nline 2",
        stderr="test_all stderr content",
    )
    
    runner = FakeRunner(
        bootstrap_result,
        test_all_result,
        junit_content='<testsuite><testcase classname="pkg" name="passing"/></testsuite>',
        evidence_dir=evidence_dir,
    )
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    
    report = prepare_environment(
        workspace_path=workspace,
        evidence_dir=evidence_dir,
        matrix=matrix,
        service_env={},
        service_prober=FakeProber(),
        runner=runner,
    )
    
    assert report.classification == "ready"
    assert report.test_all_stdout_path is not None
    assert report.test_all_stderr_path is not None
    assert report.test_all_stdout_path.exists()
    assert report.test_all_stderr_path.exists()
    assert report.test_all_stdout_path.read_text(encoding="utf-8") == "test_all stdout content\nline 2"
    assert report.test_all_stderr_path.read_text(encoding="utf-8") == "test_all stderr content"


def test_artifact_paths_are_relative_to_artifact_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_dir = workspace / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text(MINIMAL_PROFILE, encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    
    bootstrap_result = ExecutedCommand(command="true", exit_code=0, stdout="out", stderr="err")
    test_all_result = ExecutedCommand(command="test", exit_code=0, stdout="test out", stderr="test err")
    
    runner = FakeRunner(
        bootstrap_result,
        test_all_result,
        junit_content='<testsuite><testcase classname="pkg" name="passing"/></testsuite>',
        evidence_dir=evidence_dir,
    )
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    
    report = prepare_environment(
        workspace_path=workspace,
        evidence_dir=evidence_dir,
        matrix=matrix,
        service_env={},
        service_prober=FakeProber(),
        runner=runner,
    )
    
    artifact_dir = evidence_dir / "readiness"
    
    assert report.bootstrap_stdout_path == artifact_dir / "bootstrap.stdout"
    assert report.bootstrap_stderr_path == artifact_dir / "bootstrap.stderr"
    assert report.test_all_stdout_path == artifact_dir / "test_all.stdout"
    assert report.test_all_stderr_path == artifact_dir / "test_all.stderr"


def test_artifact_paths_are_none_when_bootstrap_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_dir = workspace / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text(MINIMAL_PROFILE, encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    
    bootstrap_result = ExecutedCommand(
        command="false",
        exit_code=1,
        stdout="bootstrap failed",
        stderr="error message",
    )
    # test_all won't run, but we need a placeholder
    test_all_result = ExecutedCommand(command="", exit_code=0, stdout="", stderr="")
    
    runner = FakeRunner(bootstrap_result, test_all_result)
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    
    report = prepare_environment(
        workspace_path=workspace,
        evidence_dir=evidence_dir,
        matrix=matrix,
        service_env={},
        service_prober=FakeProber(),
        runner=runner,
    )
    
    assert report.classification == "bootstrap-failed"
    assert report.bootstrap_stdout_path is not None
    assert report.bootstrap_stderr_path is not None
    # test_all artifacts should be None since it never ran
    assert report.test_all_stdout_path is None
    assert report.test_all_stderr_path is None
    # Verify the bootstrap artifacts were still persisted
    assert report.bootstrap_stdout_path.read_text(encoding="utf-8") == "bootstrap failed"
    assert report.bootstrap_stderr_path.read_text(encoding="utf-8") == "error message"


def test_runnable_red_persists_both_command_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_dir = workspace / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text(MINIMAL_PROFILE, encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    
    bootstrap_result = ExecutedCommand(command="true", exit_code=0, stdout="bootstrap ok", stderr="")
    test_all_result = ExecutedCommand(
        command="test",
        exit_code=1,
        stdout="test failed output",
        stderr="failure details",
    )
    
    runner = FakeRunner(
        bootstrap_result,
        test_all_result,
        junit_content='<testsuite><testcase classname="pkg" name="broken"><failure/></testcase></testsuite>',
        evidence_dir=evidence_dir,
    )
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    
    report = prepare_environment(
        workspace_path=workspace,
        evidence_dir=evidence_dir,
        matrix=matrix,
        service_env={},
        service_prober=FakeProber(),
        runner=runner,
    )
    
    assert report.classification == "runnable-red"
    assert report.bootstrap_stdout_path is not None
    assert report.bootstrap_stderr_path is not None
    assert report.test_all_stdout_path is not None
    assert report.test_all_stderr_path is not None
    assert report.test_all_stdout_path.read_text(encoding="utf-8") == "test failed output"
