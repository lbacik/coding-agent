"""Step-workflow finalisation and recovery: terminal writes and restart settlement.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §12 (SIGTERM
and runtime-only endings), §14.2 (settlement on restart), §15 (Terminal
Outcomes and finalisation) and §16 (recovery).

Matrix row ids: REC-5 … REC-10, REC-12 … REC-21, REC-24 … REC-26 (REC-23 is
retired).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.github.errors import CredentialFailure
from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import InputComment, InputType, TrackedInput
from coding_agent.ledger.store import RunLedger, step_effect_marker
from coding_agent.runtime.step_finalisation import (
    count_uncommitted_files,
    filter_end_labels,
    perform_terminal_writes,
    record_terminal_outcome,
    render_terminal_comment,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.step_recovery import (
    MAX_CONSECUTIVE_RECOVERY_FAILURES,
    RecoveryFailureTracker,
    decide_restart_outcome,
    finalise_shutdown,
    recover_attempt,
    recover_unconfirmed_attempt,
    settle_open_step_effects,
    settle_step_effect,
    should_halt_after_failures,
)
from support.fake_step_tracker import FakeStepTracker

ATTEMPT = "implement#7/1"
NUMBER = 7
LABEL = "agent:implement"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _issue(
    *,
    number: int = NUMBER,
    labels: tuple[str, ...] = (LABEL,),
    assignees: tuple[str, ...] = (),
    is_open: bool = True,
    merged: bool = False,
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=is_open,
        labels=labels,
        assignees=assignees,
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
        merged=merged,
    )


def _open_attempt(ledger: RunLedger, attempt: str = ATTEMPT) -> None:
    if not ledger.has_attempt(attempt):
        ledger.open_attempt(attempt, effective_configuration={"workflow": "implement"})


def _comment_intent(
    ledger: RunLedger,
    attempt: str,
    step: str,
    seq: int,
    body: str,
    *,
    operation: str = "post-comment",
) -> None:
    ledger.record_step_effect_intent(
        attempt,
        step,
        seq,
        0,
        rendered_payload=f"{operation} {attempt}\n{body}",
        expected_target={
            "operation": operation,
            "input_number": NUMBER,
            "input_type": "issue",
        },
    )


# --- REC-13: write-once outcome -------------------------------------------------


def test_terminal_outcome_recorded_once_immutable(ledger: RunLedger) -> None:
    """REC-13: the first record wins; a second record is refused."""
    _open_attempt(ledger)
    first = record_terminal_outcome(
        ledger,
        ATTEMPT,
        outcome="completed",
        reason="ended at end completed after result done of step process",
        terminal_comment_body="first",
        writes=terminal_writes_for_outcome(),
    )
    second = record_terminal_outcome(
        ledger,
        ATTEMPT,
        outcome="failed",
        reason="a later decision",
        terminal_comment_body="second",
        writes=terminal_writes_for_outcome(),
    )
    assert second.outcome == "completed"
    assert second.terminal_comment_body == "first"
    assert ledger.get_outcome(ATTEMPT) == first


# --- REC-14: terminal writes ----------------------------------------------------


def test_terminal_writes_comment_labels_assignment(
    ledger: RunLedger,
) -> None:
    """REC-14: one terminal comment, label removed, end labels, unassign."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    comment = render_terminal_comment(
        attempt_id=ATTEMPT,
        outcome="completed",
        ending_step="process",
        step_result="done",
        workflow_end="completed",
        uncommitted_count=0,
    )
    assert f"coding-agent {ATTEMPT}: completed" in comment
    assert f"Attempt: {ATTEMPT}" in comment
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=("done-label",),
        comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    assert len(tracker.comments) == 1
    assert comment in tracker.comments[0].body
    assert LABEL not in tracker.inputs[NUMBER].labels
    assert "done-label" in tracker.inputs[NUMBER].labels
    assert tracker.inputs[NUMBER].assignees == ()
    assert "remove_label" in tracker.call_names()
    intents = ledger.list_step_effects(ATTEMPT)
    assert {effect.ordinal for effect in intents} == {0, 1, 2, 3}


