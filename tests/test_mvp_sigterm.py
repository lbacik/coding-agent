"""SIGTERM while the Worker is live: intake stops, the Attempt finalizes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (SIGTERM stops
intake and the process then exits), §11 (SIGTERM: intake stops at once; the
live Attempt observes the signal after every model response and every tool
result; the in-flight model request is cancelled and the running command's
whole process group is killed; before the Delivery Intent the Attempt takes
the claim-confirmed path of the recovery table within
``shutdown_grace_seconds`` — local commits pushed fast-forward, the dirty
tree never committed, ``failed`` / ``interrupted``, or ``abandoned`` where
the issue was cancelled meanwhile; after the Delivery Intent publication
completes as ``delivered``; a second signal is an ordinary process loss) and
§17 (the claim-confirmed recovery row).

MVP row ids: P-CAN-3, P-CAN-2, REC-SIG-1, REC-SIG-2, G-REV-3, W-STP-3.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage

from conftest import init_origin_repo, run_git
from coding_agent.identity.commit_identity import attempt_branch_name
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.gates import GateStop
from coding_agent.runtime.review_snapshot import check_in_tool_gate
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.shutdown import (
    SHUTDOWN_EXIT_CODE,
    is_shutdown_requested,
    request_shutdown,
    reset_shutdown,
)
from coding_agent.tracker.port import CandidateIssue, PullRequest
from coding_agent.validate.runner import (
    ExecutedCommand,
    SubprocessCommandRunner,
    run_command_with_timeout,
)
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import REVIEW_SKILL_TEXT

EFFECTIVE_CONFIGURATION: dict[str, object] = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

IDENTITY = Identity(account_id=999, login="coding-agent")

SELECTION_LABEL = "ready-for-agent"
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"

HAPPY_PATH_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

LONG_COMMAND_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "sleep 30"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


@pytest.fixture(autouse=True)
def _clean_shutdown_flag() -> Iterator[None]:
    reset_shutdown()
    try:
        yield
    finally:
        reset_shutdown()


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _make_remote(src: Path, profile: str) -> Path:
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(profile, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = src.parent / (src.name + ".git")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    return bare


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    return _make_remote(tmp_path / "origin-src", HAPPY_PATH_PROFILE)


@pytest.fixture
def long_remote(tmp_path: Path) -> Path:
    return _make_remote(tmp_path / "long-src", LONG_COMMAND_PROFILE)


def _clone_into(tmp_path: Path, remote: Path, name: str) -> Path:
    workdir = tmp_path / name
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    return _clone_into(tmp_path, bare_remote, "workspace")


@pytest.fixture
def long_workspace(tmp_path: Path, long_remote: Path) -> Path:
    return _clone_into(tmp_path, long_remote, "long-workspace")


def _tool_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[_tool_call("activate_implement", {}, call_id)],
    )


def _write_message(
    path: str, content: str, call_id: str, extra: list[dict[str, object]] | None = None
) -> AIMessage:
    calls = [_tool_call("write_file", {"path": path, "content": content}, call_id)]
    if extra:
        calls.extend(extra)
    return AIMessage(content="working", tool_calls=calls)


def _test_targeted_call(call_id: str) -> dict[str, object]:
    return _tool_call("test_targeted", {"path": "tests/test_greet.py"}, call_id)


def _review_message(call_id: str = "call_review_1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[_tool_call("activate_code_review", {}, call_id)],
    )


def _seat_message(call_id: str, findings: str = "no findings") -> AIMessage:
    return AIMessage(
        content="review seat",
        tool_calls=[_tool_call("report_review", {"findings": findings}, call_id)],
    )


def _completion_message(call_id: str = "call_completion_1") -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[_tool_call("report_completion", {"AC-1": ["pkg::test_ok"]}, call_id)],
    )


class ShutdownArmingModel(ScriptedModel):
    """A scripted model that records SIGTERM when it produces ``arm_on`` (§11).

    The signal arrives mid-turn — while the response is produced — so the
    next stop point (the in-tool Gate, the between-calls break, the routing
    exit, the step boundary) diverts deterministically, with no threads and
    no timing race. ``on_arm`` hooks run after the flag is set, so a test
    can also close the issue to prove the cancelled-meantime path.
    """

    def __init__(
        self,
        outcomes: list[AIMessage],
        arm_on: str,
        on_arm: list[Callable[[], None]] | None = None,
    ) -> None:
        super().__init__(outcomes)
        self._arm_on = arm_on
        self._on_arm: list[Callable[[], None]] = list(on_arm or [])

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        message = super().invoke(input)
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call.get("name") == self._arm_on:
                    request_shutdown()
                    for hook in self._on_arm:
                        hook()
        return message


def _terminal_comments(tracker: FakeTracker) -> list[str]:
    """Every terminal comment body the Attempt performed (§14, §15)."""
    bodies: list[str] = []
    for name, args in tracker.calls:
        if name == "post_terminal_comment":
            bodies.append(str(args[1]))
    return bodies


def _remote_branches(remote: Path) -> list[str]:
    return run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], remote
    ).splitlines()


def _invoke_attempt(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    tracker: FakeTracker,
    model: ScriptedModel,
    remote: Path,
    command_runner: Any | None = None,
) -> AttemptResult:
    (skills_dir / "code-review" / "SKILL.md").write_text(
        REVIEW_SKILL_TEXT, encoding="utf-8"
    )
    kwargs: dict[str, Any] = {
        "issue_number": 7,
        "attempt_number": 1,
        "tracker": tracker,
        "ledger": ledger,
        "model": model,
        "workspace": workspace,
        "effective_configuration": EFFECTIVE_CONFIGURATION,
        "skills_dir": skills_dir,
        "remote_url": str(remote),
        "agent_identity": IDENTITY,
    }
    if command_runner is not None:
        kwargs["command_runner"] = command_runner
    return run_attempt(**kwargs)  # type: ignore[arg-type]


def test_p_can_3_sigterm_mid_implementation_ends_failed_interrupted(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-CAN-3: SIGTERM mid-implementation ends failed/interrupted, pushed.

    The review snapshot already pushed ``greeting.txt``; the signal armed
    while the completion response was produced diverts at the next stop
    point, before the Delivery Intent. The Delivery Snapshot never runs, so
    ``notes.txt`` stays uncommitted and the comment states its count.
    """
    tracker = FakeTracker()
    model = ShutdownArmingModel(
        [
            _activation_message(),
            _write_message(
                "greeting.txt",
                "hello\n",
                "call_write_1",
                extra=[_test_targeted_call("call_test_1")],
            ),
            _review_message(),
            _seat_message("seat-std-1"),
            _seat_message("seat-spec-1"),
            _write_message("notes.txt", "draft\n", "call_write_2"),
            _completion_message(),
        ],
        arm_on="report_completion",
    )
    result = _invoke_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model,
        remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "interrupted")
    branch = attempt_branch_name(7, 1, "Add a greeting")
    assert branch in _remote_branches(bare_remote)
    listed = run_git(["ls-tree", "-r", "--name-only", branch], bare_remote).splitlines()
    assert "greeting.txt" in listed
    assert "notes.txt" not in listed
    porcelain = run_git(["status", "--porcelain"], workspace)
    assert "notes.txt" in porcelain
    comments = _terminal_comments(tracker)
    assert len(comments) == 1
    assert "1 uncommitted file(s)" in comments[0]
    assert "open_pull_request" not in tracker.call_names()
    stored = ledger.get_outcome("#7/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")


