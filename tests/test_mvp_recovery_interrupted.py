"""Startup recovery of an Attempt lost before its Delivery Intent (M8).

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup runs
recovery before any selection), §12 (the Delivery Snapshot is committed and
pushed before the final checks), §13 (fast-forward only; the remote ref is
ours when it equals the last receipt's sha or an open push intent's sha),
§14 (the terminal writes, in order), §15 (the terminal comment carries the
last persisted Continuation Snapshot only if its commit is an ancestor of
the pushed head) and §17 (the recovery table row "Claim confirmed, no
outcome recorded": preserve, decide, record, then perform).

MVP row ids: P-REC-1, P-REC-3, P-REC-4, REC-INT-1, REC-INT-2, REC-INT-3,
REC-INT-4, REC-INT-5, REC-INT-6.

Each test injects the crash by preparing the ledger, the workspace and the
remote exactly as the crashed process left them, then runs startup recovery
and asserts the recovery-table outcome. Recovery never opens a pull request
(P-REC-3), never overwrites pushed commits (P-REC-4), and never calls the
model.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import run_git
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.context_handoff import ContinuationSnapshot
from coding_agent.runtime.review_snapshot import review_snapshot_write_kind
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker.recovery import (
    InterruptedRecovery,
    recover_unfinished_attempts,
)
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_tracker import FakeTracker
from test_mvp_attempt_graph import IDENTITY  # noqa: F401 (shared fixtures)
from test_mvp_attempt_graph import bare_remote  # noqa: F401 (shared fixture)

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"
ISSUE_NUMBER = 7
ATTEMPT_NUMBER = 1
ATTEMPT_ID = "#7/1"
ISSUE_TITLE = "Add a greeting"
BRANCH = "agent/7/1-add-a-greeting"


def _pinned(label: str = SELECTION_LABEL) -> dict[str, object]:
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
            "selector": {"label": label},
        },
    }


def _candidate(
    number: int,
    *,
    is_open: bool = True,
    assignees: tuple[str, ...] = (IDENTITY.login,),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title=ISSUE_TITLE,
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=is_open,
        assignees=assignees,
        labels=labels,
        created_at="2026-10-01T00:00:00Z",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


def _confirm_attempt(ledger: RunLedger, base_revision: str) -> str:
    """Open `#7/1` as the Claim left it, then confirm it: the crash came after."""
    ledger.open_attempt(
        ATTEMPT_ID,
        effective_configuration=_pinned(),
        fingerprint="fingerprint",
        base_revision=base_revision,
        agent_identity_id=IDENTITY.account_id,
        assigned_by_us=True,
    )
    ledger.set_attempt_state(ATTEMPT_ID, "claim-confirmed")
    return ATTEMPT_ID


def _recovery(
    workspace: Path, bare_remote: Path, tmp_path: Path
) -> InterruptedRecovery:
    return InterruptedRecovery(
        workspace_path=workspace,
        remote_url=str(bare_remote),
        evidence_root=tmp_path / "evidence",
    )


def _recover(
    tracker: FakeTracker,
    ledger: RunLedger,
    workspace: Path,
    bare_remote: Path,
    tmp_path: Path,
) -> None:
    summary = recover_unfinished_attempts(
        tracker,
        ledger,
        agent_login=IDENTITY.login,
        interrupted=_recovery(workspace, bare_remote, tmp_path),
    )
    assert summary.settled == (ATTEMPT_ID,)
    assert summary.pending == ()


def _commit(workspace: Path, name: str, content: str) -> str:
    (workspace / name).write_text(content, encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", f"work {name}"], workspace)
    return run_git(["rev-parse", "HEAD"], workspace).strip()


# --- REC-INT-1: crash during a step -------------------------------------------


def test_rec_int_1_local_commits_pushed_dirty_tree_not_committed(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-1, P-REC-1: both local commits are pushed fast-forward; the
    dirty tree is not committed, the comment states its file count, and the
    outcome is ``failed`` / ``interrupted`` with exactly one comment."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    _commit(workspace, "one.txt", "one\n")
    _commit(workspace, "two.txt", "two\n")
    (workspace / "dirty-a.txt").write_text("a\n", encoding="utf-8")
    (workspace / "dirty-b.txt").write_text("b\n", encoding="utf-8")
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    listed = run_git(["ls-tree", "-r", "--name-only", BRANCH], bare_remote)
    assert "one.txt" in listed.splitlines()
    assert "two.txt" in listed.splitlines()
    assert "dirty-a.txt" not in listed.splitlines()
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")
    assert "2 uncommitted file(s)" in stored.terminal_comment_body
    assert tracker.call_names().count("post_terminal_comment") == 1
    assert "open_pull_request" not in tracker.call_names()
    assert stored.writes == (
        "terminal-comment",
        "remove-selection-label",
        "add-ready-for-human",
        "release-assignment",
    )
    assert tracker.call_names()[-4:] == [
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ]
    assert tracker.candidates[ISSUE_NUMBER].assignees == ()
    assert SELECTION_LABEL not in tracker.candidates[ISSUE_NUMBER].labels
    assert READY_FOR_HUMAN in tracker.candidates[ISSUE_NUMBER].labels
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


# --- REC-INT-2: the issue closed during the outage ----------------------------


def test_rec_int_2_issue_closed_during_outage_gives_abandoned(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-2: the commits are still preserved, but the re-read finds the
    issue closed, so the outcome is ``abandoned``."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    _commit(workspace, "one.txt", "one\n")
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER, is_open=False)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    listed = run_git(["ls-tree", "-r", "--name-only", BRANCH], bare_remote)
    assert "one.txt" in listed.splitlines()
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("abandoned", "cancelled")
    assert tracker.call_names().count("post_terminal_comment") == 1
    assert "open_pull_request" not in tracker.call_names()


# --- REC-INT-3: a foreign push during the outage ------------------------------


def test_rec_int_3_foreign_push_means_no_push_and_a_note(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-3, P-REC-4: a human pushed to the Attempt branch during the
    outage, so there is no push and no force; the outcome is ``interrupted``
    with a note, and the remote is untouched."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    _commit(workspace, "ours.txt", "ours\n")
    other = tmp_path / "human-workspace"
    run_git(["clone", "-q", str(bare_remote), str(other)], cwd=tmp_path)
    run_git(["checkout", "-q", "-b", BRANCH], other)
    _commit(other, "theirs.txt", "theirs\n")
    run_git(
        ["push", "-q", str(bare_remote), f"HEAD:refs/heads/{BRANCH}"], other
    )
    foreign_sha = run_git(["rev-parse", "HEAD"], other).strip()
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")
    assert "foreign push" in stored.terminal_comment_body
    listed = run_git(["ls-tree", "-r", "--name-only", BRANCH], bare_remote)
    assert "ours.txt" not in listed.splitlines()
    assert "theirs.txt" in listed.splitlines()
    assert run_git(["rev-parse", BRANCH], bare_remote).strip() == foreign_sha
    assert "open_pull_request" not in tracker.call_names()


# --- REC-INT-4: crash after the review-snapshot push --------------------------


def test_rec_int_4_open_review_snapshot_intent_is_receipted_as_ours(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-4: the review-snapshot push landed but its receipt was never
    recorded. The remote sha equals the push intent's sha, so it is
    recognised as ours and the receipt is recorded — not misread as foreign."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    local_sha = _commit(workspace, "reviewed.txt", "reviewed\n")
    write_kind = review_snapshot_write_kind(1)
    ledger.record_effect_intent(
        ATTEMPT_ID,
        write_kind,
        remote_id=local_sha,
        payload={"branch": BRANCH, "commit_sha": local_sha, "attempt_id": ATTEMPT_ID},
    )
    # The push the crashed process made after recording the intent.
    run_git(
        ["push", "-q", str(bare_remote), f"HEAD:refs/heads/{BRANCH}"], workspace
    )
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    effect = ledger.get_effect(ATTEMPT_ID, write_kind)
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.remote_id == local_sha
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")
    assert "foreign" not in stored.terminal_comment_body


def test_rec_int_4_non_push_intent_with_branch_is_not_receipted(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-4, §13: only push write kinds are receipted. A non-push
    intent whose payload happens to carry a ``branch`` and whose sha the
    remote holds stays open — it is never mistaken for a push."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    local_sha = _commit(workspace, "reviewed.txt", "reviewed\n")
    ledger.record_effect_intent(
        ATTEMPT_ID,
        "terminal-comment",
        remote_id=local_sha,
        payload={"branch": BRANCH},
    )
    run_git(
        ["push", "-q", str(bare_remote), f"HEAD:refs/heads/{BRANCH}"], workspace
    )
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    effect = ledger.get_effect(ATTEMPT_ID, "terminal-comment")
    assert effect is not None
    assert effect.status == "intent"


# --- REC-INT-5: the Continuation Snapshot in the recovery comment -------------


def _persist_snapshot(
    ledger: RunLedger,
    tmp_path: Path,
    *,
    commit: str,
    base_revision: str,
) -> str:
    """Persist one step-boundary snapshot the crashed Attempt left behind."""
    snapshot = ContinuationSnapshot.from_payload(
        {
            "version": 1,
            "attempt_id": ATTEMPT_ID,
            "base_revision": base_revision,
            "branch": BRANCH,
            "commit": commit,
            "changed_files": ["one.txt"],
            "commands": [],
            "validation_evidence": [],
            "unresolved_questions": [],
            "next_intended_action": "keep going",
        }
    )
    encoded = snapshot.to_json()
    artifact_ref = f"continuation-snapshots/{snapshot.digest}.json"
    evidence_dir = tmp_path / "evidence" / str(ISSUE_NUMBER) / str(ATTEMPT_NUMBER)
    target = evidence_dir / "tool-results" / artifact_ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded, encoding="utf-8")
    ledger.record(
        ATTEMPT_ID,
        "step_boundary",
        BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals()),
        detail="step boundary",
        artifact_ref=artifact_ref,
        payload={"snapshot_digest": snapshot.digest, "snapshot_version": 1},
    )
    return snapshot.digest


def test_rec_int_5_snapshot_carried_only_when_ancestor_of_pushed_head(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-5: the recovery comment carries the last persisted snapshot
    because its commit is an ancestor of the pushed head."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    first = _commit(workspace, "one.txt", "one\n")
    digest = _persist_snapshot(ledger, tmp_path, commit=first, base_revision=base)
    _commit(workspace, "two.txt", "two\n")
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert "Continuation Snapshot" in stored.terminal_comment_body
    assert digest in stored.terminal_comment_body


def test_rec_int_5_snapshot_diverged_from_pushed_head_is_dropped(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-5: a persisted snapshot whose commit is not an ancestor of the
    pushed head is dropped — the comment carries none."""
    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    _commit(workspace, "one.txt", "one\n")
    run_git(["checkout", "-q", "-b", "other-branch"], workspace)
    diverged = _commit(workspace, "diverged.txt", "diverged\n")
    _persist_snapshot(ledger, tmp_path, commit=diverged, base_revision=base)
    run_git(["checkout", "-q", BRANCH], workspace)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert "Continuation Snapshot" not in stored.terminal_comment_body