def test_terminal_comment_posted_on_closed_input(
    ledger: RunLedger,
) -> None:
    """REC-14: the terminal comment posts even on a closed input."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(is_open=False, assignees=("coding-agent",))
    _open_attempt(ledger)
    comment = render_terminal_comment(attempt_id=ATTEMPT, outcome="withdrawn")
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    assert len(tracker.comments) == 1


def test_assignment_kept_while_another_claim_in_force(
    ledger: RunLedger,
) -> None:
    """REC-14: the assignment stays while another Claim is in force."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(
        labels=(LABEL,), assignees=("coding-agent",)
    )
    tracker.seed_comments(["other worker claim kind=claim marker"])
    _open_attempt(ledger)
    _open_attempt(ledger, "implement#7/2")
    comment = render_terminal_comment(attempt_id=ATTEMPT, outcome="completed")
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    assert tracker.inputs[NUMBER].assignees == ("coding-agent",)


# --- REC-26: runtime guard ------------------------------------------------------


def test_end_labels_never_carry_selection_label() -> None:
    """REC-26: the runtime guard drops the Workflow's own Selection Label."""
    assert filter_end_labels((LABEL, "ship-it"), LABEL) == ("ship-it",)
    assert filter_end_labels((LABEL,), LABEL) == ()


def test_perform_never_writes_selection_label(ledger: RunLedger) -> None:
    """REC-26: a bypassed config still never writes the Selection Label."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    comment = render_terminal_comment(attempt_id=ATTEMPT, outcome="completed")
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(LABEL, "ship-it"),
        comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    assert LABEL not in tracker.inputs[NUMBER].labels
    assert "ship-it" in tracker.inputs[NUMBER].labels


# --- REC-5/REC-6/REC-7: settlement by key lookup --------------------------------


def test_restart_finds_landed_write_by_key(ledger: RunLedger) -> None:
    """REC-5: the landed write found remotely records the receipt, once."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(ATTEMPT, "handoff-comment", 1, 0, kind="post-comment")
    body = f"a landed comment\n{marker}"
    tracker.post_comment(NUMBER, "issue", body)
    _comment_intent(ledger, ATTEMPT, "handoff-comment", 1, body)
    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
    )
    assert settled == {"receipt": 1, "performed": 0, "not-performed": 0}
    assert len(tracker.comments) == 1
    effect = ledger.get_step_effect(ATTEMPT, "handoff-comment", 1, 0)
    assert effect is not None and effect.status == "receipt"


def test_restart_performs_absent_write_once(ledger: RunLedger) -> None:
    """REC-6: the absent write is performed once where preconditions hold."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(ATTEMPT, "handoff-comment", 1, 0, kind="post-comment")
    body = f"a fresh comment\n{marker}"
    _comment_intent(ledger, ATTEMPT, "handoff-comment", 1, body)
    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        gate=lambda: GateResult(status="ok"),
    )
    assert settled == {"receipt": 0, "performed": 1, "not-performed": 0}
    assert len(tracker.comments) == 1
    assert marker in tracker.comments[0].body


def test_restart_records_not_performed_where_gate_fails(
    ledger: RunLedger,
) -> None:
    """REC-6: a withdrawn input records ``not-performed`` instead of writing."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(ATTEMPT, "handoff-comment", 1, 0, kind="post-comment")
    _comment_intent(ledger, ATTEMPT, "handoff-comment", 1, f"body\n{marker}")
    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )
    assert settled == {"receipt": 0, "performed": 0, "not-performed": 1}
    assert tracker.comments == []
    effect = ledger.get_step_effect(ATTEMPT, "handoff-comment", 1, 0)
    assert effect is not None and effect.status == "not-performed"


