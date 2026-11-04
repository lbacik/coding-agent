"""Unit tests for the shutdown flag and the shared interrupted finalization.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (SIGTERM stops
intake; the process then exits) and §11 (SIGTERM: intake stops at once; the
live Attempt observes the signal after every model response and every tool
result; before the Delivery Intent the Attempt takes the claim-confirmed
path of the recovery table within ``shutdown_grace_seconds``; a second
signal is an ordinary process loss) and §17 (the claim-confirmed recovery
row) and §13 (the remote ref is ours when it equals the last receipt's sha
or an open push intent's sha).

MVP row ids: P-CAN-3, REC-SIG-1, REC-INT-1, REC-INT-2, REC-INT-3.
"""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import attempt_branch_sha
from coding_agent.runtime.interrupted import (
    INTERRUPTED_EVENT_KIND,
    INTERRUPTED_PUSH_KIND,
    decide_interrupted_outcome,
    finalize_interrupted_attempt,
    perform_recorded_terminal_writes,
    push_interrupted_commits,
)
from coding_agent.runtime.shutdown import (
    INTERRUPTED_REASON,
    install_shutdown_handlers,
    is_shutdown_requested,
    request_shutdown,
    reset_shutdown,
    shutdown_grace_deadline,
)
from support.fake_tracker import FakeTracker

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}


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


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


def test_shutdown_flag_defaults_to_unset() -> None:
    """No stop is observed until SIGTERM (or the test seam) records one."""
    assert is_shutdown_requested() is False


def test_request_shutdown_sets_the_flag() -> None:
    request_shutdown()
    assert is_shutdown_requested() is True


def test_shutdown_grace_deadline_is_in_the_future() -> None:
    deadline = shutdown_grace_deadline(30.0)
    assert deadline > 0
    with pytest.raises(ValueError, match="must be positive"):
        shutdown_grace_deadline(0)


def test_install_shutdown_handlers_installs_an_explicit_sigterm_handler() -> None:
    """The handler is installed explicitly, so it fires even as PID 1 (§11).

    In the image ``agent run`` is PID 1, where the kernel ignores a signal
    with no installed handler. An explicit handler is what makes SIGTERM
    observable in the container.
    """
    previous = signal.getsignal(signal.SIGTERM)
    try:
        install_shutdown_handlers()
        installed = signal.getsignal(signal.SIGTERM)
        assert installed not in (signal.SIG_DFL, signal.SIG_IGN)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_push_with_no_local_branch_pushes_nothing(
    ledger: RunLedger, workspace: Path, bare_remote: Path
) -> None:
    """REC-INT-1: with no local Attempt branch there are no commits to preserve."""
    ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
    push = push_interrupted_commits(
        workspace_path=workspace,
        branch_name="agent/7/1-add-a-greeting",
        remote_url=str(bare_remote),
        ledger=ledger,
        attempt_id="#7/1",
    )
    assert push.pushed is False
    assert push.remote_sha is None
    assert push.foreign is False
    assert ledger.get_effect("#7/1", INTERRUPTED_PUSH_KIND) is None


def test_push_preserves_local_commits_fast_forward(
    ledger: RunLedger, workspace: Path, bare_remote: Path
) -> None:
    """REC-INT-1: local commits are pushed fast-forward; the tree stays dirty."""
    ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
    branch = "agent/7/1-add-a-greeting"
    run_git(["checkout", "-q", "-b", branch], workspace)
    (workspace / "committed.txt").write_text("kept\n", encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", "local work"], workspace)
    (workspace / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    push = push_interrupted_commits(
        workspace_path=workspace,
        branch_name=branch,
        remote_url=str(bare_remote),
        ledger=ledger,
        attempt_id="#7/1",
    )
    assert push.pushed is True
    assert push.foreign is False
    assert push.uncommitted_count == 1
    assert push.remote_sha is not None
    remote_branches = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], bare_remote
    )
    assert branch in remote_branches.splitlines()
    listed = run_git(["ls-tree", "-r", "--name-only", branch], bare_remote)
    assert "committed.txt" in listed.splitlines()
    assert "dirty.txt" not in listed.splitlines()
    assert attempt_branch_sha(workspace, branch) == push.remote_sha


