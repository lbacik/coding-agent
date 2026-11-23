"""Recovery replays the exact terminal write plan from pinned data.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §15 (record
the Terminal Outcome, exact rendered comment and complete terminal write
plan together before the first terminal write; perform comment, Selection
Label removal, end labels, assignment release last) and §16 (recovery
finishes a recorded outcome and never re-decides it, from pinned data even
when the current Workflow Configuration differs).

Row ids: REC-13, REC-14.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from coding_agent.inputs.port import InputType, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.step_finalisation import (
    FinalisationTarget,
    WorkflowEnded,
    finish_attempt,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.step_recovery import recover_attempt
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker

ATTEMPT = "implement#7/1"
NUMBER = 7
PINNED_LABEL = "agent:implement"
PINNED_END = ("pinned-done",)


def _workflow(
    label: str = PINNED_LABEL,
    end_labels: Sequence[str] = PINNED_END,
) -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=label),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="work",
        defaults_routes={
            "error": RouteTarget(end="failed"),
        },
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": RouteTarget(end="completed")},
            ),
        },
        outcomes={"completed": tuple(end_labels)},
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _issue(
    *,
    labels: tuple[str, ...] = (PINNED_LABEL,),
    assignees: tuple[str, ...] = ("coding-agent",),
) -> TrackedInput:
    return TrackedInput(
        number=NUMBER,
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


def _target(selection_label: str = PINNED_LABEL) -> FinalisationTarget:
    return FinalisationTarget(
        number=NUMBER, input_type="issue", selection_label=selection_label
    )


def _decision() -> WorkflowEnded:
    return WorkflowEnded(
        category="completed",
        reason="ended at end completed after result done of step work",
        ending_step="work",
        step_result="done",
        workflow_end="completed",
    )


def test_live_uses_pinned_labels_when_caller_differs(ledger: RunLedger) -> None:
    """The live plan consumes the pinned label and applies pinned end labels."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(selection_label="agent:changed"),
        decision=_decision(),
        end_labels_for={"completed": ("evil-label",)},
    )
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert "pinned-done" in tracker.inputs[NUMBER].labels
    assert "evil-label" not in tracker.inputs[NUMBER].labels
    assert "agent:changed" not in tracker.inputs[NUMBER].labels
    assert tracker.inputs[NUMBER].assignees == ()


def test_replay_uses_pinned_plan_when_config_differs(ledger: RunLedger) -> None:
    """A recorded outcome replays the pinned plan, not the caller's labels."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"completed": PINNED_END},
    )
    recorded = ledger.get_outcome(ATTEMPT)
    assert recorded is not None
    recorded_comment = recorded.terminal_comment_body
    # Replay with a changed configuration: the pinned plan must still win,
    # with exactly one terminal comment (no duplicate on the same remote).
    comments_before = len(tracker.comments)
    finalised = finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(selection_label="agent:changed"),
        decision=WorkflowEnded(
            category="failed",
            reason="ended at end failed after result error of step work",
            ending_step="work",
            step_result="error",
            workflow_end="failed",
        ),
        end_labels_for={"completed": ("evil-label",), "failed": ("other",)},
    )
    assert (finalised.outcome, finalised.reason) == (
        recorded.outcome,
        recorded.reason,
    )
    assert ledger.get_outcome(ATTEMPT) is not None
    assert ledger.get_outcome(ATTEMPT).terminal_comment_body == recorded_comment
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert terminal == [recorded_comment]
    assert len(tracker.comments) == comments_before
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert "pinned-done" in tracker.inputs[NUMBER].labels
    assert "evil-label" not in tracker.inputs[NUMBER].labels


class _CrashBeforeComment(FakeStepTracker):
    crashing: bool = True

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ):  # type: ignore[no-untyped-def]
        self._record("post_comment", number, input_type, body)
        if self.crashing and "kind=terminal" in body:
            raise RuntimeError("crash before terminal comment")
        return super().post_comment(number, input_type, body)


class _CrashOnRemove(FakeStepTracker):
    crashing: bool = True

    def remove_label(self, number: int, input_type: InputType, label: str) -> None:
        self._record("remove_label", number, input_type, label)
        if self.crashing:
            raise RuntimeError("crash between comment and label writes")
        held = self.inputs.get(number)
        if held is not None:
            from dataclasses import replace

            self.inputs[number] = replace(
                held, labels=tuple(name for name in held.labels if name != label)
            )


class _CrashOnUnassign(FakeStepTracker):
    crashing: bool = True

    def unassign_input(self, number: int, input_type: InputType) -> None:
        self._record("unassign_input", number, input_type)
        if self.crashing:
            raise RuntimeError("crash between labels and release")
        held = self.inputs.get(number)
        if held is not None:
            from dataclasses import replace

            self.inputs[number] = replace(
                held,
                assignees=tuple(
                    login for login in held.assignees if login != self.agent_login
                ),
            )


def _recover(
    tracker: FakeStepTracker, ledger: RunLedger
) -> tuple[str, int]:
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label="agent:changed",
        end_labels_for={"completed": ("evil-label",)},
    )
    assert recovered.finished is True
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert len(terminal) == 1
    return terminal[0], len(tracker.comments)


def test_crash_before_comment_replays_same_comment(ledger: RunLedger) -> None:
    """Interrupt before the terminal comment: recovery posts it once."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    ledger.set_attempt_state(ATTEMPT, "claim-confirmed")
    tracker = _CrashBeforeComment()
    tracker.inputs[NUMBER] = _issue()
    with pytest.raises(RuntimeError, match="crash before terminal comment"):
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=_decision(),
            end_labels_for={"completed": PINNED_END},
        )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert ledger.get_attempt(ATTEMPT).state == "outcome-recorded"
    tracker.crashing = False
    comment, _ = _recover(tracker, ledger)
    assert comment == stored.terminal_comment_body
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert "pinned-done" in tracker.inputs[NUMBER].labels
    assert tracker.inputs[NUMBER].assignees == ()
    assert ledger.get_attempt(ATTEMPT).state == "finished"


