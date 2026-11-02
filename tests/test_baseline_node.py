"""The ``baseline`` node: the pinned Validation Contract before any model call.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (after Readiness,
``baseline`` runs ``bootstrap``, then ``test_all`` and every check, on a tree
at the Base Revision, before any model call; a bootstrap failure is an
infrastructure-class failure of class ``setup``; a red baseline ends
``failed`` / ``red-baseline`` naming the failing commands and, where evidence
provides them, the individual failure identifiers; a suite that executes zero
tests and exits zero is not red; a check without an ``evidence`` block is
judged by its exit code alone; Baseline Failure exclusion is deferred) and
§14 (the Terminal Outcome is recorded before the terminal writes).

MVP row ids: P-RDY-4, C-RDY-3, C-RDY-4, L2-1.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.substitution import render_command
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.validate.runner import ExecutedCommand
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

BASE_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

PROFILE_WITH_CHECKS = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "bootstrap-cmd"
  test_all: "test-all-cmd --junit {evidence_dir}/test_all.xml"
  test_targeted: "test-targeted-cmd --junit {evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: types
    command: "types-cmd"
  - name: lint
    command: "lint-cmd --out {evidence_dir}/lint.xml"
    evidence: "{evidence_dir}/lint.xml"
services: []
"""

PROFILE_PHP_STYLE = """\
schema: 2
language: php
working_directory: app
toolchain:
  php: "8.4"
  package_manager: composer
commands:
  bootstrap: "composer install --no-interaction --no-progress"
  test_all: "vendor/bin/phpunit --log-junit {evidence_dir}/test_all.xml"
  test_targeted: "vendor/bin/phpunit --log-junit {evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: syntax
    command: "php -l src/Greeting.php"
services: []
"""

PROFILE_TS_STYLE = """\
schema: 2
language: typescript
working_directory: .
toolchain:
  node: "22"
  package_manager: pnpm
commands:
  bootstrap: "pnpm install --frozen-lockfile"
  test_all: "pnpm vitest run --reporter=junit --outputFile={evidence_dir}/test_all.xml"
  test_targeted: "pnpm vitest run --reporter=junit --outputFile={evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: types
    command: "pnpm tsc --noEmit"
services: []
"""

GOOD_BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"

PASSING_JUNIT = (
    '<testsuite name="suite" tests="1">'
    '<testcase classname="pkg" name="test_ok"/>'
    "</testsuite>"
)

FAILING_JUNIT = (
    '<testsuite name="suite" tests="2">'
    '<testcase classname="pkg" name="test_ok"/>'
    '<testcase classname="pkg" name="test_shout">'
    '<failure message="boom"/>'
    "</testcase>"
    "</testsuite>"
)

EMPTY_JUNIT = '<testsuite name="suite" tests="0"></testsuite>'


def completion_message() -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"summary": "did it"},
                "id": "call_completion_1",
                "type": "tool_call",
            }
        ],
    )


@dataclass
class ScriptedCall:
    """One scripted command run, in call order: the exit code plus the JUnit
    evidence file the command would have written, if any."""

    exit_code: int
    evidence_path: Path | None = None
    evidence_text: str | None = None


class ScriptedRunner:
    """A ``CommandRunner`` fake scripted by call order: the first ``run`` is
    the bootstrap, the second ``test_all``, then each check in profile order.

    Order-keyed rather than command-keyed so profiles whose bootstrap and
    ``test_all`` render to the same string (``"true"`` in the happy-path
    profile) still script independently.
    """

    def __init__(self, calls: list[ScriptedCall]) -> None:
        self._calls = list(calls)
        self.commands: list[str] = []
        self.cwds: list[Path] = []

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        self.cwds.append(cwd)
        self.commands.append(command)
        script = self._calls.pop(0)
        if script.evidence_path is not None and script.evidence_text is not None:
            script.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            script.evidence_path.write_text(script.evidence_text, encoding="utf-8")
        return ExecutedCommand(command=command, exit_code=script.exit_code, stdout="", stderr="")


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(BASE_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def commit_profile(workspace: Path, text: str) -> None:
    path = workspace / "docs" / "agents" / "project-profile.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", "update profile"], workspace)


def invoke(
    ledger: RunLedger,
    workspace: Path,
    runner: ScriptedRunner,
    tmp_path: Path,
) -> tuple[AttemptResult, FakeTracker, ScriptedModel]:
    tracker = FakeTracker()
    model = ScriptedModel([completion_message()])
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        command_runner=runner,
        evidence_dir=tmp_path / "evidence",
    )
    return result, tracker, model