def test_no_intent_means_no_write_at_restart(ledger: RunLedger) -> None:
    """REC-7: a crash before the intent attempts no write at restart."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
    )
    assert settled == {"receipt": 0, "performed": 0, "not-performed": 0}
    assert tracker.comments == []


def test_receipt_recovered_after_write_before_receipt(
    ledger: RunLedger,
) -> None:
    """REC-7: a crash after the write but before its receipt recovers it."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(ATTEMPT, "handoff-comment", 1, 0, kind="post-comment")
    body = f"written but unreceipted\n{marker}"
    tracker.post_comment(NUMBER, "issue", body)
    _comment_intent(ledger, ATTEMPT, "handoff-comment", 1, body)
    intent = ledger.get_step_effect(ATTEMPT, "handoff-comment", 1, 0)
    assert intent is not None and intent.status == "intent"
    assert (
        settle_step_effect(
            tracker=tracker,
            ledger=ledger,
            intent=intent,
            number=NUMBER,
            input_type="issue",
        )
        == "receipt"
    )
    assert len(tracker.comments) == 1


def test_recovery_publishes_recorded_truncated_body_unchanged(
    ledger: RunLedger,
) -> None:
    """OP-19 (S8 half): recovery republishes the recorded truncated body."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(ATTEMPT, "handoff-comment", 1, 0, kind="post-comment")
    truncated = "x" * 100 + "\n[truncated]\n" + marker
    _comment_intent(ledger, ATTEMPT, "handoff-comment", 1, truncated)
    assert (
        settle_open_step_effects(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            number=NUMBER,
            input_type="issue",
            gate=lambda: GateResult(status="ok"),
        )["performed"]
        == 1
    )
    assert tracker.comments[0].body == truncated


# --- REC-8: human-closed pull request -------------------------------------------


def test_human_closed_pull_request_never_recreated(ledger: RunLedger) -> None:
    """REC-8: a human-closed own pull request is never re-created."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(
        ATTEMPT, "open-pr", 1, 0, kind="open-pull-request"
    )
    body = f"implementing\n{marker}"
    opened = tracker.open_pull_request(
        title="t", head_branch="agent/branch", base_branch="main", body=body
    )
    tracker.pull_requests[0] = tracker.pull_requests[0].__class__(
        **{**opened.__dict__, "state": "closed"},
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "open-pr",
        1,
        0,
        rendered_payload=f"open-pull-request {ATTEMPT}\n{body}",
        expected_target={
            "operation": "open-pull-request",
            "head_branch": "agent/branch",
            "base_branch": "main",
            "title": "t",
            "draft": False,
        },
    )
    calls_before = list(tracker.call_names())
    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        gate=lambda: GateResult(status="ok"),
    )
    assert settled["receipt"] == 1
    assert "open_pull_request" not in [
        name for name in tracker.call_names()[len(calls_before):]
    ]
    assert len(tracker.pull_requests) == 1


# --- REC-9: drift keeps the pull request -----------------------------------------