def test_rec_sig_1_graph_killed_command_ends_interrupted_pushed_with_one_comment(
    ledger: RunLedger,
    long_workspace: Path,
    skills_dir: Path,
    long_remote: Path,
) -> None:
    """REC-SIG-1 (graph half): the killed command ends the Attempt here.

    The arming runner records SIGTERM as the long ``test_targeted`` starts,
    so the runtime kills its process group at the first slice; the Attempt
    ends ``interrupted`` with the review snapshot's push kept, one comment,
    and no pull request.
    """
    tracker = FakeTracker()
    inner = SubprocessCommandRunner(timeout_seconds=60.0)
    seen: dict[str, ExecutedCommand] = {}

    class ArmingRunner:
        def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
            if "sleep 30" in command:
                request_shutdown()
            result = inner.run(command, cwd=cwd)
            if "sleep 30" in command:
                seen["long"] = result
            return result

    model = ShutdownArmingModel(
        [
            _activation_message(),
            _write_message("greeting.txt", "hello\n", "call_write_1"),
            _review_message(),
            _seat_message("seat-std-1"),
            _seat_message("seat-spec-1"),
            AIMessage(
                content="validating",
                tool_calls=[_test_targeted_call("call_test_long")],
            ),
        ],
        # The review itself must not divert: the tool never fires, so arm
        # only on the long command — the runner arms instead.
        arm_on="never-a-tool",
    )
    result = _invoke_attempt(
        ledger=ledger,
        workspace=long_workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model,
        remote=long_remote,
        command_runner=ArmingRunner(),
    )
    assert (result.outcome, result.reason) == ("failed", "interrupted")
    assert "long" in seen
    assert seen["long"].exit_code == SHUTDOWN_EXIT_CODE
    assert seen["long"].timed_out is False
    assert "whole process group" in seen["long"].stdout
    branch = attempt_branch_name(7, 1, "Add a greeting")
    assert branch in _remote_branches(long_remote)
    assert len(_terminal_comments(tracker)) == 1
    assert "open_pull_request" not in tracker.call_names()


