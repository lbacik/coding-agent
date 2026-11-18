"""The single Attempt-finalisation entry point: one decision, one record.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §15 (the
Terminal Outcome set; the finalisation sequence: settle in-flight effects,
record the Terminal Outcome with the rendered terminal comment and the
terminal-write list once and immutably, then perform the terminal writes;
then the Attempt is marked ``finished``), §14.2 (settlement on restart) and
§16 (recovery finishes a recorded outcome and never re-decides it).

Every outcome decision kind is driven through
:func:`finish_attempt` with a ``FakeStepTracker`` and a tmp Run Ledger: the
recorded row (outcome, reason, comment, writes) and the tracker writes are
asserted, as are the record-before-publish ordering, the crash-window replay,
the recorded-Workflow-End restart rule, the unsettled-intent refusal, the
Credential Failure halt, and end labels following the recorded outcome.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import InputComment, InputType, TrackedInput
from coding_agent.ledger.store import RunLedger, step_effect_marker
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_DETAIL,
    PIN_VIOLATION_REASON,
)
from coding_agent.runtime.step_finalisation import (
    AttemptDecision,
    CredentialFailed,
    FinalisationTarget,
    GateStopped,
    InternalFailure,
    Interrupted,
    PinViolated,
    UnsettledEffectsError,
    WorkflowEnded,
    finish_attempt,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.step_recovery import recover_attempt
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker

ATTEMPT = "implement#7/1"
NUMBER = 7
LABEL = "agent:implement"

END_LABELS: dict[str, Sequence[str]] = {
    "completed": ("done-label",),
    "failed": ("needs-human",),
    "blocked": (),
    "unfinished": (),
    "withdrawn": (),
    "drift": (),
    "interrupted": ("ops-label",),
    "internal-failure": ("ops-label",),
}
"""The outcome → end-labels mapping callers pass from the pinned config."""


def _workflow(workflow_id: str = "implement") -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type="issue", label=LABEL),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps={
            "work": V2Step(
                id="work",
                agent=V2Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": V2RouteTarget(end="completed")},
            ),
        },
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _issue(
    *,
    number: int = NUMBER,
    labels: tuple[str, ...] = (LABEL,),
    assignees: tuple[str, ...] = ("coding-agent",),
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=True,
        labels=labels,
        assignees=assignees,
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
        merged=False,
    )


def _open_attempt(ledger: RunLedger, attempt: str = ATTEMPT) -> None:
    if not ledger.has_attempt(attempt):
        # Restart recovery validates the pinned archive, so Attempts that go
        # through ``recover_attempt`` pin the full Effective Configuration.
        ledger.open_attempt(
            attempt,
            effective_configuration=full_effective_configuration(_workflow()),
        )


def _target() -> FinalisationTarget:
    return FinalisationTarget(
        number=NUMBER, input_type="issue", selection_label=LABEL
    )


def _terminal_comments(tracker: FakeStepTracker) -> list[str]:
    return [
        comment.body
        for comment in tracker.comments
        if "kind=terminal" in comment.body
    ]


def test_each_decision_records_once_with_terminal_writes(
    ledger: RunLedger,
) -> None:
    """Each decision kind records its outcome, reason, comment and writes."""
    cases: list[tuple[AttemptDecision, str, str]] = [
        (
            WorkflowEnded(
                category="completed",
                reason="ended at end completed after result done of step work",
                ending_step="work",
                step_result="done",
                workflow_end="completed",
            ),
            "completed",
            "ended at end completed after result done of step work",
        ),
        (
            WorkflowEnded(
                category="failed",
                reason="ended at end failed after result error of step work",
                ending_step="work",
                step_result="error",
                workflow_end="failed",
            ),
            "failed",
            "ended at end failed after result error of step work",
        ),
        (
            GateStopped(outcome="withdrawn", reason="withdrawn: closed"),
            "withdrawn",
            "withdrawn: closed",
        ),
        (
            GateStopped(outcome="drift", reason="drift: head-changed"),
            "drift",
            "drift: head-changed",
        ),
        (PinViolated(step="work"), "interrupted", PIN_VIOLATION_REASON),
        (
            InternalFailure(reason="internal-failure: boom"),
            "internal-failure",
            "internal-failure: boom",
        ),
        (
            Interrupted(reason="process-loss"),
            "interrupted",
            "process-loss",
        ),
        (
            Interrupted(reason="shutdown"),
            "interrupted",
            "shutdown",
        ),
    ]
    for index, (decision, outcome, reason) in enumerate(cases):
        attempt = f"implement#7/{index + 1}"
        tracker = FakeStepTracker()
        tracker.inputs[NUMBER] = _issue()
        _open_attempt(ledger, attempt)
        finalised = finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt,
            target=_target(),
            decision=decision,
            end_labels_for=END_LABELS,
        )
        assert (finalised.outcome, finalised.reason) == (outcome, reason)
        assert finalised.finished is True
        stored = ledger.get_outcome(attempt)
        assert stored is not None
        assert (stored.outcome, stored.reason) == (outcome, reason)
        assert list(stored.writes) == list(terminal_writes_for_outcome())
        assert f"coding-agent {attempt}: {outcome}" in stored.terminal_comment_body
        assert "kind=terminal" in stored.terminal_comment_body
        assert ledger.get_attempt(attempt).state == "finished"
        # The tracker writes: one terminal comment, the Selection Label
        # removed, the end labels applied, the assignment released.
        assert len(_terminal_comments(tracker)) == 1
        assert LABEL not in tracker.inputs[NUMBER].labels
        assert tracker.inputs[NUMBER].assignees == ()
        assert "remove_label" in tracker.call_names()
        assert "unassign_input" in tracker.call_names()


def test_pin_violation_comment_carries_the_detail(ledger: RunLedger) -> None:
    """The pin-violation decision renders the pin-violation detail."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=PinViolated(step="work"),
        end_labels_for=END_LABELS,
    )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert PIN_VIOLATION_DETAIL in stored.terminal_comment_body


