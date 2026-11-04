"""Recorded-outcome recovery: finish, never re-decide (M8, ADR 0017).

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (the recovery
table row "A Delivery Intent recorded, writes partly done": check the
branch, find the pull request by head branch and Attempt Marker, then the
comment, labels and assignment, each looked up before it is written; the
row "An outcome without a pull request recorded, writes partly done":
finish the remaining writes), §14 ("Recording before publishing": the
Terminal Outcome is recorded before its first write, so recovery finishes
it and never re-decides it) and §17 (recovery never calls the model;
pinned values only; rendered bodies come from the ledger).

MVP row ids: REC-OUT-1, REC-OUT-2, REC-DEL-1, REC-DEL-2, REC-DEL-3,
REC-DEL-4, REC-DEL-5, REC-DEL-6, P-REC-2, L3-DEL-15, C-OUT-1.

A crash is injected after each write and before its receipt — for the
tracker writes by raising out of a ``FakeTracker`` that already performed
the write, for the two pushes by raising out of ``push_branch`` after the
push landed — and the rerun must cause no duplicate (L3-DEL-15). The write
kinds are the review-snapshot push, the delivery push, the pull request,
the comment, the label removal, ``ready-for-human`` and the assignment
removal.
"""

from __future__ import annotations

import inspect
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import init_origin_repo
from coding_agent.github.errors import CredentialFailure
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.runtime import delivery as delivery_module
from coding_agent.runtime import review_snapshot as review_snapshot_module
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.delivery import publish_delivery_snapshot
from coding_agent.runtime.git import (
    Workspace,
    checkout_workspace,
    ensure_mirror,
    remote_ref_sha,
)
from coding_agent.runtime.git import push_branch as git_push_branch
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.runtime.publication import (
    DELIVERY_INTENT_KIND,
    PUBLISH_WRITE_KIND,
    TERMINAL_COMMENT_KIND,
    attempt_marker,
    render_terminal_comment,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.runtime.review_snapshot import publish_review_snapshot
from coding_agent.tracker.port import PullRequest
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.recovery import (
    recover_recorded_outcome,
    recover_unfinished_attempts,
)
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from test_mvp_attempt_graph import IDENTITY  # noqa: F401  (shared identity)

ATTEMPT_ID = "#7/1"
ISSUE_NUMBER = 7
BRANCH = "agent/7/1-add-a-greeting"
INTENT_SHA = "a" * 40
SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


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
    number: int = ISSUE_NUMBER,
    *,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
) -> Any:
    from coding_agent.tracker.port import CandidateIssue

    return CandidateIssue(
        number=number,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=True,
        assignees=assignees,
        labels=labels,
        created_at="2026-10-01T00:00:00Z",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _tracker() -> FakeTracker:
    """The fake bound to the Agent Identity's numeric id (§13, §18)."""
    return FakeTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)


def _terminal_body(
    ledger: RunLedger,
    attempt_id: str = ATTEMPT_ID,
    *,
    outcome: str,
    reason: str,
    pull_request_url: str | None = None,
    branch_name: str | None = BRANCH,
) -> str:
    """The recorded terminal comment body, rendered once before any write."""
    attempt = ledger.get_attempt(attempt_id)
    return render_terminal_comment(
        attempt_id=attempt_id,
        outcome=outcome,
        reason=reason,
        pull_request_url=pull_request_url,
        branch_name=branch_name,
        workflow_id="implement",
        configuration_digest=attempt.effective_configuration_digest,
    )


def _record_failed_outcome(
    ledger: RunLedger,
    attempt_id: str = ATTEMPT_ID,
    *,
    reason: str,
    assigned_by_us: bool = True,
) -> str:
    """Open the Attempt and record a failed outcome with its writes (§14)."""
    ledger.open_attempt(
        attempt_id,
        effective_configuration=_pinned(),
        agent_identity_id=IDENTITY.account_id,
        assigned_by_us=assigned_by_us,
    )
    body = _terminal_body(ledger, attempt_id, outcome="failed", reason=reason)
    ledger.record_terminal_outcome(
        attempt_id,
        outcome="failed",
        reason=reason,
        terminal_comment_body=body,
        writes=list(terminal_writes_for_outcome(outcome="failed")),
    )
    return body