def test_rec_sig_1_shutdown_kills_running_command_group(tmp_path: Path) -> None:
    """REC-SIG-1 (runner half): SIGTERM kills the command's process group."""
    arm = threading.Timer(0.3, request_shutdown)
    arm.start()
    try:
        start = time.monotonic()
        result = run_command_with_timeout(
            "sleep 30", cwd=tmp_path, env={}, timeout_seconds=60.0
        )
        elapsed = time.monotonic() - start
    finally:
        arm.cancel()
    assert result.exit_code == SHUTDOWN_EXIT_CODE
    assert result.timed_out is False
    assert elapsed < 10
    assert "whole process group" in result.stdout


def test_rec_sig_1_shutdown_kills_grandchildren(tmp_path: Path) -> None:
    """REC-SIG-1: a grandchild holding the stdout pipe dies with the group.

    The inner shell backgrounds a ``sleep`` and waits: killing only the
    group leader would leave the grandchild holding the pipe and
    ``communicate`` blocked. The group kill returns promptly instead.
    """
    arm = threading.Timer(0.3, request_shutdown)
    arm.start()
    try:
        start = time.monotonic()
        result = run_command_with_timeout(
            "sh -c 'sleep 30 & wait'", cwd=tmp_path, env={}, timeout_seconds=60.0
        )
        elapsed = time.monotonic() - start
    finally:
        arm.cancel()
    assert result.exit_code == SHUTDOWN_EXIT_CODE
    assert result.timed_out is False
    assert elapsed < 10


def test_rec_sig_2_shutdown_after_delivery_intent_delivers(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """REC-SIG-2: SIGTERM after the Delivery Intent still delivers.

    The signal is recorded inside ``open_pull_request`` — past the point of
    no return — and publication completes as ``delivered``.
    """
    tracker = FakeTracker()
    real_open = tracker.open_pull_request

    def arming_open(
        *,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
    ) -> PullRequest:
        request_shutdown()
        return real_open(
            title=title, head_branch=head_branch, base_branch=base_branch, body=body
        )

    tracker.open_pull_request = arming_open  # type: ignore[method-assign]
    model = ShutdownArmingModel(
        [
            _activation_message(),
            _write_message(
                "greeting.txt",
                "hello\n",
                "call_write_1",
                extra=[_test_targeted_call("call_test_1")],
            ),
            _review_message(),
            _seat_message("seat-std-1"),
            _seat_message("seat-spec-1"),
            _completion_message(),
        ],
        arm_on="never-a-tool",
    )
    result = _invoke_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model,
        remote=bare_remote,
    )
    assert is_shutdown_requested()
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert "open_pull_request" in tracker.call_names()


def test_g_rev_3_sigterm_half_in_tool_gate_raises_interrupted() -> None:
    """G-REV-3 (SIGTERM half): the in-tool Gate ends failed/interrupted."""
    tracker = FakeTracker()
    request_shutdown()
    with pytest.raises(GateStop) as excinfo:
        check_in_tool_gate(
            tracker=tracker,
            issue_number=7,
            selection_label=tracker.selection_label,
        )
    assert (excinfo.value.outcome, excinfo.value.reason) == ("failed", "interrupted")


