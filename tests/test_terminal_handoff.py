"""The terminal settlement of a Round Handoff round (contract §8, §15, §16).

Issue #612, acceptance bullet 3. A prepared offer settles its terminal
comment, the consumed intake label and the Pickup Label before releasing
assignment: the Pickup Label replaces the configured ``unfinished`` labels
for an eligible round. An exhausted offer or a diagnostic keeps the
configured labels; an eligible end with no prepared offer records the
``missing-handoff`` diagnostic (the push landed before preparation) and
offers no pickup after restart. Replay parses the stored comment's own
record and ignores the passed handoff.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from coding_agent.inputs.port import InputComment, InputType, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    render_handoff_block,
)
from coding_agent.runtime.step_finalisation import (
    FinalisationTarget,
    WorkflowEnded,
    _replay_pickup_label,
    finish_attempt,
    pinned_handoff_eligibility,
    render_terminal_comment,
)
from coding_agent.workflow.schema import (
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    RoundHandoffAnnotation,
    RoundHandoffConfig,
    Workflow,
    WorkflowStep,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker

ATTEMPT = "implement#7/1"
NUMBER = 7
PINNED_LABEL = "agent:implement"
PICKUP_LABEL = "round-finished"
CONFIGURED_UNFINISHED = ("stale-unfinished",)
WORK = "a" * 40
BASE = "b" * 40


def _workflow(
    *,
    enable_handoff: bool = True,
    annotate: bool = True,
    pickup_label: str | None = None,
    limit: int = 2,
) -> Workflow:
    routes: dict[str, RouteTarget] = {
        "pushed": (
            RouteTarget(
                end="unfinished",
                round_handoff=RoundHandoffAnnotation(kind="hard"),
            )
            if annotate
            else RouteTarget(end="unfinished")
        ),
    }
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=PINNED_LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="publish",
        defaults_routes={
            "error": RouteTarget(end="failed"),
        },
        steps={
            "publish": WorkflowStep(
                id="publish",
                agent=None,
                operation="commit-and-push",
                max_executions=1,
                routes=routes,
            ),
        },
        outcomes={"unfinished": CONFIGURED_UNFINISHED},
        round_handoff=(
            RoundHandoffConfig(
                max_automatic_pickups=limit,
                **(
                    {"pickup_label": pickup_label}
                    if pickup_label is not None
                    else {}
                ),
            )
            if enable_handoff
            else None
        ),
    )


def _offer(**overrides: object) -> RoundHandoffOffer:
    fields: dict[str, object] = {
        "workflow_id": "implement",
        "issue": NUMBER,
        "attempt": ATTEMPT,
        "work_commit": WORK,
        "ref": "agent/implement-7-1-work",
        "base_revision": BASE,
        "note_kind": "hard",
        "note": "did the thing",
        "pickup_label": PICKUP_LABEL,
        "used": 0,
        "limit": 2,
    }
    fields.update(overrides)
    return RoundHandoffOffer(**fields)  # type: ignore[arg-type]


def _diagnostic(
    reason: str = "failed-push", detail: str = "the push never landed"
) -> HandoffDiagnostic:
    return HandoffDiagnostic(reason=reason, detail=detail)


def _decision() -> WorkflowEnded:
    return WorkflowEnded(
        category="unfinished",
        reason="ended at end unfinished after result pushed of step publish",
        ending_step="publish",
        step_result="pushed",
        workflow_end="unfinished",
    )


def _target() -> FinalisationTarget:
    return FinalisationTarget(
        number=NUMBER, input_type="issue", selection_label=PINNED_LABEL
    )


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


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _open(ledger: RunLedger, workflow: Workflow) -> None:
    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(workflow)
    )


# --- the comment section ----------------------------------------------------------


def test_handoff_section_rides_above_the_terminal_marker() -> None:
    """The record block is in the comment; the marker stays the last line."""
    comment = render_terminal_comment(
        attempt_id=ATTEMPT,
        outcome="unfinished",
        handoff_section="Round Handoff: offers pickup",
    )

    assert "Round Handoff: offers pickup" in comment
    assert "kind=terminal" in comment.rstrip().splitlines()[-1]


def test_no_handoff_section_keeps_the_old_shape() -> None:
    """Ordinary rounds render exactly the fixed template."""
    comment = render_terminal_comment(attempt_id=ATTEMPT, outcome="completed")

    assert "Round Handoff" not in comment


# --- pinned eligibility ------------------------------------------------------------


def test_eligible_end_resolves_label_and_limit(ledger: RunLedger) -> None:
    """The pinned enablement plus the taken annotation resolve the series."""
    _open(ledger, _workflow())

    assert pinned_handoff_eligibility(ledger, ATTEMPT, "publish", "pushed") == (
        PICKUP_LABEL,
        2,
    )


def test_disabled_workflow_is_ineligible(ledger: RunLedger) -> None:
    """Without enablement the round keeps its ordinary path."""
    _open(ledger, _workflow(enable_handoff=False))

    assert pinned_handoff_eligibility(ledger, ATTEMPT, "publish", "pushed") is None


def test_unannotated_end_is_ineligible(ledger: RunLedger) -> None:
    """Enablement alone publishes nothing: the taken end names the route."""
    _open(ledger, _workflow(annotate=False))

    assert pinned_handoff_eligibility(ledger, ATTEMPT, "publish", "pushed") is None


def test_other_end_is_ineligible(ledger: RunLedger) -> None:
    """Eligibility follows the taken Route, not the publishing step."""
    _open(ledger, _workflow())

    assert pinned_handoff_eligibility(ledger, ATTEMPT, "publish", "no-changes") is None
    assert pinned_handoff_eligibility(ledger, ATTEMPT, "other", "pushed") is None
    assert pinned_handoff_eligibility(ledger, ATTEMPT, "", "") is None


def test_unknown_attempt_is_ineligible(ledger: RunLedger) -> None:
    """No pinned record, no eligibility: recovery never guesses."""
    assert pinned_handoff_eligibility(ledger, "implement#9/1", "publish", "pushed") is None


# --- the recording path -------------------------------------------------------------


def test_offer_replaces_configured_end_labels(ledger: RunLedger) -> None:
    """The Pickup Label replaces the configured unfinished labels."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_offer(),
    )

    assert PINNED_LABEL not in tracker.inputs[NUMBER].labels
    assert PICKUP_LABEL in tracker.inputs[NUMBER].labels
    assert "stale-unfinished" not in tracker.inputs[NUMBER].labels
    assert tracker.inputs[NUMBER].assignees == ()
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert len(terminal) == 1
    assert "round-handoff-record v1" in terminal[0]
    assert WORK in terminal[0]