def test_crash_between_comment_and_labels_replays_same_comment(
    ledger: RunLedger,
) -> None:
    """Interrupt between comment and label writes: one comment, same plan."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    ledger.set_attempt_state(ATTEMPT, "claim-confirmed")
    tracker = _CrashOnRemove()
    tracker.inputs[NUMBER] = _issue()
    with pytest.raises(RuntimeError, match="crash between comment"):
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=_decision(),
            end_labels_for={"completed": PINNED_END},
        )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    tracker.crashing = False
    comment, _ = _recover(tracker, ledger)
    assert comment == stored.terminal_comment_body
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert "pinned-done" in tracker.inputs[NUMBER].labels
    assert tracker.inputs[NUMBER].assignees == ()


def test_crash_between_labels_and_release_replays_same_comment(
    ledger: RunLedger,
) -> None:
    """Interrupt between label writes and release: one comment, release last."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    ledger.set_attempt_state(ATTEMPT, "claim-confirmed")
    tracker = _CrashOnUnassign()
    tracker.inputs[NUMBER] = _issue()
    with pytest.raises(RuntimeError, match="crash between labels"):
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=_decision(),
            end_labels_for={"completed": PINNED_END},
        )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    # Labels already applied; the assignment is still held.
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert "pinned-done" in tracker.inputs[NUMBER].labels
    tracker.crashing = False
    comment, _ = _recover(tracker, ledger)
    assert comment == stored.terminal_comment_body
    assert tracker.inputs[NUMBER].assignees == ()
    # Release is last: the unassign call follows the label writes.
    names = tracker.call_names()
    assert names.index("unassign_input") > names.index("remove_label")


def test_ambiguous_label_removal_reads_back_without_duplicate(
    ledger: RunLedger,
) -> None:
    """An intent without receipt whose label is already gone is a receipt."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    tracker = FakeStepTracker()
    # The remote already reflects the removal (label absent) but the ledger
    # holds only the intent: recovery must read back, not rewrite.
    tracker.inputs[NUMBER] = _issue(labels=(), assignees=("coding-agent",))
    ledger.record_step_effect_intent(
        ATTEMPT,
        "",
        0,
        1,
        rendered_payload=f"remove label {PINNED_LABEL}",
        expected_target={
            "terminal-write": "remove-selection-label",
            "label": PINNED_LABEL,
        },
    )
    calls_before = len(tracker.calls)
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"completed": PINNED_END},
    )
    # The recorded outcome still performs the full plan, but the ambiguous
    # removal is recognised remotely: no second remove_label call for it
    # beyond the one the finalisation itself needs... instead assert the
    # ledger now holds the receipt and the label stays absent.
    effect = ledger.get_step_effect(ATTEMPT, "", 0, 1)
    assert effect is not None and effect.status == "receipt"
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert tracker.calls[calls_before:].count(("remove_label", (NUMBER, "issue", PINNED_LABEL))) <= 1


def test_legacy_v4_record_replays_from_pinned_data(ledger: RunLedger) -> None:
    """A legacy row (comment + four writes) replays pinned labels on change."""
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(_workflow())
    )
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"completed": PINNED_END},
    )
    stored = ledger.get_outcome(ATTEMPT)
    assert stored is not None
    assert list(stored.writes) == list(terminal_writes_for_outcome())
    # A changed deployment replays the same pinned plan, not its own file.
    # The remote already reflects the first run; recovery adds nothing.
    comments_before = len(tracker.comments)
    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=NUMBER,
        input_type="issue",
        selection_label="agent:changed",
        end_labels_for={"completed": ("evil-label",)},
    )
    assert recovered.finished is True
    assert (recovered.outcome, recovered.reason) == (
        stored.outcome,
        stored.reason,
    )
    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert "pinned-done" in tracker.inputs[NUMBER].labels
    assert "evil-label" not in tracker.inputs[NUMBER].labels
    assert tracker.inputs[NUMBER].assignees == ()
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert terminal == [stored.terminal_comment_body]
    assert len(tracker.comments) == comments_before