def test_g_rev_3_cancellation_keeps_precedence_over_sigterm() -> None:
    """G-REV-3: a concurrently cancelled issue still ends abandoned."""
    tracker = FakeTracker()
    tracker.candidates[7] = CandidateIssue(
        number=7,
        title="Add a greeting",
        body=BODY,
        is_open=False,
        assignees=(),
        labels=(),
        created_at="2026-10-01T00:00:00Z",
    )
    request_shutdown()
    with pytest.raises(GateStop) as excinfo:
        check_in_tool_gate(
            tracker=tracker,
            issue_number=7,
            selection_label=tracker.selection_label,
        )
    assert (excinfo.value.outcome, excinfo.value.reason) == ("abandoned", "cancelled")


def test_g_rev_3_graph_sigterm_at_gate_keeps_pushed_work(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """G-REV-3 (graph half): SIGTERM at the review Gate ends it there.

    The signal is armed while the ``activate_code_review`` response is
    produced, so the Gate fires before the review-snapshot push: no Attempt
    branch exists remotely, and the Attempt ends failed/interrupted.
    """
    tracker = FakeTracker()
    model = ShutdownArmingModel(
        [
            _activation_message(),
            _write_message("greeting.txt", "hello\n", "call_write_1"),
            _review_message(),
        ],
        arm_on="activate_code_review",
    )
    result = _invoke_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model,
        remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "interrupted")
    branch = attempt_branch_name(7, 1, "Add a greeting")
    assert branch not in _remote_branches(bare_remote)
    assert len(_terminal_comments(tracker)) == 1
    assert "open_pull_request" not in tracker.call_names()


def test_cancelled_meantime_ends_abandoned(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """SIGTERM with the issue cancelled meanwhile ends abandoned (§11, §17)."""
    tracker = FakeTracker()
    tracker.candidates[7] = CandidateIssue(
        number=7,
        title="Add a greeting",
        body=BODY,
        is_open=True,
        assignees=(),
        labels=(SELECTION_LABEL,),
        created_at="2026-10-01T00:00:00Z",
    )

    def close_issue() -> None:
        candidate = tracker.candidates[7]
        tracker.candidates[7] = replace(candidate, is_open=False)

    model = ShutdownArmingModel(
        [
            _activation_message(),
            _write_message(
                "greeting.txt",
                "hello\n",
                "call_write_1",
                extra=[_test_targeted_call("call_test_1")],
            ),
            _completion_message(),
        ],
        arm_on="report_completion",
        on_arm=[close_issue],
    )
    result = _invoke_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model,
        remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("abandoned", "cancelled")


def _candidate(number: int, created_at: str) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title=f"Add a greeting {number}",
        body=BODY,
        is_open=True,
        assignees=(),
        labels=(SELECTION_LABEL,),
        created_at=created_at,
    )


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        selection_label=SELECTION_LABEL,
        agent_login=IDENTITY.login,
        agent_identity_id=IDENTITY.account_id,
        effective_configuration={"repo": "example-owner/example-repo"},
    )


def test_p_can_3_sigterm_stops_intake_with_no_new_claim(ledger: RunLedger) -> None:
    """P-CAN-3: SIGTERM stops intake — the second issue is never claimed."""
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[31] = _candidate(31, "2026-10-02T00:00:00Z")
    tracker.candidates[32] = _candidate(32, "2026-10-01T00:00:00Z")
    ran: list[int] = []

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ran.append(claimed.issue_number)
        request_shutdown()
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="failed", reason="interrupted"
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id, outcome="failed", reason="interrupted"
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=WorkerLoopConfig(
            claim_config=_claim_config(), poll_interval_seconds=60.0
        ),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=2,
    )
    assert exit_code == 0
    assert ran == [32]
    assert tracker.call_names().count("assign_issue") == 1


def test_shutdown_sleep_returns_at_once_when_signalled() -> None:
    """The interruptible poll sleep yields instead of sleeping through grace."""
    from coding_agent.worker.run import _sleep_or_shutdown

    request_shutdown()
    assert _sleep_or_shutdown(lambda seconds: None, 42.0) is True