def _record_delivered_outcome(
    ledger: RunLedger,
    attempt_id: str = ATTEMPT_ID,
    *,
    branch: str = BRANCH,
    head_sha: str = INTENT_SHA,
) -> tuple[str, str]:
    """Record the Delivery Intent event plus the ``delivered`` outcome (§12)."""
    ledger.open_attempt(
        attempt_id,
        effective_configuration=_pinned(),
        agent_identity_id=IDENTITY.account_id,
        assigned_by_us=True,
    )
    pr_body = (
        f"Closes #{ISSUE_NUMBER}\n"
        f"{attempt_marker(attempt_id, kind='delivery')}\n"
        "\nrecorded pull-request body"
    )
    comment_body = _terminal_body(
        ledger, attempt_id, outcome="delivered", reason="verified-completion"
    )
    ledger.record(
        attempt_id,
        DELIVERY_INTENT_KIND,
        BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals()),
        detail=f"delivery intent: delivered {branch}@{head_sha}",
        payload={
            "attempt_id": attempt_id,
            "branch": branch,
            "head_sha": head_sha,
            "verdict": "verified-completion",
            "writes": [
                PUBLISH_WRITE_KIND,
                *terminal_writes_for_outcome(outcome="delivered"),
            ],
            "pull_request_body": pr_body,
            "terminal_comment_body": comment_body,
        },
    )
    ledger.record_terminal_outcome(
        attempt_id,
        outcome="delivered",
        reason="verified-completion",
        pull_request_body=pr_body,
        terminal_comment_body=comment_body,
        writes=(
            PUBLISH_WRITE_KIND,
            *terminal_writes_for_outcome(outcome="delivered"),
        ),
    )
    return pr_body, comment_body


def _posted_bodies(tracker: FakeTracker) -> list[str]:
    return [
        str(args[1]) for name, args in tracker.calls if name == "post_terminal_comment"
    ]


# --- REC-OUT-1: failed-limit recorded, crash after the comment ----------------


def test_rec_out_1_crash_after_comment_finishes_writes_without_a_second(
    ledger: RunLedger,
) -> None:
    """REC-OUT-1: the comment is the receipt; labels and assignment finish."""
    body = _record_failed_outcome(ledger, reason="failed-limit: wall_clock")
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.post_terminal_comment(ISSUE_NUMBER, body)
    tracker.calls.clear()

    settled = recover_recorded_outcome(
        tracker, ledger, ledger.get_attempt(ATTEMPT_ID), agent_login=IDENTITY.login
    )

    assert settled is True
    assert _posted_bodies(tracker) == []
    assert tracker.call_names().count("remove_selection_label") == 1
    assert tracker.call_names().count("add_ready_for_human") == 1
    assert tracker.call_names().count("release_assignment") == 1
    assert tracker.candidates[ISSUE_NUMBER].labels == (READY_FOR_HUMAN,)
    assert tracker.candidates[ISSUE_NUMBER].assignees == ()
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "failed-limit: wall_clock")
    assert stored.terminal_comment_body == body
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


