"""M4 tail, first half: ``delivery_snapshot`` and ``final_checks``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the Delivery
Snapshot is committed and pushed for every Implementation Outcome whenever
there is a diff; final checks are one authoritative run of ``test_all`` and
every check against the Delivery Snapshot, only for ``verified
completion``, with no retry and no remediation; green means every command
exits zero, ``test_all`` executes at least one test with no failure and
every check passes; zero executed tests is missing evidence and therefore
red; no Baseline Failure exclusion; the Validation Contract is pinned at
the Base Revision even where the candidate edits the profile; validation
covers only the declared working area) and §13 (the write sequence keyed by
``(attempt_id, write_kind)``) and §18 (one branch per Attempt; the id-based
noreply identity; the ``Attempt: #<issue>/<n>`` trailer).

MVP row ids: C-PUB-1, P-DEL-2, P-DEL-8, L2-3, L2-17, L2-18, W-STP-1, C-STP-1,
L3-IMP-29.

Every push here goes to a local bare remote; no test touches the network.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import LedgerRecord, RunLedger
from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.substitution import render_command
from coding_agent.runtime.completion import (
    REPORT_STEP_CANNOT_CONTINUE,
    REPORT_STEP_DONE,
)
from coding_agent.runtime.final_checks import FINAL_CHECKS_KIND
from coding_agent.runtime.git import head_revision
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.graph.step import WorkflowStep
from coding_agent.validate.runner import CommandRunner, ExecutedCommand, SubprocessCommandRunner
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

EXPECTED_BRANCH = "agent/7/1-add-a-greeting"


def _profile(
    test_all: str,
    *,
    working_directory: str = ".",
    checks: str = "checks: none",
) -> str:
    return f"""\
schema: 2
language: python
working_directory: {working_directory}
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "{test_all}"
  test_targeted: "mkdir -p {{evidence_dir}} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {{evidence_dir}}/test_targeted.xml && true {{path}}"
evidence:
  format: junit-xml
  test_all: "{{evidence_dir}}/test_all.xml"
  test_targeted: "{{evidence_dir}}/test_targeted.xml"
{checks}
services: []
"""


def _passing_test_all(marker: str = "test_ok") -> str:
    # The YAML file holds these double quotes escaped (\"), exactly like the
    # existing JUNIT fixtures: the profile is double-quoted YAML holding shell
    # holding XML.
    return (
        "mkdir -p {evidence_dir} && printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"1\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"" + marker + "\\\"/>"
        "</testsuite>' > {evidence_dir}/test_all.xml"
    )


GREEN_PROFILE = _profile(_passing_test_all())


def _init_remote(
    tmp_path: Path,
    profile_text: str,
    *,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """A local bare remote whose base tree holds the profile (and any extra files)."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    for relpath, content in (extra_files or {}).items():
        target = src / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(profile_text, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    return _init_remote(tmp_path, GREEN_PROFILE)


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


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _activate(call_id: str = "call-act-1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[_call("activate_implement", {}, call_id)],
    )


def _write_and_test(
    write_id: str = "call-write-1", test_id: str = "call-test-1"
) -> AIMessage:
    return AIMessage(
        content="writing then testing",
        tool_calls=[
            _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id),
            _call("test_targeted", {"path": "tests/test_greet.py"}, test_id),
        ],
    )


def _completion(
    call_id: str, covering: tuple[str, ...] = ("pkg::test_ok",)
) -> AIMessage:
    return AIMessage(
        content="the change is complete",
        tool_calls=[_call("report_completion", {"AC-1": list(covering)}, call_id)],
    )


def _review(call_id: str = "call-rev-1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[_call("activate_code_review", {}, call_id)],
    )


def _spawn_pair(s1: str = "call_spawn_1", s2: str = "call_spawn_2") -> AIMessage:
    return AIMessage(
        content="reviewing with subagents",
        tool_calls=[
            _call(
                "spawn_subagent",
                {
                    "description": "review standards",
                    "instructions": "review the change against the Target Issue",
                },
                s1,
            ),
            _call(
                "spawn_subagent",
                {
                    "description": "review spec",
                    "instructions": "review the change against the Target Issue",
                },
                s2,
            ),
        ],
    )


def _report(call_id: str, report: str = "no findings") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[_call("report_subagent", {"report": report}, call_id)],
    )


def _silent(text: str = "still thinking") -> AIMessage:
    return AIMessage(content=text)


