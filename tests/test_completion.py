"""M1 completion: declared and substantiated, never inferred.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (the runtime-owned
targeted-test tool runs the Validation Contract's ``test_targeted`` command
with ``{path}`` substituted, is the sole writer of the per-Attempt
``{evidence_dir}``, parses the JUnit result and records Criterion Evidence;
the ``report_completion`` schema is generated per Attempt from that Attempt's
Acceptance Criteria; exactly one Implementation Outcome is decided from the
claim and Criterion Evidence alone; only ``verified completion`` may use PASS
wording).

MVP row ids: L2-2, L3-IMP-10, L3-IMP-20.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.runtime.completion import (
    CriterionEvidence,
    decide_implementation_outcome,
    normalize_claim,
    report_completion_schema,
)
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.graph.tools import build_implement_step_tools
from coding_agent.runtime.toolset import build_test_targeted_tool
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.junit import JUnitResult, parse_junit_xml
from coding_agent.validate.runner import ExecutedCommand
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

HAPPY_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_targeted.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

GOOD_BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


def _parse_happy_profile() -> object:
    parsed = parse_profile_yaml(HAPPY_PROFILE)
    assert not isinstance(parsed, str)
    return parsed


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
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


def _completion_message(covering: object = ("pkg::test_ok",)) -> AIMessage:
    args: dict[str, object] = {"AC-1": covering}
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {"name": "report_completion", "args": args, "id": "call_completion_1", "type": "tool_call"}
        ],
    )


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {"name": "activate_implement", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


class _RecordingRunner:
    """A CommandRunner fake that records commands and writes scripted JUnit."""

    def __init__(self, junit_text: str) -> None:
        self._junit_text = junit_text
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        self.commands.append(command)
        target = _evidence_target(command)
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self._junit_text, encoding="utf-8")
        return ExecutedCommand(command=command, exit_code=0, stdout="", stderr="")


def _evidence_target(command: str) -> Path | None:
    """Extract the `> <path>` redirect target naming the JUnit report."""
    if ">" not in command:
        return None
    after = command.rsplit(">", 1)[1].strip()
    token = after.split()[0].strip("'\"")
    try:
        return Path(token)
    except Exception:
        return None


def test_l2_2_test_targeted_runs_the_named_file_and_only_that_file(tmp_path: Path) -> None:
    """L2-2: `test_targeted` with `{path}` runs the named test file only."""
    profile = _parse_happy_profile()
    assert hasattr(profile, "test_targeted")
    evidence_dir = tmp_path / "evidence"
    working = tmp_path / "work"
    working.mkdir()
    junit = (
        '<testsuite name="suite" tests="1">'
        '<testcase classname="pkg" name="test_ok"/>'
        "</testsuite>"
    )
    runner = _RecordingRunner(junit)
    from coding_agent.profile.schema import ProjectProfile

    assert isinstance(profile, ProjectProfile)
    context = CommandContext(runner, working, evidence_dir, workspace_root=working)
    evidence = CriterionEvidence()
    tool = build_test_targeted_tool(profile, context, on_junit=evidence.record_junit)
    result = str(tool.invoke({"path": "tests/test_greet.py"}))
    assert result != ""
    assert len(runner.commands) == 1
    assert "tests/test_greet.py" in runner.commands[0]
    assert "tests/test_other.py" not in runner.commands[0]
    assert "{path}" not in runner.commands[0]
    assert evidence.latest_result("pkg::test_ok") is True


def test_criterion_evidence_records_history_and_shell_adds_nothing(
    tmp_path: Path, skills_dir: Path
) -> None:
    """§9: history records every targeted-test execution; file tools add nothing."""
    first = parse_junit_xml(
        '<testsuite name="suite" tests="1">'
        '<testcase classname="pkg" name="test_ok"/>'
        "</testsuite>"
    )
    second = parse_junit_xml(
        '<testsuite name="suite" tests="2">'
        '<testcase classname="pkg" name="test_ok"/>'
        '<testcase classname="pkg" name="test_bad"><failure message="boom"/></testcase>'
        "</testsuite>"
    )
    evidence = CriterionEvidence()
    evidence.record_junit(first)
    evidence.record_junit(second)
    history = evidence.history()
    # `record_junit` records passes (sorted) then failures (sorted).
    assert [(record.test_id, record.passed) for record in history] == [
        ("pkg::test_ok", True),
        ("pkg::test_ok", True),
        ("pkg::test_bad", False),
    ]
    assert evidence.latest_result("pkg::test_bad") is False
    assert evidence.latest_result("pkg::test_ok") is True

    # A JUnit-shaped write through any other channel is not Criterion
    # Evidence: only the targeted-test tool's `record_junit` writes here.
    assert isinstance(first, JUnitResult)
    fresh = CriterionEvidence()
    assert fresh.history() == ()
    assert fresh.executed_ids() == frozenset()

    # File tools are workspace-scoped and never touch the evidence store.
    profile = _parse_happy_profile()
    from coding_agent.profile.schema import ProjectProfile

    assert isinstance(profile, ProjectProfile)
    store_evidence = CriterionEvidence()
    tools = build_implement_step_tools(
        tmp_path / "ws",
        skills_dir=skills_dir,
        profile=profile,
        evidence_dir=tmp_path / "evidence",
        criterion_evidence=store_evidence,
        criteria_ids=("AC-1",),
    )
    names = {tool.name for tool in tools}
    assert "test_targeted" in names
    assert store_evidence.history() == ()


def test_report_completion_schema_lists_exactly_the_attempt_criteria() -> None:
    """§9: the per-Attempt schema lists exactly the Attempt's `AC-n` ids."""
    schema = report_completion_schema(("AC-1", "AC-2"))
    assert set(schema["properties"]) == {"AC-1", "AC-2"}
    assert set(schema["required"]) == {"AC-1", "AC-2"}
    assert schema["additionalProperties"] is False
    single = report_completion_schema(("AC-1",))
    assert set(single["properties"]) == {"AC-1"}
    assert list(single["required"]) == ["AC-1"]