def test_c_rdy_3_bootstrap_failure_ends_infrastructure_failure_without_a_model_call(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    runner = ScriptedRunner([ScriptedCall(exit_code=1)])
    result, tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "infrastructure-failure" in result.reason
    assert "setup" in result.reason
    assert "bootstrap" in result.reason
    assert model.invocations == []
    assert runner.commands == ["true"]
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert outcome.outcome == "failed"
    assert "setup" in outcome.reason
    assert tracker.call_names() == [
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ]


def test_p_rdy_4_red_test_all_names_the_command_and_the_failing_tests(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence" / "test_all.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=1, evidence_path=evidence, evidence_text=FAILING_JUNIT),
        ]
    )
    result, tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "red-baseline" in result.reason
    assert "test_all" in result.reason
    assert "pkg::test_shout" in result.reason
    assert model.invocations == []
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert "red-baseline" in outcome.reason
    assert "pkg::test_shout" in outcome.reason
    assert tracker.call_names() == [
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ]


def test_p_rdy_4_reported_failure_is_red_despite_exit_zero(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence" / "test_all.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=evidence, evidence_text=FAILING_JUNIT),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "red-baseline" in result.reason
    assert "pkg::test_shout" in result.reason
    assert model.invocations == []


def test_p_rdy_4_red_evidenced_check_names_the_check_and_its_failures(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    commit_profile(workspace, PROFILE_WITH_CHECKS)
    evidence_dir = tmp_path / "evidence"
    test_all_xml = evidence_dir / "test_all.xml"
    lint_xml = evidence_dir / "lint.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=test_all_xml, evidence_text=PASSING_JUNIT),
            ScriptedCall(exit_code=0),
            ScriptedCall(
                exit_code=1, evidence_path=lint_xml, evidence_text=FAILING_JUNIT
            ),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "red-baseline" in result.reason
    assert "lint" in result.reason
    assert "pkg::test_shout" in result.reason
    assert "types" not in result.reason
    assert model.invocations == []
    assert runner.commands == [
        "bootstrap-cmd",
        f"test-all-cmd --junit {evidence_dir}/test_all.xml",
        "types-cmd",
        f"lint-cmd --out {evidence_dir}/lint.xml",
    ]


def test_p_rdy_4_red_unevidenced_check_is_judged_by_its_exit_code(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    commit_profile(workspace, PROFILE_WITH_CHECKS)
    evidence_dir = tmp_path / "evidence"
    test_all_xml = evidence_dir / "test_all.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=test_all_xml, evidence_text=PASSING_JUNIT),
            ScriptedCall(exit_code=1),
            ScriptedCall(exit_code=0),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "red-baseline" in result.reason
    assert "types" in result.reason
    assert "failures:" not in result.reason
    assert model.invocations == []


def test_c_rdy_4_zero_tests_with_exit_zero_is_not_red(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence" / "test_all.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=evidence, evidence_text=EMPTY_JUNIT),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "delivered"
    assert len(model.invocations) == 1


def test_l2_1_baseline_runs_every_command_and_records_validation_evidence(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    commit_profile(workspace, PROFILE_WITH_CHECKS)
    evidence_dir = tmp_path / "evidence"
    test_all_xml = evidence_dir / "test_all.xml"
    lint_xml = evidence_dir / "lint.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=test_all_xml, evidence_text=FAILING_JUNIT),
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=lint_xml, evidence_text=PASSING_JUNIT),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "test_all" in result.reason
    assert model.invocations == []
    assert runner.commands == [
        "bootstrap-cmd",
        render_command(
            "test-all-cmd --junit {evidence_dir}/test_all.xml", evidence_dir=evidence_dir
        ),
        "types-cmd",
        render_command("lint-cmd --out {evidence_dir}/lint.xml", evidence_dir=evidence_dir),
    ]
    records = ledger.records("#7/1")
    baselines = [record for record in records if record.kind == "baseline"]
    assert len(baselines) == 1
    payload = baselines[0].payload
    assert isinstance(payload, dict)
    bootstrap = payload["bootstrap"]
    assert isinstance(bootstrap, dict)
    assert bootstrap["name"] == "bootstrap"
    assert bootstrap["exit_code"] == 0
    commands = payload["commands"]
    assert isinstance(commands, list)
    by_name = {command["name"]: command for command in commands}
    assert set(by_name) == {"test_all", "types", "lint"}
    assert by_name["test_all"]["exit_code"] == 0
    assert by_name["test_all"]["executed"] == 2
    assert by_name["test_all"]["failure_ids"] == ["pkg::test_shout"]
    assert by_name["types"]["exit_code"] == 0
    assert by_name["types"]["executed"] is None
    assert by_name["types"]["failure_ids"] is None
    assert by_name["lint"]["exit_code"] == 0
    assert by_name["lint"]["executed"] == 1
    assert by_name["lint"]["failure_ids"] == []


def test_l2_1_baseline_runs_a_php_style_profile_in_its_working_directory(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """L2-1 beyond Python: a composer/PHPUnit profile runs its bootstrap,
    ``test_all`` and checks in the declared working area, and PHPUnit failure
    identifiers are recorded like pytest's."""
    commit_profile(workspace, PROFILE_PHP_STYLE)
    evidence_dir = tmp_path / "evidence"
    test_all_xml = evidence_dir / "test_all.xml"
    phpunit_junit = (
        '<testsuite name="suite" tests="2">'
        '<testcase classname="Tests.GreetingTest" name="testGreet"/>'
        '<testcase classname="Tests.ExtraTest" name="testShout">'
        '<failure message="boom"/>'
        "</testcase>"
        "</testsuite>"
    )
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=1, evidence_path=test_all_xml, evidence_text=phpunit_junit),
            ScriptedCall(exit_code=0),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "red-baseline" in result.reason
    assert "test_all" in result.reason
    assert "Tests.ExtraTest::testShout" in result.reason
    assert model.invocations == []
    assert runner.commands == [
        "composer install --no-interaction --no-progress",
        f"vendor/bin/phpunit --log-junit {evidence_dir}/test_all.xml",
        "php -l src/Greeting.php",
    ]
    assert all(cwd == workspace / "app" for cwd in runner.cwds)


def test_baseline_reads_the_profile_pinned_at_the_base_revision(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    (workspace / "docs" / "agents" / "project-profile.yml").write_text(
        BASE_PROFILE.replace('bootstrap: "true"', 'bootstrap: "false"'),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence" / "test_all.xml"
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=0, evidence_path=evidence, evidence_text=PASSING_JUNIT),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "delivered"
    assert len(model.invocations) == 1
    assert runner.commands == ["true", "true"]


def test_l2_1_baseline_runs_a_typescript_style_profile(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """L2-1 beyond Python and PHP: a pnpm/vitest profile runs its bootstrap,
    ``test_all`` and checks, and vitest failure identifiers (in a
    ``testsuites`` wrapper, the shape vitest's junit reporter emits) are
    recorded like pytest's and PHPUnit's."""
    commit_profile(workspace, PROFILE_TS_STYLE)
    evidence_dir = tmp_path / "evidence"
    test_all_xml = evidence_dir / "test_all.xml"
    vitest_junit = (
        "<testsuites>"
        '<testsuite name="greet.spec.ts" tests="2">'
        '<testcase classname="greet" name="returns a greeting"/>'
        '<testcase classname="greet" name="shouts">'
        '<failure message="boom"/>'
        "</testcase>"
        "</testsuite>"
        "</testsuites>"
    )
    runner = ScriptedRunner(
        [
            ScriptedCall(exit_code=0),
            ScriptedCall(exit_code=1, evidence_path=test_all_xml, evidence_text=vitest_junit),
            ScriptedCall(exit_code=0),
        ]
    )
    result, _tracker, model = invoke(ledger, workspace, runner, tmp_path)
    assert result.outcome == "failed"
    assert "red-baseline" in result.reason
    assert "test_all" in result.reason
    assert "greet::shouts" in result.reason
    assert model.invocations == []
    assert runner.commands == [
        "pnpm install --frozen-lockfile",
        f"pnpm vitest run --reporter=junit --outputFile={evidence_dir}/test_all.xml",
        "pnpm tsc --noEmit",
    ]
    assert all(cwd == workspace for cwd in runner.cwds)


def test_baseline_without_a_base_revision_refuses_without_running_commands(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """Baseline with no Base Revision in state is an internal invariant
    violation: it refuses as a missing Readiness Fact rather than silently
    baselining whatever HEAD happens to point at, and runs no command."""
    from coding_agent.runtime.graph.attempt import baseline as baseline_node
    from coding_agent.runtime.graph.context import AttemptContext

    runner = ScriptedRunner([ScriptedCall(exit_code=0)])
    model = ScriptedModel([completion_message()])
    context = AttemptContext(
        tracker=FakeTracker(),
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        command_runner=runner,
        evidence_dir=tmp_path / "evidence",
    )
    outcome = baseline_node(
        {"attempt_id": "#7/1", "issue_number": 7, "attempt_number": 1},
        {"configurable": context.to_configurable()},
    )
    assert outcome["outcome"] == "failed"
    assert "missing-readiness-fact" in str(outcome["reason"])
    assert "base-revision" in str(outcome["reason"])
    assert runner.commands == []
    assert model.invocations == []