def test_drift_keeps_pull_request_and_lists_it(ledger: RunLedger) -> None:
    """REC-9: Drift after a pull request keeps it and lists it."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(
        ATTEMPT, "open-pr", 1, 0, kind="open-pull-request"
    )
    body = f"implementing\n{marker}"
    opened = tracker.open_pull_request(
        title="t", head_branch="agent/branch", base_branch="main", body=body
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "open-pr",
        1,
        0,
        rendered_payload=f"open-pull-request {ATTEMPT}\n{body}",
        expected_target={
            "operation": "open-pull-request",
            "head_branch": "agent/branch",
            "base_branch": "main",
            "title": "t",
            "draft": False,
        },
    )
    ledger.record_step_effect_receipt(ATTEMPT, "open-pr", 1, 0, remote_id="101")
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        gate=lambda: GateResult(status="drift", reason="head-changed"),
    )
    assert recovered.outcome == "drift"
    assert len(tracker.pull_requests) == 1
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert "open-pull-request 101" in stored.terminal_comment_body


# --- REC-10: unconfirmed claim ----------------------------------------------------


def test_restart_releases_unconfirmed_claim(
    ledger: RunLedger,
) -> None:
    """REC-10: restart deletes the own Claim Comment and releases."""
    tracker = FakeStepTracker()
    from coding_agent.ledger.store import step_effect_key as _key

    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    key_text = _key(ATTEMPT, "", 0, 1)
    tracker.post_claim_comment(NUMBER, "issue", f"stale claim {key_text}")
    ledger.open_attempt(
        ATTEMPT,
        effective_configuration={"workflow": "implement"},
        agent_identity_id=1001,
    )
    ledger.set_assigned_by_us(ATTEMPT, True)
    recovered = recover_unconfirmed_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
    )
    assert recovered.released is True
    assert recovered.outcome is None
    assert tracker.comments == []
    assert ledger.get_attempt(ATTEMPT).state == "released"


# --- REC-12/REC-19: interrupted executions ---------------------------------------


def test_restart_closes_open_execution_as_interrupted(
    ledger: RunLedger,
) -> None:
    """REC-12/REC-19: an open Step Execution closes as interrupted (trace)."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    ledger.open_step_execution(ATTEMPT, "process", 1, entry_result="start")
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        gate=lambda: GateResult(status="ok"),
    )
    assert recovered.outcome == "interrupted"
    executions = ledger.list_step_executions(ATTEMPT)
    assert executions[0].state == "interrupted"
    assert executions[0].result is None
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None and stored.outcome == "interrupted"


def test_restart_ends_withdrawn_where_input_withdrawn(
    ledger: RunLedger,
) -> None:
    """REC-12: a withdrawn input ends ``withdrawn`` at restart."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    ledger.open_step_execution(ATTEMPT, "process", 1, entry_result="start")
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )
    assert recovered.outcome == "withdrawn"
    assert ledger.list_step_executions(ATTEMPT)[0].state == "interrupted"


def test_recorded_outcome_finished_as_recorded(ledger: RunLedger) -> None:
    """Recovery finishes a recorded outcome and never re-decides it."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    comment = render_terminal_comment(attempt_id=ATTEMPT, outcome="failed")
    record_terminal_outcome(
        ledger,
        ATTEMPT,
        outcome="failed",
        reason="ended at end failed after result error of step process",
        terminal_comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=("needs-human",),
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )
    assert recovered.outcome == "failed"
    assert len(tracker.comments) == 1


# --- REC-15: nothing landed is reverted -------------------------------------------


def test_failed_attempt_reverts_nothing(ledger: RunLedger) -> None:
    """REC-15: a failed Attempt after a pull request reverts nothing."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    tracker.open_pull_request(
        title="t", head_branch="agent/branch", base_branch="main", body="b"
    )
    tracker.post_comment(NUMBER, "issue", "a review note")
    comment = render_terminal_comment(attempt_id=ATTEMPT, outcome="failed")
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    assert len(tracker.pull_requests) == 1
    assert tracker.pull_requests[0].state == "open"
    assert len(tracker.comments) == 2


# --- REC-17: credential failure ----------------------------------------------------


class _CredentialTracker(FakeStepTracker):
    failing: bool = True

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        self._record("post_comment", number, input_type, body)
        if self.failing:
            raise CredentialFailure("POST", "/repos/o/r/issues/7/comments")
        return super().post_comment(number, input_type, body)


def test_credential_failure_halts_and_recovers(ledger: RunLedger) -> None:
    """REC-17: a Credential Failure ends ``interrupted``/``credential-failure``.

    Recovery finishes the terminal writes after the credential is fixed.
    """
    tracker = _CredentialTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    marker = step_effect_marker(ATTEMPT, "handoff-comment", 1, 0, kind="post-comment")
    _comment_intent(
        ledger, ATTEMPT, "handoff-comment", 1, f"a comment\n{marker}"
    )
    with pytest.raises(CredentialFailure):
        recover_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            number=NUMBER,
            input_type="issue",
            selection_label=LABEL,
        )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert stored.outcome == "interrupted"
    assert stored.reason == "credential-failure"
    tracker.failing = False
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
    )
    assert recovered.finished is True
    assert any(
        "kind=terminal" in comment.body for comment in tracker.comments
    )


# --- REC-18: halt after three ------------------------------------------------------


def test_three_consecutive_recovery_failures_halt() -> None:
    """REC-18: three consecutive recovery write failures halt the Worker."""
    tracker = RecoveryFailureTracker()
    assert tracker.note_failure() is False
    assert tracker.note_failure() is False
    assert tracker.note_failure() is True
    assert should_halt_after_failures(3) is True
    assert should_halt_after_failures(2) is False
    assert MAX_CONSECUTIVE_RECOVERY_FAILURES == 3
    tracker.note_success()
    assert tracker.consecutive_failures == 0


# --- REC-21: dirty tree -------------------------------------------------------------


def test_dirty_tree_reported_never_pushed(tmp_path: Path) -> None:
    """REC-21: the dirty tree is never committed; the comment reports it."""
    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "a@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    assert count_uncommitted_files(repo) == 0
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    assert count_uncommitted_files(repo) == 1
    comment = render_terminal_comment(
        attempt_id=ATTEMPT, outcome="interrupted", uncommitted_count=1
    )
    assert "Uncommitted files: 1" in comment


def test_recover_never_commits_dirty_tree(ledger: RunLedger, tmp_path: Path) -> None:
    """REC-21: restart recovery performs no commit or push of the tree."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        uncommitted_count=2,
    )
    assert recovered.outcome == "interrupted"
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert "Uncommitted files: 2" in stored.terminal_comment_body