def test_interrupted_decision_closes_open_executions(ledger: RunLedger) -> None:
    """Open Step Executions close as interrupted under the close reason."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    ledger.open_step_execution(ATTEMPT, "work", 1, entry_result="start")
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=Interrupted(reason="shutdown"),
        end_labels_for=END_LABELS,
    )
    executions = ledger.list_step_executions(ATTEMPT)
    assert len(executions) == 1
    assert executions[0].state == "interrupted"


class _OrderingTracker(FakeStepTracker):
    """A tracker that proves the record predates the first tracker write."""

    def __init__(
        self, ledger: RunLedger, attempt_id: str, *args: object, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._ledger = ledger
        self._attempt_id = attempt_id
        self.checked = 0

    def _check(self) -> None:
        self.checked += 1
        assert self._ledger.get_outcome(self._attempt_id) is not None

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        self._check()
        return super().post_comment(number, input_type, body)

    def remove_label(self, number: int, input_type: InputType, label: str) -> None:
        self._check()
        return super().remove_label(number, input_type, label)

    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None:
        self._check()
        return super().add_labels(number, input_type, labels)

    def unassign_input(self, number: int, input_type: InputType) -> None:
        self._check()
        return super().unassign_input(number, input_type)


def test_recorded_outcome_predates_first_tracker_write(
    ledger: RunLedger,
) -> None:
    """The Terminal Outcome row exists before the first tracker write."""
    tracker = _OrderingTracker(ledger, ATTEMPT)
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=WorkflowEnded(
            category="completed",
            reason="ended at end completed after result done of step work",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
        ),
        end_labels_for=END_LABELS,
    )
    # Comment, Selection Label removal, end labels, assignment release.
    assert tracker.checked == 4


class _CrashTracker(FakeStepTracker):
    """A tracker that crashes on the first terminal comment post."""

    crashing: bool = True

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        self._record("post_comment", number, input_type, body)
        if self.crashing:
            raise RuntimeError("crash between the record and the publish")
        return super().post_comment(number, input_type, body)


def test_crash_between_record_and_publish_replays_same_bytes(
    ledger: RunLedger,
) -> None:
    """A crash after the record is finished by recovery with the same bytes."""
    tracker = _CrashTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    ledger.set_attempt_state(ATTEMPT, "claim-confirmed")
    with pytest.raises(RuntimeError, match="crash between the record"):
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=WorkflowEnded(
                category="completed",
                reason="ended at end completed after result done of step work",
                ending_step="work",
                step_result="done",
                workflow_end="completed",
            ),
            end_labels_for=END_LABELS,
        )
    # The record is durable; nothing was published and nothing is finished.
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert (stored.outcome, stored.reason) == (
        "completed",
        "ended at end completed after result done of step work",
    )
    recorded_comment = stored.terminal_comment_body
    assert "kind=terminal" in recorded_comment
    assert list(stored.writes) == list(terminal_writes_for_outcome())
    assert _terminal_comments(tracker) == []
    assert ledger.get_attempt(ATTEMPT).state == "outcome-recorded"
    # Recovery finishes the recorded row — never re-decides it — with the
    # same bytes and no second terminal comment.
    tracker.crashing = False
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels_for=END_LABELS,
        decision=Interrupted(reason="process-loss"),
    )
    assert recovered.finished is True
    assert (recovered.outcome, recovered.reason) == (
        "completed",
        "ended at end completed after result done of step work",
    )
    replayed = ledger.get_outcome(ATTEMPT)
    assert replayed is not None
    assert replayed.terminal_comment_body == recorded_comment
    assert _terminal_comments(tracker) == [recorded_comment]
    assert "done-label" in tracker.inputs[NUMBER].labels
    assert ledger.get_attempt(ATTEMPT).state == "finished"


def test_recorded_workflow_end_is_recovered_with_its_category(
    ledger: RunLedger,
) -> None:
    """A Workflow End without an outcome ends with that category at restart.

    Even where the restart Gate sees a withdrawal: the Attempt already
    reached its end, so the end category stands (ADR 0028).
    """
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    ledger.set_attempt_state(ATTEMPT, "claim-confirmed")
    ledger.record_workflow_end(
        ATTEMPT, from_step="work", result="done", end="completed"
    )
    assert ledger.get_outcome(ATTEMPT) is None
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels_for=END_LABELS,
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )
    assert recovered.finished is True
    assert recovered.outcome == "completed"
    assert (
        recovered.reason == "ended at end completed after result done of step work"
    )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert stored.outcome == "completed"
    assert "done-label" in tracker.inputs[NUMBER].labels
    assert len(_terminal_comments(tracker)) == 1


def test_refuses_an_attempt_with_an_unsettled_effect_intent(
    ledger: RunLedger,
) -> None:
    """Settling stays the caller's job: an unsettled intent refuses."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    marker = step_effect_marker(
        ATTEMPT, "handoff-comment", 1, 0, kind="post-comment"
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "handoff-comment",
        1,
        0,
        rendered_payload=f"post-comment {ATTEMPT}\nmarker {marker}",
        expected_target={
            "operation": "post-comment",
            "input_number": NUMBER,
            "input_type": "issue",
        },
    )
    with pytest.raises(UnsettledEffectsError):
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=Interrupted(reason="process-loss"),
            end_labels_for=END_LABELS,
        )
    assert ledger.get_outcome(ATTEMPT) is None
    # The Credential Failure records without settling: recovery finishes the
    # terminal writes after the credential is fixed (REC-17).
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=CredentialFailed(),
        end_labels_for=END_LABELS,
    )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "credential-failure")