def test_exhausted_offer_keeps_configured_labels(ledger: RunLedger) -> None:
    """The last round publishes its note with ordinary labels and no pickup."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached"),
    )

    assert PICKUP_LABEL not in tracker.inputs[NUMBER].labels
    assert "stale-unfinished" in tracker.inputs[NUMBER].labels
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert len(terminal) == 1
    assert "exhausted" in terminal[0]
    assert "pickup-limit-reached" in terminal[0]


def test_diagnostic_keeps_configured_labels(ledger: RunLedger) -> None:
    """A diagnostic settles the comment with ordinary labels and no pickup."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_diagnostic(),
    )

    assert PICKUP_LABEL not in tracker.inputs[NUMBER].labels
    assert "stale-unfinished" in tracker.inputs[NUMBER].labels
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert len(terminal) == 1
    assert "no pickup" in terminal[0]


def test_missing_offer_on_eligible_end_records_missing_handoff(
    ledger: RunLedger,
) -> None:
    """A push that landed before preparation yields a diagnostic, no offer."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=None,
    )

    assert PICKUP_LABEL not in tracker.inputs[NUMBER].labels
    assert "stale-unfinished" in tracker.inputs[NUMBER].labels
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert len(terminal) == 1
    assert "missing-handoff" in terminal[0]


def test_missing_offer_on_ordinary_end_stays_ordinary(ledger: RunLedger) -> None:
    """An ineligible end with no offer renders the fixed template alone."""
    _open(ledger, _workflow(enable_handoff=False))
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=None,
    )

    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert len(terminal) == 1
    assert "Round Handoff" not in terminal[0]


def test_offer_on_completed_outcome_keeps_configured_labels(
    ledger: RunLedger,
) -> None:
    """An offer never diverts a non-unfinished outcome."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()

    finish_attempt(
        inputs=tracker, code_host=tracker,
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
        end_labels_for={"completed": ("did-it",)},
        handoff=_offer(),
    )

    assert PICKUP_LABEL not in tracker.inputs[NUMBER].labels
    assert "did-it" in tracker.inputs[NUMBER].labels


# --- the replay path -----------------------------------------------------------------