# --- REC-INT-6: crash during the final checks ---------------------------------


def test_rec_int_6_crash_during_final_checks_keeps_delivery_snapshot(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """REC-INT-6, P-REC-3: the Delivery Snapshot is already pushed and is
    kept. The outcome is ``interrupted`` and no pull request is opened."""
    from coding_agent.runtime.git import Workspace
    from coding_agent.runtime.delivery import publish_delivery_snapshot

    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    (workspace / "feature.txt").write_text("feature\n", encoding="utf-8")
    published = publish_delivery_snapshot(
        workspace=Workspace(path=workspace, base_branch="main", base_revision=base),
        ledger=ledger,
        attempt_id=ATTEMPT_ID,
        issue_number=ISSUE_NUMBER,
        attempt_number=ATTEMPT_NUMBER,
        issue_title=ISSUE_TITLE,
        identity=IDENTITY,
        remote_url=str(bare_remote),
    )
    assert published.pushed is True
    delivery_sha = published.commit_sha
    assert delivery_sha is not None
    # The crash: final checks run against the delivered tree, then the
    # process is lost before any outcome is recorded.
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    assert run_git(["rev-parse", BRANCH], bare_remote).strip() == delivery_sha
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")
    assert "open_pull_request" not in tracker.call_names()
    assert tracker.call_names().count("post_terminal_comment") == 1


# --- Recovery through the poll loop ------------------------------------------


def test_recovery_of_interrupted_attempt_settles_then_selects_again(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """The loop recovers the interrupted Attempt to ``finished`` and selects
    new work on the next sweep; intake was closed until the settle."""
    from coding_agent.worker.claim import ClaimedAttempt
    from coding_agent.runtime.graph.attempt import AttemptResult

    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    _commit(workspace, "one.txt", "one\n")
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)
    tracker.candidates[8] = _candidate(8, assignees=())
    ran: list[int] = []

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ran.append(claimed.issue_number)
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="delivered", reason="verified-completion"
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id,
            outcome="delivered",
            reason="verified-completion",
        )

    from coding_agent.worker.claim import ClaimConfig

    loop_config = WorkerLoopConfig(
        claim_config=ClaimConfig(
            selection_label=SELECTION_LABEL,
            ready_for_human=READY_FOR_HUMAN,
            agent_login=IDENTITY.login,
            agent_identity_id=IDENTITY.account_id,
            effective_configuration=_pinned(),
        ),
        poll_interval_seconds=60.0,
        interrupted_recovery=_recovery(workspace, bare_remote, tmp_path),
    )
    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=loop_config,
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"
    assert ledger.get_outcome(ATTEMPT_ID) is not None
    assert ran == [8]