def test_verified_run_with_diff_and_passing_evidence_is_verified_completion(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """§9: a diff plus passing evidence for every criterion is verified."""
    tracker = FakeTracker()
    model = ScriptedModel(
        [
            _activation_message(),
            AIMessage(
                content="working",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "greeting.txt", "content": "hello\n"},
                        "id": "call_write_1",
                        "type": "tool_call",
                    },
                    {
                        "name": "test_targeted",
                        "args": {"path": "tests/test_greet.py"},
                        "id": "call_test_1",
                        "type": "tool_call",
                    },
                ],
            ),
            _completion_message(["pkg::test_ok"]),
        ]
    )
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
    )
    assert result.outcome == "delivered"
    assert result.reason == "verified-completion"
    bodies = [args[1] for name, args in tracker.calls if name == "post_terminal_comment"]
    assert len(bodies) == 1
    assert "PASS" in str(bodies[0])


def test_l3_imp_10_identical_tree_with_passing_evidence_is_already_satisfied(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """L3-IMP-10: no diff plus complete passing evidence is already satisfied."""
    tracker = FakeTracker()
    model = ScriptedModel(
        [
            _activation_message(),
            AIMessage(
                content="checking",
                tool_calls=[
                    {
                        "name": "test_targeted",
                        "args": {"path": "tests/test_greet.py"},
                        "id": "call_test_1",
                        "type": "tool_call",
                    }
                ],
            ),
            _completion_message(["pkg::test_ok"]),
        ]
    )
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
    )
    assert result.outcome == "failed"
    assert result.reason == "already-satisfied"
    # No candidate diff: the tree is identical to the Base Revision.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status == ""
    # No branch and no pull request are recorded: M1 publishes nothing.
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "agent/" not in branches
    # The audit evidence is retained: the baseline Validation Evidence row exists.
    records = ledger.records("#7/1")
    assert any(record.kind == "baseline" for record in records)
    assert "add_ready_for_human" in tracker.call_names()
    bodies = [args[1] for name, args in tracker.calls if name == "post_terminal_comment"]
    assert len(bodies) == 1
    assert "PASS" not in str(bodies[0])


def test_l3_imp_20_incomplete_evidence_never_already_satisfied_and_pass_only_verified(
    ledger: RunLedger, workspace: Path, skills_dir: Path, tmp_path: Path
) -> None:
    """L3-IMP-20: incomplete evidence yields one outcome, never already satisfied."""
    # Case 1: claimed test never executed (no passing Criterion Evidence).
    tracker = FakeTracker()
    model = ScriptedModel([_activation_message(), _completion_message(["pkg::never_ran"])])
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
    )
    assert result.outcome == "failed"
    assert result.reason == "criteria-uncovered"
    bodies = [args[1] for name, args in tracker.calls if name == "post_terminal_comment"]
    assert len(bodies) == 1
    assert "PASS" not in str(bodies[0])

    # Case 2: no claim at all (the model never calls report_completion).
    ledger2 = RunLedger(tmp_path / "second.sqlite3")
    try:
        tracker2 = FakeTracker()
        model2 = ScriptedModel([_activation_message(), AIMessage(content="done")])
        src = tmp_path / "origin2"
        init_origin_repo(src)
        (src / "docs" / "agents").mkdir(parents=True, exist_ok=True)
        (src / "docs" / "agents" / "project-profile.yml").write_text(
            HAPPY_PROFILE, encoding="utf-8"
        )
        run_git(["add", "."], src)
        run_git(["commit", "-q", "-m", "add project profile"], src)
        bare2 = tmp_path / "remote2.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare2)], check=True)
        work2 = tmp_path / "workspace2"
        run_git(["clone", "-q", str(bare2), str(work2)], cwd=tmp_path)
        result2 = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker2,
            ledger=ledger2,
            model=model2,
            workspace=work2,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert result2.outcome == "failed"
        assert result2.reason == "no-completion-evidence"
        assert result2.reason != "already-satisfied"
    finally:
        ledger2.close()


def test_explicitly_uncovered_criterion_is_accepted_as_criteria_uncovered() -> None:
    """§9: an explicit `uncovered` marker is accepted, not refused."""
    evidence = CriterionEvidence()
    evidence.record_junit(
        parse_junit_xml(
            '<testsuite name="suite" tests="1">'
            '<testcase classname="pkg" name="test_ok"/>'
            "</testsuite>"
        )
    )
    claim = normalize_claim({"AC-1": "uncovered"}, ("AC-1",))
    assert claim == {"AC-1": None}
    outcome, reason = decide_implementation_outcome(claim, evidence, has_diff=True)
    assert outcome == "completion not established"
    assert reason == "criteria-uncovered"
