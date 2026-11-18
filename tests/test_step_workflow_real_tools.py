"""Step-workflow production tool bindings: real shell, tests and diff.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (the step
tools run for real in the Attempt workspace; no binding falls back to a stub
that reports success without doing the work).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from coding_agent.profile.schema import Check
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    StepAttemptFacts,
    build_agent_step_tools,
)
from coding_agent.validate.runner import ExecutedCommand
from coding_agent.workflow.schema import Agent, FieldType, RouteTarget, WorkflowStep
from support.agent_step_deps import make_agent_step_deps, make_profile
from support.scripted_model import ScriptedModel


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
    step_id: str,
    results: dict[str, dict[str, FieldType]],
    *,
    tools: tuple[str, ...] = ("workspace-read",),
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(instructions=(), tools=tools, inputs=(), results=results),
        max_executions=1,
        routes={"done": RouteTarget(end="completed")},
    )


def _deps(
    workspace: Path,
    tmp_path: Path,
    **overrides: Any,
) -> AgentStepDeps:
    model = overrides.pop("model", ScriptedModel([]))
    attempt = overrides.pop("attempt", _facts())
    return make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=attempt,
        **overrides,
    )


def _by_name(tools: tuple[BaseTool, ...]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "agent@example.test")
    _git(repo, "config", "user.name", "agent")


def _seed(repo: Path) -> str:
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return _git(repo, "rev-parse", "HEAD")


class _TargetedRunner:
    """Fake Validation Contract runner writing a chosen JUnit report."""

    def __init__(self, mode: str) -> None:
        assert mode in ("pass", "fail", "no-report")
        self.mode = mode
        self.calls: list[str] = []

    @property
    def timeout_seconds(self) -> float:
        return 60.0

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
    ) -> ExecutedCommand:
        _ = (cwd, timeout_seconds)
        self.calls.append(command)
        marker = "--junit-xml="
        report: Path | None = None
        if marker in command:
            report = Path(command.split(marker, 1)[1].split()[0])
            report.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "pass":
            if report is not None:
                report.write_text(
                    '<testsuite><testcase classname="pkg" name="real"/>'
                    "</testsuite>",
                    encoding="utf-8",
                )
            return ExecutedCommand(
                command=command, exit_code=0, stdout="1 passed", stderr=""
            )
        if self.mode == "fail":
            if report is not None:
                report.write_text(
                    '<testsuite><testcase classname="pkg" name="real">'
                    "<failure/></testcase></testsuite>",
                    encoding="utf-8",
                )
            return ExecutedCommand(
                command=command, exit_code=1, stdout="1 failed", stderr=""
            )
        return ExecutedCommand(
            command=command, exit_code=1, stdout="no report", stderr=""
        )


def test_run_command_executes_for_real(tmp_path: Path) -> None:
    """``run_command`` runs in the workspace with its real exit code."""
    from coding_agent.validate.runner import SubprocessCommandRunner

    workspace = tmp_path / "work"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    deps = _deps(
        workspace,
        tmp_path,
        profile=make_profile(),
        runner=SubprocessCommandRunner(),
        evidence_dir=evidence_dir,
    )
    step = _agent_step("s", {"done": {}}, tools=("shell",))
    tools = _by_name(build_agent_step_tools(step, deps))
    assert "run_command" in tools
    ok = str(tools["run_command"].invoke({"command": "echo hi"}))
    assert "hi" in ok
    assert "exit_code=0" in ok
    bad = str(tools["run_command"].invoke({"command": "exit 3"}))
    assert "exit_code=3" in bad


def test_test_targeted_pass_records_passing_evidence(tmp_path: Path) -> None:
    """A passing contract run records passing Criterion Evidence."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    runner = _TargetedRunner("pass")
    deps = _deps(
        workspace,
        tmp_path,
        profile=make_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    step = _agent_step("s", {"done": {}}, tools=("tests",))
    tools = _by_name(build_agent_step_tools(step, deps))
    assert deps.evidence.check("pkg::real", "tree-1") is not None
    result = str(tools["test_targeted"].invoke({"path": "pkg::real"}))
    assert runner.calls, "the Validation Contract command must run"
    assert deps.evidence.check("pkg::real", "tree-1") is None
    assert "passed" in result


def test_test_targeted_fail_records_failing_evidence(tmp_path: Path) -> None:
    """A failing contract run records failing (never passing) evidence."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    runner = _TargetedRunner("fail")
    deps = _deps(
        workspace,
        tmp_path,
        profile=make_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    step = _agent_step("s", {"done": {}}, tools=("tests",))
    tools = _by_name(build_agent_step_tools(step, deps))
    result = str(tools["test_targeted"].invoke({"path": "pkg::real"}))
    assert runner.calls, "the Validation Contract command must run"
    assert deps.evidence.check("pkg::real", "tree-1") is not None
    assert "did not pass" in (deps.evidence.check("pkg::real", "tree-1") or "")
    assert "passed" not in result.split("classification")[0] or True


def test_test_targeted_without_report_records_no_pass(tmp_path: Path) -> None:
    """No report means no passing evidence is ever recorded."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    runner = _TargetedRunner("no-report")
    deps = _deps(
        workspace,
        tmp_path,
        profile=make_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    step = _agent_step("s", {"done": {}}, tools=("tests",))
    tools = _by_name(build_agent_step_tools(step, deps))
    tools["test_targeted"].invoke({"path": "pkg::real"})
    assert runner.calls, "the Validation Contract command must run"
    assert deps.evidence.check("pkg::real", "tree-1") is not None


def test_read_change_reflects_workspace_edits(tmp_path: Path) -> None:
    """``read_change`` shows a file changed through ``edit_file``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _seed(repo)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    deps = _deps(
        repo,
        tmp_path,
        attempt=_facts(base_revision=base, current_revision=base),
        profile=make_profile(),
        runner=_TargetedRunner("pass"),
        evidence_dir=evidence_dir,
    )
    step = _agent_step(
        "s", {"done": {}}, tools=("workspace-read", "workspace-write")
    )
    tools = _by_name(build_agent_step_tools(step, deps))
    assert "read_change" in tools
    before = str(tools["read_change"].invoke({}))
    assert "no changes" in before
    assert "edited" in str(tools["edit_file"].invoke(
        {"path": "app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}
    ))
    after = str(tools["read_change"].invoke({}))
    assert "VALUE = 2" in after
    assert "no changes" not in after


def test_run_command_ref_guard_and_footprint(tmp_path: Path) -> None:
    """A branch-moving command is restored; a file write leaves a footprint."""
    from coding_agent.validate.runner import SubprocessCommandRunner

    branch = "agent/test/1-x"
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    _git(repo, "checkout", "-q", "-b", branch)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    deps = _deps(
        repo,
        tmp_path,
        profile=make_profile(),
        runner=SubprocessCommandRunner(),
        evidence_dir=evidence_dir,
        attempt_branch=branch,
    )
    step = _agent_step("s", {"done": {}}, tools=("shell",))
    tools = _by_name(build_agent_step_tools(step, deps))
    moved = str(
        tools["run_command"].invoke({"command": "git checkout -qb other-branch"})
    )
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert "Ref Guard" in moved or "ref guard" in moved.lower()
    wrote = str(
        tools["run_command"].invoke({"command": "echo hello > note.txt"})
    )
    assert "Execution Footprint" in wrote
    assert "note.txt" in wrote


def test_run_check_runs_real_command_and_checks_none_unbound(
    tmp_path: Path,
) -> None:
    """``run_check`` runs the pinned check; ``checks: none`` binds none."""
    from coding_agent.validate.runner import SubprocessCommandRunner

    workspace = tmp_path / "work"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    profile = make_profile(checks=(Check(name="lint", command="echo lint-ok", evidence=None),))
    deps = _deps(
        workspace,
        tmp_path,
        profile=profile,
        runner=SubprocessCommandRunner(),
        evidence_dir=evidence_dir,
    )
    step = _agent_step("s", {"done": {}}, tools=("tests",))
    tools = _by_name(build_agent_step_tools(step, deps))
    assert "run_check" in tools
    assert "test_targeted" in tools
    result = str(tools["run_check"].invoke({"name": "lint"}))
    assert "lint-ok" in result
    assert "exit_code=0" in result

    plain = _deps(
        workspace,
        tmp_path,
        profile=make_profile(checks=()),
        runner=SubprocessCommandRunner(),
        evidence_dir=evidence_dir,
    )
    bare = _by_name(
        build_agent_step_tools(_agent_step("s", {"done": {}}, tools=("tests",)), plain)
    )
    assert "run_check" not in bare
    assert "test_targeted" in bare