def test_replay_reapplies_the_stored_pickup_label(ledger: RunLedger) -> None:
    """Recovery replays the stored record's label even as config differs."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_offer(),
    )
    recorded = ledger.get_outcome(ATTEMPT)
    assert recorded is not None
    recorded_comment = recorded.terminal_comment_body

    comments_before = len(tracker.comments)
    finalised = finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": ("evil-label",)},
        handoff=None,
    )

    assert finalised.outcome == "unfinished"
    assert PICKUP_LABEL in tracker.inputs[NUMBER].labels
    assert "evil-label" not in tracker.inputs[NUMBER].labels
    assert "stale-unfinished" not in tracker.inputs[NUMBER].labels
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert [comment for comment in terminal] == [recorded_comment]
    assert len(tracker.comments) == comments_before


def test_replay_ignores_the_passed_handoff(ledger: RunLedger) -> None:
    """ADR 0017: a recorded outcome replays its own comment, never re-decides."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_diagnostic(),
    )
    recorded = ledger.get_outcome(ATTEMPT)
    assert recorded is not None

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_offer(),
    )

    replayed = ledger.get_outcome(ATTEMPT)
    assert replayed is not None
    assert replayed.terminal_comment_body == recorded.terminal_comment_body
    assert PICKUP_LABEL not in tracker.inputs[NUMBER].labels
    assert "stale-unfinished" in tracker.inputs[NUMBER].labels


def test_replay_of_exhausted_record_uses_configured_labels(
    ledger: RunLedger,
) -> None:
    """An exhausted stored record carries no pickup on replay."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached"),
    )

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": ("evil-label",)},
        handoff=None,
    )

    assert PICKUP_LABEL not in tracker.inputs[NUMBER].labels


def test_replay_pickup_label_reads_only_valid_offers() -> None:
    """Forged, exhausted and ordinary comments replay configured labels."""
    assert _replay_pickup_label(render_handoff_block(_offer())) == PICKUP_LABEL
    assert (
        _replay_pickup_label(
            render_handoff_block(
                _offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached")
            )
        )
        is None
    )
    assert _replay_pickup_label("coding-agent implement#7/1: unfinished\n") is None
    assert _replay_pickup_label(render_handoff_block(_offer()) + "tampered") is None


# --- ambiguous writes and interrupts --------------------------------------------------


class _CrashOnAddLabels(FakeStepTracker):
    crashing: bool = True

    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None:
        self._record("add_labels", number, input_type, labels)
        if self.crashing and PICKUP_LABEL in labels:
            raise RuntimeError("crash between comment and pickup label")
        held = self._held(number, input_type)
        current = list(held.labels)
        for name in labels:
            if name not in current:
                current.append(name)
        self.inputs[number] = replace(held, labels=tuple(current))


def test_crash_between_comment_and_pickup_replays_same_offer(
    ledger: RunLedger,
) -> None:
    """An ambiguous pickup write replays the one recorded comment and label."""
    _open(ledger, _workflow())
    tracker = _CrashOnAddLabels()
    tracker.inputs[NUMBER] = _issue()
    with pytest.raises(RuntimeError, match="crash between comment and pickup"):
        finish_attempt(
            inputs=tracker, code_host=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=_decision(),
            end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
            handoff=_offer(),
        )
    tracker.crashing = False

    finalised = finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=None,
    )

    assert finalised.outcome == "unfinished"
    recorded = ledger.get_outcome(ATTEMPT)
    assert recorded is not None
    assert "round-handoff-record v1" in recorded.terminal_comment_body
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert terminal == [recorded.terminal_comment_body]
    assert PICKUP_LABEL in tracker.inputs[NUMBER].labels


class _CrashBeforeComment(FakeStepTracker):
    crashing: bool = True

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        self._record("post_comment", number, input_type, body)
        if self.crashing and "kind=terminal" in body:
            raise RuntimeError("crash before the terminal comment")
        return super().post_comment(number, input_type, body)


def test_crash_before_comment_records_once_and_replays(ledger: RunLedger) -> None:
    """The outcome row wins once; the retry replays its comment and label."""
    _open(ledger, _workflow())
    tracker = _CrashBeforeComment()
    tracker.inputs[NUMBER] = _issue()
    with pytest.raises(RuntimeError, match="crash before the terminal comment"):
        finish_attempt(
            inputs=tracker, code_host=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=_decision(),
            end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
            handoff=_offer(),
        )
    tracker.crashing = False

    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=None,
    )

    recorded = ledger.get_outcome(ATTEMPT)
    assert recorded is not None
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert terminal == [recorded.terminal_comment_body]
    assert PICKUP_LABEL in tracker.inputs[NUMBER].labels


def test_eligible_labels_for_unfinished(
    ledger: RunLedger,
) -> None:
    """The pickup write is one of the four terminal writes, before release."""
    _open(ledger, _workflow())
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue()
    finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=_offer(),
    )

    names = tracker.call_names()
    assert names.index("post_comment") < names.index("remove_label")
    assert names.index("remove_label") < names.index("add_labels")
    assert names.index("add_labels") < names.index("unassign_input")