def test_rec_out_1_credential_failure_after_record_recovers_on_restart(
    ledger: RunLedger,
) -> None:
    """REC-OUT-1 through the #154 path: a 401 after the outcome is recorded.

    The graph's ``CredentialFailure`` leaves the Attempt
    ``outcome-recorded`` — not finalized — so the restart's recovery
    finishes the unfinished writes with a working credential, posting the
    comment exactly once and keeping the recorded ``failed-limit`` outcome.
    """
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(), labels=(SELECTION_LABEL,)
    )
    claim_config = ClaimConfig(
        selection_label=SELECTION_LABEL,
        agent_login=IDENTITY.login,
        agent_identity_id=IDENTITY.account_id,
        effective_configuration=_pinned(),
    )
    loop_config = WorkerLoopConfig(
        claim_config=claim_config, poll_interval_seconds=60.0
    )

    def run_one_crash(claimed: ClaimedAttempt) -> AttemptResult:
        body = _terminal_body(
            ledger,
            claimed.attempt_id,
            outcome="failed",
            reason="failed-limit: wall_clock",
        )
        ledger.record_terminal_outcome(
            claimed.attempt_id,
            outcome="failed",
            reason="failed-limit: wall_clock",
            terminal_comment_body=body,
            writes=list(terminal_writes_for_outcome(outcome="failed")),
        )
        raise CredentialFailure("POST", "/repos/o/r/issues/7/comments")

    halted = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=loop_config,
        run_one_attempt=run_one_crash,
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert halted == 1
    assert ledger.get_attempt(ATTEMPT_ID).state == "outcome-recorded"
    assert "post_terminal_comment" not in tracker.call_names()

    def run_one_idle(claimed: ClaimedAttempt) -> AttemptResult:
        raise AssertionError("no new Attempt may start before recovery settles")

    restarted = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=loop_config,
        run_one_attempt=run_one_idle,
        sleep=lambda seconds: None,
        max_sweeps=3,
    )

    assert restarted == 0
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"
    assert _posted_bodies(tracker) == [
        ledger.get_outcome(ATTEMPT_ID).terminal_comment_body  # type: ignore[union-attr]
    ]
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "failed-limit: wall_clock")


# --- REC-OUT-2: readiness refusal recorded, crash before the comment ----------


def test_rec_out_2_readiness_refusal_posts_exactly_one_comment(
    ledger: RunLedger,
) -> None:
    """REC-OUT-2: no comment exists; recovery posts exactly one with the reason."""
    body = _record_failed_outcome(ledger, reason="missing-readiness-fact")
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )

    assert recover_recorded_outcome(
        tracker, ledger, ledger.get_attempt(ATTEMPT_ID), agent_login=IDENTITY.login
    )

    posted = _posted_bodies(tracker)
    assert len(posted) == 1
    assert posted[0] == body
    assert "missing-readiness-fact" in posted[0]
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "missing-readiness-fact")
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


# --- REC-DEL-1: crash after the open, before its receipt ----------------------


def test_rec_del_1_crash_after_open_finds_the_pull_request(
    ledger: RunLedger,
) -> None:
    """REC-DEL-1, P-REC-2: the PR is found by branch and marker; none created."""
    pr_body, _comment_body = _record_delivered_outcome(ledger)
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.open_pull_request(
        title="Add a greeting",
        head_branch=BRANCH,
        base_branch="main",
        body=pr_body,
    )
    opened_url = tracker.pull_requests[0].url
    tracker.calls.clear()

    assert recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
        remote_branch_sha=lambda branch: INTENT_SHA,
    )

    assert "open_pull_request" not in tracker.call_names()
    assert len(tracker.pull_requests) == 1
    posted = _posted_bodies(tracker)
    assert len(posted) == 1
    assert opened_url in posted[0]
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("delivered", "verified-completion")
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


# --- REC-DEL-2: crash after the comment, before the labels --------------------


def test_rec_del_2_crash_after_comment_finishes_without_a_second(
    ledger: RunLedger,
) -> None:
    """REC-DEL-2: comment and PR are receipts; labels and assignment finish."""
    pr_body, comment_body = _record_delivered_outcome(ledger)
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.open_pull_request(
        title="Add a greeting",
        head_branch=BRANCH,
        base_branch="main",
        body=pr_body,
    )
    tracker.post_terminal_comment(ISSUE_NUMBER, comment_body)
    tracker.calls.clear()

    assert recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
        remote_branch_sha=lambda branch: INTENT_SHA,
    )

    assert "open_pull_request" not in tracker.call_names()
    assert _posted_bodies(tracker) == []
    assert tracker.call_names().count("remove_selection_label") == 1
    assert "add_ready_for_human" not in tracker.call_names()
    assert tracker.call_names().count("release_assignment") == 1
    assert tracker.candidates[ISSUE_NUMBER].labels == ()
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("delivered", "verified-completion")


# --- REC-DEL-3: a human closed the pull request --------------------------------