def test_credential_failure_records_without_terminal_writes(
    ledger: RunLedger,
) -> None:
    """A live Credential Failure records and halts, unfinished (REC-17)."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    ledger.set_attempt_state(ATTEMPT, "claim-confirmed")
    finalised = finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=CredentialFailed(),
        end_labels_for=END_LABELS,
    )
    assert (finalised.outcome, finalised.reason) == (
        "interrupted",
        "credential-failure",
    )
    assert finalised.finished is False
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "credential-failure")
    assert list(stored.writes) == list(terminal_writes_for_outcome())
    assert tracker.comments == []
    assert tracker.inputs[NUMBER].labels == (LABEL,)
    assert ledger.get_attempt(ATTEMPT).state == "outcome-recorded"
    # Recovery finishes the recorded row with its terminal writes.
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label=LABEL,
        end_labels_for=END_LABELS,
    )
    assert recovered.finished is True
    assert (recovered.outcome, recovered.reason) == (
        "interrupted",
        "credential-failure",
    )
    assert len(_terminal_comments(tracker)) == 1


def test_end_labels_follow_the_recorded_outcome(ledger: RunLedger) -> None:
    """End labels follow the replayed outcome, not the caller's decision."""
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    _open_attempt(ledger)
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=WorkflowEnded(
            category="failed",
            reason="ended at end failed after result error of step work",
            ending_step="work",
            step_result="error",
            workflow_end="failed",
        ),
        end_labels_for=END_LABELS,
    )
    assert "needs-human" in tracker.inputs[NUMBER].labels
    # A later call with a different decision replays the recorded row: the
    # recorded outcome's end labels hold, not the caller's.
    second = FakeStepTracker()
    second.inputs[NUMBER] = _issue(labels=("needs-human",))
    finalised = finish_attempt(
        tracker=second,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=WorkflowEnded(
            category="completed",
            reason="ended at end completed after result done of step work",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
        ),
        end_labels_for=END_LABELS,
    )
    assert (finalised.outcome, finalised.reason) == (
        "failed",
        "ended at end failed after result error of step work",
    )
    assert "needs-human" in second.inputs[NUMBER].labels
    assert "done-label" not in second.inputs[NUMBER].labels