# --- Recovery never calls the model ------------------------------------------


def test_interrupted_recovery_takes_no_model() -> None:
    """§17: recovery of the interrupted Attempt cannot call the model — no
    recovery entry point accepts one, asserted structurally on the signatures."""
    from coding_agent.worker import recovery  # noqa: PLC0415

    for name in (
        "recover_unfinished_attempts",
        "recover_open_attempt",
        "recover_interrupted_attempt",
        "validate_recovery_configs",
        "pinned_recovery_config",
    ):
        parameters = inspect.signature(getattr(recovery, name)).parameters
        assert "model" not in parameters
        assert "chat_model" not in parameters
        assert "provider" not in parameters


# --- C-SUB-35: process loss with Sub-agents in flight --------------------------


def _subagent_kinds(ledger: RunLedger) -> list[str]:
    """The Sub-agent ledger event kinds for the test Attempt, in order."""
    from coding_agent.runtime.graph.subagent import (  # noqa: PLC0415
        SUBAGENT_ENDED_KIND,
        SUBAGENT_QUEUED_KIND,
        SUBAGENT_STARTED_KIND,
    )

    wanted = {SUBAGENT_QUEUED_KIND, SUBAGENT_STARTED_KIND, SUBAGENT_ENDED_KIND}
    return [record.kind for record in ledger.records(ATTEMPT_ID) if record.kind in wanted]