def test_rec_del_3_human_closed_pull_request_is_a_withdrawal(
    ledger: RunLedger,
) -> None:
    """REC-DEL-3: never reopened; the outcome is ``abandoned`` with one comment."""
    pr_body, _comment_body = _record_delivered_outcome(ledger)
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.open_pull_request(
        title="Add a greeting",
        head_branch=BRANCH,
        base_branch="main",
        body=pr_body,
    )
    tracker.human_close_pull_request(1)
    tracker.calls.clear()

    assert recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
        remote_branch_sha=lambda branch: INTENT_SHA,
    )

    assert "open_pull_request" not in tracker.call_names()
    assert len(tracker.pull_requests) == 1
    assert tracker.pull_requests[0].state == "closed"
    posted = _posted_bodies(tracker)
    assert len(posted) == 1
    assert "withdrawal" in posted[0]
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("abandoned", "withdrawal")


# --- REC-DEL-4: the remote branch is not at the Intent's sha ------------------


def test_rec_del_4_branch_moved_opens_no_pull_request(ledger: RunLedger) -> None:
    """REC-DEL-4: no PR; ``failed`` / ``branch-moved`` with ``ready-for-human``."""
    _record_delivered_outcome(ledger)
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )

    assert recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
        remote_branch_sha=lambda branch: "b" * 40,
    )

    assert "open_pull_request" not in tracker.call_names()
    assert tracker.pull_requests == []
    posted = _posted_bodies(tracker)
    assert len(posted) == 1
    assert "branch-moved" in posted[0]
    assert tracker.candidates[ISSUE_NUMBER].labels == (READY_FOR_HUMAN,)
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "branch-moved")


def test_rec_del_4_absent_remote_branch_opens_no_pull_request(
    ledger: RunLedger,
) -> None:
    """REC-DEL-4: a missing remote ref is not at the Intent's sha either."""
    _record_delivered_outcome(ledger)
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )

    assert recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
        remote_branch_sha=lambda branch: None,
    )

    assert "open_pull_request" not in tracker.call_names()
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "branch-moved")


# --- REC-DEL-5 / REC-DEL-6 at the recovery level -------------------------------


def test_rec_del_5_recovery_finds_a_comment_past_one_hundred(
    ledger: RunLedger,
) -> None:
    """REC-DEL-5: ours sits past 100 human comments; no duplicate is written."""
    body = _record_failed_outcome(ledger, reason="failed-limit: wall_clock")
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.seed_comments([f"human note {index}" for index in range(100)])
    tracker.post_terminal_comment(ISSUE_NUMBER, body)
    tracker.seed_comments([f"later human note {index}" for index in range(100)])
    tracker.calls.clear()

    assert recover_recorded_outcome(
        tracker, ledger, ledger.get_attempt(ATTEMPT_ID), agent_login=IDENTITY.login
    )

    assert _posted_bodies(tracker) == []
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


def test_rec_del_6_recovery_ignores_a_foreign_marker_comment(
    ledger: RunLedger,
) -> None:
    """REC-DEL-6: a human comment carrying our marker is never our receipt."""
    _record_failed_outcome(ledger, reason="failed-limit: wall_clock")
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    marker = attempt_marker(ATTEMPT_ID, kind=TERMINAL_COMMENT_KIND)
    tracker.seed_foreign_marker_comment(marker)
    tracker.calls.clear()

    assert recover_recorded_outcome(
        tracker, ledger, ledger.get_attempt(ATTEMPT_ID), agent_login=IDENTITY.login
    )

    posted = _posted_bodies(tracker)
    assert len(posted) == 1
    assert marker in posted[0]
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


# --- L3-DEL-15: crash after each write, before its receipt ---------------------


class _InjectedCrash(Exception):
    """The scripted crash between a write and its receipt (L3-DEL-15)."""