def _report_step(call_id: str, status: str, justification: str) -> AIMessage:
    return AIMessage(
        content=f"step {status}",
        tool_calls=[
            _call(
                "report_step",
                {"status": status, "justification": justification},
                call_id,
            )
        ],
    )


def _run_tail(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    model: ScriptedModel,
    bare_remote: Path,
    steps: tuple[WorkflowStep, ...] | None = None,
    command_runner: CommandRunner | None = None,
    evidence_dir: Path | None = None,
) -> AttemptResult:
    kwargs: dict[str, object] = {
        "issue_number": 7,
        "attempt_number": 1,
        "tracker": FakeTracker(),
        "ledger": ledger,
        "model": model,
        "workspace": workspace,
        "effective_configuration": EFFECTIVE_CONFIGURATION,
        "skills_dir": skills_dir,
        "remote_url": str(bare_remote),
        "agent_identity": IDENTITY,
    }
    if steps is not None:
        kwargs["steps"] = steps
    if command_runner is not None:
        kwargs["command_runner"] = command_runner
    if evidence_dir is not None:
        kwargs["evidence_dir"] = evidence_dir
    return run_attempt(**kwargs)  # type: ignore[arg-type]


def _remote_branches(bare_remote: Path) -> list[str]:
    output = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], bare_remote
    )
    return sorted(line for line in output.splitlines() if line.strip())


def _final_checks_events(ledger: RunLedger) -> list[LedgerRecord]:
    return [record for record in ledger.records("#7/1") if record.kind == FINAL_CHECKS_KIND]


def _final_payload(ledger: RunLedger) -> dict[str, object]:
    events = _final_checks_events(ledger)
    assert len(events) == 1
    return dict(events[0].payload)


def _final_commands(payload: dict[str, object]) -> list[dict[str, object]]:
    entries = payload.get("commands")
    assert isinstance(entries, list)
    commands: list[dict[str, object]] = []
    for entry in entries:
        assert isinstance(entry, dict)
        commands.append({str(key): value for key, value in entry.items()})
    return commands


class CountingRunner:
    """A ``CommandRunner`` that counts every command run, then really runs it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._real = SubprocessCommandRunner()

    def run(self, command: str, *, cwd: Path, timeout_seconds: float | None = None) -> ExecutedCommand:
        self.calls.append((command, str(cwd)))
        return self._real.run(command, cwd=cwd)


def test_c_pub_1_verified_completion_pushes_and_runs_final_checks_exactly_once(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """C-PUB-1: a diff is committed and pushed; final checks run once, no retry."""
    evidence_dir = workspace.parent / "evidence"
    runner = CountingRunner()
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            # A completed review meets the review gate, so the claim is
            # accepted at once with no P-DEL-3 return.
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        command_runner=runner,
        evidence_dir=evidence_dir,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")

    # The Delivery Snapshot is on the bare remote, under the Attempt branch.
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    remote_sha = run_git(["rev-parse", EXPECTED_BRANCH], bare_remote)
    assert remote_sha == head_revision(workspace)
    assert "Attempt: #7/1" in run_git(["log", "-1", "--format=%B", "HEAD"], workspace)

    # The write went through the effects sequence.
    effect = ledger.get_effect("#7/1", "delivery-snapshot-push")
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.remote_id == remote_sha

    # Final checks ran exactly once: one ledger event, and the final
    # ``test_all`` rendering ran a single time (no retry, no remediation).
    events = _final_checks_events(ledger)
    assert len(events) == 1
    parsed = parse_profile_yaml(GREEN_PROFILE)
    assert isinstance(parsed, ProjectProfile)
    final_test_all = render_command(
        parsed.test_all, evidence_dir=evidence_dir / "final-checks"
    )
    assert sum(1 for command, _ in runner.calls if command == final_test_all) == 1


def test_c_pub_1_implemented_but_unverified_pushes_without_final_checks(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """C-PUB-1: force-accepted claim with a diff pushes; no final checks run."""
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test("call-write-1", "call-test-1"),
            # Unbacked identifiers fail the evidence check twice; the third
            # call is force-accepted with the diff standing.
            _completion("call-rep-1", ("pkg::nope",)),
            _completion("call-rep-2", ("pkg::nope",)),
            _completion("call-rep-3", ("pkg::nope",)),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "unverified")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    effect = ledger.get_effect("#7/1", "delivery-snapshot-push")
    assert effect is not None
    assert effect.status == "receipt"
    assert _final_checks_events(ledger) == []


def test_already_satisfied_pushes_nothing_and_creates_no_branch(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """No diff means no commit, no push and no branch — and no final checks."""
    model = ScriptedModel(
        [
            _activate(),
            AIMessage(
                content="testing only",
                tool_calls=[_call("test_targeted", {"path": "tests/test_greet.py"}, "call-test-1")],
            ),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "already-satisfied")
    assert _remote_branches(bare_remote) == ["main"]
    assert ledger.get_effect("#7/1", "delivery-snapshot-push") is None
    assert _final_checks_events(ledger) == []


def test_w_stp_1_cannot_continue_pushes_the_diff(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """W-STP-1, pushed half: the diff is committed and pushed, no final checks."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            AIMessage(
                content="writing only",
                tool_calls=[
                    _call(
                        "write_file",
                        {"path": "greeting.txt", "content": "hello\n"},
                        "call-write-1",
                    )
                ],
            ),
            _report_step("call-step-1", REPORT_STEP_CANNOT_CONTINUE, "blocked: needs a human"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        steps=steps,
    )
    assert (result.outcome, result.reason) == ("failed", "step-cannot-continue")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert _final_checks_events(ledger) == []