def test_c_sub_35_process_loss_with_unfinished_subagent_discloses_count(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """C-SUB-35: process loss with a started-but-unended Sub-agent.

    The ledger holds ``subagent_queued`` plus ``subagent_started`` and no
    ``subagent_ended``: recovery appends no Sub-agent event, settles the
    Attempt ``failed`` / ``interrupted``, and the comment discloses the one
    Sub-agent without a report. Recovery calls no model (asserted structurally
    by ``test_interrupted_recovery_takes_no_model``; no Sub-agent could run
    without one).
    """
    from coding_agent.runtime.graph.subagent import (  # noqa: PLC0415
        SUBAGENT_QUEUED_KIND,
        SUBAGENT_STARTED_KIND,
    )

    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    snapshot = BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    ledger.record(
        ATTEMPT_ID,
        SUBAGENT_QUEUED_KIND,
        snapshot,
        detail="subagent call-1 queued: unfinished work",
        payload={"call_id": "call-1", "description": "unfinished work"},
    )
    ledger.record(
        ATTEMPT_ID,
        SUBAGENT_STARTED_KIND,
        snapshot,
        detail="subagent call-1 started: unfinished work",
        payload={"call_id": "call-1", "description": "unfinished work"},
    )
    before = _subagent_kinds(ledger)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    assert _subagent_kinds(ledger) == before
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")
    assert (
        "1 Sub-agent had no Sub-agent Report at interruption."
        in stored.terminal_comment_body
    )


def test_c_sub_35_finished_subagent_needs_no_disclosure_line(
    ledger: RunLedger, workspace: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """C-SUB-35: a fully ended Sub-agent leaves no disclosure line.

    ``subagent_queued`` plus ``subagent_started`` plus ``subagent_ended`` means
    no Sub-agent lacked a report at interruption: the comment omits the
    Sub-agent line, and recovery still appends no Sub-agent event.
    """
    from coding_agent.runtime.graph.subagent import (  # noqa: PLC0415
        SUBAGENT_ENDED_KIND,
        SUBAGENT_QUEUED_KIND,
        SUBAGENT_STARTED_KIND,
    )

    base = run_git(["rev-parse", "HEAD"], workspace).strip()
    _confirm_attempt(ledger, base)
    run_git(["checkout", "-q", "-b", BRANCH], workspace)
    snapshot = BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    ledger.record(
        ATTEMPT_ID,
        SUBAGENT_QUEUED_KIND,
        snapshot,
        detail="subagent call-1 queued: finished work",
        payload={"call_id": "call-1", "description": "finished work"},
    )
    ledger.record(
        ATTEMPT_ID,
        SUBAGENT_STARTED_KIND,
        snapshot,
        detail="subagent call-1 started: finished work",
        payload={"call_id": "call-1", "description": "finished work"},
    )
    ledger.record(
        ATTEMPT_ID,
        SUBAGENT_ENDED_KIND,
        snapshot,
        detail="subagent call-1 ended: reported / reported",
        payload={
            "call_id": "call-1",
            "description": "finished work",
            "status": "reported",
            "reason": "reported",
            "report": "findings",
        },
    )
    before = _subagent_kinds(ledger)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[ISSUE_NUMBER] = _candidate(ISSUE_NUMBER)

    _recover(tracker, ledger, workspace, bare_remote, tmp_path)

    assert _subagent_kinds(ledger) == before
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "interrupted")
    assert "Sub-agent" not in stored.terminal_comment_body