class _CrashAfterWrite(FakeTracker):
    """A fake that crashes once, right after performing one write kind."""

    crash_after: str | None = None

    def _crash_if(self, name: str) -> None:
        if self.crash_after == name:
            self.crash_after = None
            raise _InjectedCrash(name)

    def open_pull_request(
        self, *, title: str, head_branch: str, base_branch: str, body: str
    ) -> PullRequest:
        opened = super().open_pull_request(
            title=title, head_branch=head_branch, base_branch=base_branch, body=body
        )
        self._crash_if("open_pull_request")
        return opened

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        super().post_terminal_comment(issue_number, body)
        self._crash_if("post_terminal_comment")

    def remove_selection_label(self, issue_number: int) -> None:
        super().remove_selection_label(issue_number)
        self._crash_if("remove_selection_label")

    def add_ready_for_human(self, issue_number: int) -> None:
        super().add_ready_for_human(issue_number)
        self._crash_if("add_ready_for_human")

    def release_assignment(self, issue_number: int) -> None:
        super().release_assignment(issue_number)
        self._crash_if("release_assignment")


def _crashing_tracker() -> _CrashAfterWrite:
    return _CrashAfterWrite(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )


def _recover(
    tracker: FakeTracker,
    ledger: RunLedger,
    remote_sha: str | None = INTENT_SHA,
) -> bool:
    return recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
        remote_branch_sha=lambda branch: remote_sha,
    )


@pytest.mark.parametrize(
    "write_kind",
    [
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ],
)
def test_l3_del_15_crash_after_each_terminal_write_writes_once(
    ledger: RunLedger, write_kind: str
) -> None:
    """L3-DEL-15: a crash after each terminal write still performs it once."""
    _record_failed_outcome(ledger, reason="failed-limit: wall_clock")
    tracker = _crashing_tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.crash_after = write_kind

    with pytest.raises(_InjectedCrash):
        _recover(tracker, ledger)
    assert recover_recorded_outcome(
        tracker,
        ledger,
        ledger.get_attempt(ATTEMPT_ID),
        agent_login=IDENTITY.login,
    )

    for name in (
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ):
        assert tracker.call_names().count(name) == 1, name
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("failed", "failed-limit: wall_clock")
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


def test_l3_del_15_crash_after_open_writes_one_pull_request(
    ledger: RunLedger,
) -> None:
    """L3-DEL-15: a crash after the open still opens exactly one PR (P-REC-2)."""
    _record_delivered_outcome(ledger)
    tracker = _crashing_tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    tracker.crash_after = "open_pull_request"

    with pytest.raises(_InjectedCrash):
        _recover(tracker, ledger)
    assert _recover(tracker, ledger)

    assert tracker.call_names().count("open_pull_request") == 1
    assert len(tracker.pull_requests) == 1
    assert len(_posted_bodies(tracker)) == 1
    stored = ledger.get_outcome(ATTEMPT_ID)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("delivered", "verified-completion")
    assert ledger.get_attempt(ATTEMPT_ID).state == "finished"