# --- REC-24/REC-25: SIGTERM ----------------------------------------------------------


def test_sigterm_finalises_interrupted_shutdown(ledger: RunLedger) -> None:
    """REC-24: SIGTERM finalises the live Attempt as ``interrupted/shutdown``."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    ledger.open_step_execution(ATTEMPT, "review", 1, entry_result="start")
    recovered = finalise_shutdown(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=(),
        gate=lambda: GateResult(status="ok"),
        ending_step="review",
    )
    assert recovered.outcome == "interrupted"
    assert recovered.reason == "shutdown"
    executions = ledger.list_step_executions(ATTEMPT)
    assert executions[0].state == "interrupted"
    assert executions[0].result is None
    assert LABEL not in tracker.inputs[NUMBER].labels
    assert len(tracker.comments) == 1


def test_sigterm_during_review_leaves_no_result(ledger: RunLedger) -> None:
    """REC-25: SIGTERM during ``review`` ends without a Step Result."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    ledger.open_step_execution(ATTEMPT, "review", 1, entry_result="start")
    recovered = finalise_shutdown(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels=("attention",),
        gate=lambda: GateResult(status="ok"),
        ending_step="review",
    )
    assert recovered.outcome == "interrupted"
    executions = ledger.list_step_executions(ATTEMPT)
    assert len(executions) == 1
    assert executions[0].result is None
    assert LABEL not in tracker.inputs[NUMBER].labels


def test_shutdown_withdrawn_where_input_withdrawn(ledger: RunLedger) -> None:
    """REC-24: a withdrawn input at SIGTERM ends ``withdrawn``."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue(assignees=("coding-agent",))
    _open_attempt(ledger)
    recovered = finalise_shutdown(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )
    assert recovered.outcome == "withdrawn"


# --- Restart outcome decisions -------------------------------------------------------


def test_decide_restart_outcome() -> None:
    """REC-12: withdrawn, drift, or interrupted/process-loss, never routed."""
    assert (
        decide_restart_outcome(GateResult(status="withdrawn", reason="closed")).outcome
        == "withdrawn"
    )
    assert (
        decide_restart_outcome(
            GateResult(status="drift", reason="head-changed")
        ).outcome
        == "drift"
    )
    decided = decide_restart_outcome(GateResult(status="ok"))
    assert (decided.outcome, decided.reason) == ("interrupted", "process-loss")
    assert decide_restart_outcome(None).outcome == "interrupted"