def test_c_stp_1_silence_pushes_the_diff(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """C-STP-1, pushed half: silence stops the list; the diff is still pushed."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            AIMessage(
                content="writing only",
                tool_calls=[
                    _call(
                        "write_file",
                        {"path": "greeting.txt", "content": "hello\n"},
                        "call-write-1",
                    )
                ],
            ),
            _silent("quiet once"),
            _silent("quiet twice"),
            _silent("quiet thrice"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        steps=steps,
    )
    assert (result.outcome, result.reason) == ("failed", "undeclared")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert _final_checks_events(ledger) == []


def test_l3_imp_29_uncovered_claim_pushes_the_diff(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """L3-IMP-29, pushed half: an explicit ``uncovered`` claim still pushes."""
    model = ScriptedModel(
        [
            _activate(),
            AIMessage(
                content="writing only",
                tool_calls=[
                    _call(
                        "write_file",
                        {"path": "greeting.txt", "content": "hello\n"},
                        "call-write-1",
                    )
                ],
            ),
            # An explicit uncovered claim is accepted, not refused; with a
            # diff and no review it is first returned for the missing review.
            _completion("call-rep-1", ("uncovered",)),
            _completion("call-rep-2", ("uncovered",)),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "criteria-uncovered")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert _final_checks_events(ledger) == []


def test_final_checks_evidence_records_commands_exit_codes_counts_and_failures(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """Validation Evidence names every command, exit, executed count and failure."""
    # Only the authoritative final run (evidence dir ``.../final-checks``)
    # fails: the baseline runs the same pinned contract at the Base Revision
    # and must stay green, so the Attempt reaches ``verified completion``.
    failing_test_all = (
        "mkdir -p {evidence_dir} && case \\\"{evidence_dir}\\\" in "
        "*final-checks*) printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"2\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_ok\\\"/>"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_bad\\\">"
        "<failure message=\\\"boom\\\"/></testcase></testsuite>' "
        "> {evidence_dir}/test_all.xml && false;; "
        "*) printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"1\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_ok\\\"/></testsuite>' "
        "> {evidence_dir}/test_all.xml;; esac"
    )
    remote = _init_remote(tmp_path, _profile(failing_test_all))
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("failed", "failing-validation")

    payload = _final_payload(ledger)
    assert payload["green"] is False
    assert payload["working_area"] == "."
    (command,) = [
        entry for entry in _final_commands(payload) if entry["name"] == "test_all"
    ]
    assert command["exit_code"] == 1
    assert command["executed"] == 2
    assert command["failure_ids"] == ["pkg::test_bad"]


def test_l2_3_zero_tests_is_failed_validation_despite_exit_zero(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """L2-3: exit zero with zero executed tests is missing evidence, hence red."""
    empty_test_all = (
        "mkdir -p {evidence_dir} && printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"0\\\"></testsuite>' "
        "> {evidence_dir}/test_all.xml"
    )
    remote = _init_remote(tmp_path, _profile(empty_test_all))
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("failed", "failing-validation")
    payload = _final_payload(ledger)
    # Exit zero is not enough: no executed test means no evidence.
    assert payload["green"] is False
    (command,) = [
        entry for entry in _final_commands(payload) if entry["name"] == "test_all"
    ]
    assert command["exit_code"] == 0
    assert command["executed"] == 0


def test_l2_17_candidate_profile_edit_is_validated_with_the_base_contract(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """L2-17: the candidate edits the profile; validation still runs the base one."""
    candidate_test_all = _passing_test_all("candidate_test").replace(
        "test_all.xml", "candidate.xml"
    )
    candidate_profile = _profile(candidate_test_all).replace(
        "test_all.xml", "candidate.xml"
    )
    remote = _init_remote(tmp_path, GREEN_PROFILE)
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    base_revision = run_git(["rev-parse", "HEAD"], workdir)
    model = ScriptedModel(
        [
            _activate(),
            AIMessage(
                content="editing the profile and the code",
                tool_calls=[
                    _call(
                        "write_file",
                        {
                            "path": "docs/agents/project-profile.yml",
                            "content": candidate_profile,
                        },
                        "call-write-1",
                    ),
                    _call(
                        "write_file",
                        {"path": "greeting.txt", "content": "hello\n"},
                        "call-write-2",
                    ),
                    _call("test_targeted", {"path": "tests/test_greet.py"}, "call-test-1"),
                ],
            ),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    payload = _final_payload(ledger)
    assert payload["green"] is True
    # The base contract ran, not the candidate's edit ...
    (command,) = [
        entry for entry in _final_commands(payload) if entry["name"] == "test_all"
    ]
    recorded = command["command"]
    assert isinstance(recorded, str)
    assert "candidate_test" not in recorded
    assert "test_ok" in recorded
    # ... and the edit is recorded for disclosure in the pull-request body.
    assert payload["profile_changed_in_candidate"] is True
    assert payload["contract_pinned_at_base_revision"] == base_revision


def test_l2_18_two_apps_validates_only_the_declared_working_area(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """L2-18: with two applications, validation covers only the declared area."""
    scoped_test_all = (
        "test -f marker.txt && mkdir -p {evidence_dir} && printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"1\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_ok\\\"/></testsuite>' "
        "> {evidence_dir}/test_all.xml"
    )
    profile = _profile(scoped_test_all, working_directory="apps/one")
    remote = _init_remote(
        tmp_path,
        profile,
        extra_files={"apps/one/marker.txt": "one\n", "apps/two/marker.txt": "two\n"},
    )
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    events = _final_checks_events(ledger)
    assert len(events) == 1
    payload = events[0].payload
    # ``marker.txt`` exists only under ``apps/one``: green proves the run
    # covered the declared area, and the area is recorded for disclosure.
    assert payload["green"] is True
    assert payload["working_area"] == "apps/one"


def test_green_run_leaving_a_file_records_a_dirty_tree(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """A green run that leaves a modified or untracked file records a dirty tree."""
    # The leftover file appears only in the authoritative final run: the
    # baseline runs the same command at the Base Revision, and a file left
    # there would be committed as part of the Delivery Snapshot instead of
    # reported as dirty.
    dirtying_test_all = (
        _passing_test_all() + " && case \\\"{evidence_dir}\\\" in "
        "*final-checks*) touch leftover.txt;; *) true;; esac"
    )
    remote = _init_remote(tmp_path, _profile(dirtying_test_all))
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("failed", "dirty-tree")
    payload = _final_payload(ledger)
    assert payload["green"] is True
    dirty = payload["dirty_files"]
    assert isinstance(dirty, list)
    assert "leftover.txt" in dirty


def test_red_check_is_failed_validation(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """A failing check (no evidence, non-zero exit) fails validation."""
    # The check passes at the Base Revision (no candidate diff yet, so
    # ``greeting.txt`` is absent) and fails only against the Delivery
    # Snapshot: a check that fails the baseline would end the Attempt at
    # ``red-baseline`` before any model call.
    profile = _profile(
        _passing_test_all(),
        checks="checks:\n  - name: types\n    command: \"test ! -f greeting.txt\"",
    )
    remote = _init_remote(tmp_path, profile)
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )
    result = _run_tail(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("failed", "failing-validation")
    payload = _final_payload(ledger)
    assert payload["green"] is False
    names = sorted(str(entry["name"]) for entry in _final_commands(payload))
    assert names == ["test_all", "types"]
    (check,) = [
        entry for entry in _final_commands(payload) if entry["name"] == "types"
    ]
    assert check["exit_code"] == 1
    assert check["executed"] is None