def _bare_remote(tmp_path: Path) -> Path:
    """A local bare git remote standing in for the Target Repository."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _checked_out_workspace(tmp_path: Path, bare_remote: Path) -> Workspace:
    """A workspace checked out from the local bare remote via its mirror."""
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(bare_remote))
    return checkout_workspace(mirror, tmp_path / "workspace")


def test_l3_del_15_crash_after_delivery_push_pushes_once(
    tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3-DEL-15: a crash after the delivery push still pushes exactly once."""
    bare = _bare_remote(tmp_path)
    workspace = _checked_out_workspace(tmp_path, bare)
    (workspace.path / "note.txt").write_text("delivery work\n", encoding="utf-8")
    ledger.open_attempt(
        ATTEMPT_ID,
        effective_configuration=_pinned(),
        agent_identity_id=IDENTITY.account_id,
    )
    pushes: list[str] = []
    real_push = git_push_branch

    def crashing_push(
        push_workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        pushes.append(branch_name)
        real_push(push_workspace, remote_url, branch_name, redact=redact, helper=helper)
        raise _InjectedCrash("delivery push")

    monkeypatch.setattr(delivery_module, "push_branch", crashing_push)
    with pytest.raises(_InjectedCrash):
        publish_delivery_snapshot(
            workspace=workspace,
            ledger=ledger,
            attempt_id=ATTEMPT_ID,
            issue_number=ISSUE_NUMBER,
            attempt_number=1,
            issue_title="Add a greeting",
            identity=IDENTITY,
            remote_url=str(bare),
        )
    effect = ledger.get_effect(ATTEMPT_ID, "delivery-snapshot-push")
    assert effect is not None
    assert effect.status == "intent"

    def counting_push(
        push_workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        pushes.append(branch_name)
        real_push(push_workspace, remote_url, branch_name, redact=redact, helper=helper)

    monkeypatch.setattr(delivery_module, "push_branch", counting_push)
    result = publish_delivery_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id=ATTEMPT_ID,
        issue_number=ISSUE_NUMBER,
        attempt_number=1,
        issue_title="Add a greeting",
        identity=IDENTITY,
        remote_url=str(bare),
    )

    assert pushes == [result.branch_name]
    assert result.pushed is False
    assert result.remote_sha == remote_ref_sha(str(bare), result.branch_name or "")
    assert ledger.get_effect(ATTEMPT_ID, "delivery-snapshot-push") is not None
    assert ledger.get_effect(ATTEMPT_ID, "delivery-snapshot-push").status == "receipt"  # type: ignore[union-attr]


def test_l3_del_15_crash_after_review_snapshot_push_pushes_once(
    tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3-DEL-15: a crash after the review-snapshot push pushes exactly once."""
    bare = _bare_remote(tmp_path)
    workspace = _checked_out_workspace(tmp_path, bare)
    (workspace.path / "note.txt").write_text("review work\n", encoding="utf-8")
    ledger.open_attempt(
        "#34/2",
        effective_configuration=_pinned(),
        agent_identity_id=IDENTITY.account_id,
    )
    pushes: list[str] = []
    real_push = git_push_branch

    def crashing_push(
        push_workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        pushes.append(branch_name)
        real_push(push_workspace, remote_url, branch_name, redact=redact, helper=helper)
        raise _InjectedCrash("review-snapshot push")

    monkeypatch.setattr(review_snapshot_module, "push_branch", crashing_push)
    with pytest.raises(_InjectedCrash):
        publish_review_snapshot(
            workspace=workspace,
            ledger=ledger,
            attempt_id="#34/2",
            issue_number=34,
            attempt_number=2,
            issue_title="Fix the loop!",
            identity=IDENTITY,
            remote_url=str(bare),
        )

    def counting_push(
        push_workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        pushes.append(branch_name)
        real_push(push_workspace, remote_url, branch_name, redact=redact, helper=helper)

    monkeypatch.setattr(review_snapshot_module, "push_branch", counting_push)
    result = publish_review_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(bare),
    )

    assert pushes == [result.branch_name]
    assert result.pushed is False
    assert result.remote_sha == remote_ref_sha(str(bare), result.branch_name)


# --- Recovery takes no model --------------------------------------------------


def test_recovery_takes_no_model() -> None:
    """§17, ADR 0017: recorded-outcome recovery cannot take a model either."""
    from coding_agent.worker import recovery  # noqa: PLC0415

    for name in (
        "recover_unfinished_attempts",
        "recover_open_attempt",
        "recover_recorded_outcome",
        "validate_recovery_configs",
        "pinned_recovery_config",
    ):
        parameters = inspect.signature(getattr(recovery, name)).parameters
        assert "model" not in parameters
        assert "chat_model" not in parameters
        assert "provider" not in parameters


def test_recovery_leaves_a_scripted_model_untouched(ledger: RunLedger) -> None:
    """A scripted model records no invocation across a recorded-outcome pass."""
    _record_failed_outcome(ledger, reason="failed-limit: wall_clock")
    tracker = _tracker()
    tracker.candidates[ISSUE_NUMBER] = _candidate(
        assignees=(IDENTITY.login,), labels=(SELECTION_LABEL,)
    )
    model = ScriptedModel([])

    summary = recover_unfinished_attempts(
        tracker, ledger, agent_login=IDENTITY.login
    )

    assert summary.settled == (ATTEMPT_ID,)
    assert model.invocations == []
    assert model.bound_tool_names == []