def test_push_over_a_foreign_head_pushes_nothing(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-3: a foreign remote head means no push and a note (P-REC-4)."""
    ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
    branch = "agent/7/1-add-a-greeting"
    run_git(["checkout", "-q", "-b", branch], workspace)
    (workspace / "ours.txt").write_text("ours\n", encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", "our work"], workspace)
    other = tmp_path / "human-workspace"
    run_git(["clone", "-q", str(bare_remote), str(other)], cwd=tmp_path)
    run_git(["checkout", "-q", "-b", branch], other)
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    run_git(["add", "."], other)
    run_git(["commit", "-q", "-m", "human work"], other)
    run_git(["push", "-q", str(bare_remote), f"HEAD:refs/heads/{branch}"], other)
    push = push_interrupted_commits(
        workspace_path=workspace,
        branch_name=branch,
        remote_url=str(bare_remote),
        ledger=ledger,
        attempt_id="#7/1",
    )
    assert push.pushed is False
    assert push.foreign is True
    assert push.remote_sha is not None
    listed = run_git(["ls-tree", "-r", "--name-only", branch], bare_remote)
    assert "ours.txt" not in listed.splitlines()
    assert "theirs.txt" in listed.splitlines()


def test_push_ignores_a_coincidental_sha_outside_the_sha_keys(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-3: a sha quoted under another payload key is never ours.

    Only the sha-carrying keys (``commit_sha``, ``remote_sha``,
    ``head_sha``) and the effect ``remote_id`` identify our pushes: a
    foreign head that merely appears in a note must still mean no push,
    with no push intent recorded.
    """
    ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
    branch = "agent/7/1-add-a-greeting"
    run_git(["checkout", "-q", "-b", branch], workspace)
    (workspace / "ours.txt").write_text("ours\n", encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", "our work"], workspace)
    other = tmp_path / "human-workspace"
    run_git(["clone", "-q", str(bare_remote), str(other)], cwd=tmp_path)
    run_git(["checkout", "-q", "-b", branch], other)
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    run_git(["add", "."], other)
    run_git(["commit", "-q", "-m", "human work"], other)
    run_git(["push", "-q", str(bare_remote), f"HEAD:refs/heads/{branch}"], other)
    foreign_sha = run_git(["rev-parse", "HEAD"], other).strip()
    assert len(foreign_sha) == 40
    ledger.record_effect_intent(
        "#7/1", "decoy-note", payload={"detail": foreign_sha}
    )
    push = push_interrupted_commits(
        workspace_path=workspace,
        branch_name=branch,
        remote_url=str(bare_remote),
        ledger=ledger,
        attempt_id="#7/1",
    )
    assert push.pushed is False
    assert push.foreign is True
    assert ledger.get_effect("#7/1", INTERRUPTED_PUSH_KIND) is None
    listed = run_git(["ls-tree", "-r", "--name-only", branch], bare_remote)
    assert "ours.txt" not in listed.splitlines()
    assert "theirs.txt" in listed.splitlines()


def test_push_recognises_our_own_receipt_as_ours(
    ledger: RunLedger, workspace: Path, bare_remote: Path
) -> None:
    """Our own earlier push is never misread as foreign: already pushed is a no-op."""
    ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
    branch = "agent/7/1-add-a-greeting"
    run_git(["checkout", "-q", "-b", branch], workspace)
    (workspace / "ours.txt").write_text("ours\n", encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", "our work"], workspace)
    first = push_interrupted_commits(
        workspace_path=workspace,
        branch_name=branch,
        remote_url=str(bare_remote),
        ledger=ledger,
        attempt_id="#7/1",
    )
    assert first.pushed is True
    second = push_interrupted_commits(
        workspace_path=workspace,
        branch_name=branch,
        remote_url=str(bare_remote),
        ledger=ledger,
        attempt_id="#7/1",
    )
    assert second.pushed is False
    assert second.foreign is False
    assert second.remote_sha == first.remote_sha


def test_decide_open_labeled_issue_is_interrupted() -> None:
    tracker = FakeTracker()
    assert decide_interrupted_outcome(
        tracker=tracker, issue_number=7, selection_label=tracker.selection_label
    ) == ("failed", INTERRUPTED_REASON)


def test_decide_closed_issue_is_abandoned() -> None:
    """REC-INT-2: an issue closed during the outage ends ``abandoned``."""
    from coding_agent.tracker.port import CandidateIssue

    tracker = FakeTracker()
    tracker.candidates[7] = CandidateIssue(
        number=7,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=False,
        assignees=(),
        labels=(tracker.selection_label,),
        created_at="2026-01-01T00:00:00Z",
    )
    assert decide_interrupted_outcome(
        tracker=tracker, issue_number=7, selection_label=tracker.selection_label
    ) == ("abandoned", "cancelled")


def test_finalize_records_interrupted_with_dirty_count_and_snapshot(
    ledger: RunLedger, workspace: Path, bare_remote: Path
) -> None:
    """The routine records ``failed`` / ``interrupted`` with the file count (§15)."""
    ledger.open_attempt(
        "#7/1", effective_configuration=EFFECTIVE_CONFIGURATION, assigned_by_us=True
    )
    (workspace / "dirty-a.txt").write_text("a\n", encoding="utf-8")
    (workspace / "dirty-b.txt").write_text("b\n", encoding="utf-8")
    tracker = FakeTracker()
    result = finalize_interrupted_attempt(
        ledger=ledger,
        tracker=tracker,
        workspace_path=workspace,
        attempt_id="#7/1",
        issue_number=7,
        attempt_number=1,
        issue_title="Add a greeting",
        remote_url=str(bare_remote),
        base_revision=run_git(["rev-parse", "HEAD"], workspace),
        workflow_id="implement",
        configuration_digest="digest",
        selection_label=tracker.selection_label,
    )
    assert (result.outcome, result.reason) == ("failed", INTERRUPTED_REASON)
    assert result.uncommitted_count == 2
    stored = ledger.get_outcome("#7/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", INTERRUPTED_REASON)
    assert stored.writes == (
        "terminal-comment",
        "remove-selection-label",
        "add-ready-for-human",
        "release-assignment",
    )
    assert "2 uncommitted file(s)" in stored.terminal_comment_body
    assert "Continuation Snapshot" in stored.terminal_comment_body
    events = [record for record in ledger.records("#7/1") if record.kind == INTERRUPTED_EVENT_KIND]
    assert len(events) == 1
    assert events[0].payload["uncommitted_count"] == 2
    assert "post_terminal_comment" not in tracker.call_names()


def test_finalize_is_idempotent_where_an_outcome_is_recorded(
    ledger: RunLedger, workspace: Path, bare_remote: Path
) -> None:
    """A re-entered finalization returns the recorded outcome without duplicating it."""
    ledger.open_attempt(
        "#7/1", effective_configuration=EFFECTIVE_CONFIGURATION, assigned_by_us=True
    )
    tracker = FakeTracker()
    first = finalize_interrupted_attempt(
        ledger=ledger,
        tracker=tracker,
        workspace_path=workspace,
        attempt_id="#7/1",
        issue_number=7,
        attempt_number=1,
        issue_title="Add a greeting",
        remote_url=str(bare_remote),
        base_revision=run_git(["rev-parse", "HEAD"], workspace),
        workflow_id="implement",
        configuration_digest="digest",
        selection_label=tracker.selection_label,
    )
    second = finalize_interrupted_attempt(
        ledger=ledger,
        tracker=tracker,
        workspace_path=workspace,
        attempt_id="#7/1",
        issue_number=7,
        attempt_number=1,
        issue_title="Add a greeting",
        remote_url=str(bare_remote),
        base_revision=run_git(["rev-parse", "HEAD"], workspace),
        workflow_id="implement",
        configuration_digest="digest",
        selection_label=tracker.selection_label,
    )
    assert (second.outcome, second.reason) == (first.outcome, first.reason)
    events = [record for record in ledger.records("#7/1") if record.kind == INTERRUPTED_EVENT_KIND]
    assert len(events) == 1


def test_perform_recorded_terminal_writes_runs_the_recorded_order(
    ledger: RunLedger, workspace: Path, bare_remote: Path
) -> None:
    """M8's half of the shared routine: the recorded writes are performed once."""
    ledger.open_attempt(
        "#7/1", effective_configuration=EFFECTIVE_CONFIGURATION, assigned_by_us=True
    )
    tracker = FakeTracker()
    finalize_interrupted_attempt(
        ledger=ledger,
        tracker=tracker,
        workspace_path=workspace,
        attempt_id="#7/1",
        issue_number=7,
        attempt_number=1,
        issue_title="Add a greeting",
        remote_url=str(bare_remote),
        base_revision=run_git(["rev-parse", "HEAD"], workspace),
        workflow_id="implement",
        configuration_digest="digest",
        selection_label=tracker.selection_label,
    )
    perform_recorded_terminal_writes(
        ledger=ledger, tracker=tracker, attempt_id="#7/1", issue_number=7
    )
    assert tracker.call_names().count("post_terminal_comment") == 1
    assert tracker.call_names() == [
        "read_issue_state",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ]
