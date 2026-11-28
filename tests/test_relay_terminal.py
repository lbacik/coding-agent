"""Relay, sender side: the terminal comment, record and end labels (#716, §19, §15).

A ``triage`` Attempt that ends ``completed`` with ``relay: implement`` posts
one terminal comment carrying the Relay record, applies the receiver's Relay
Label together with the configured end labels in the existing end-labels
write, and keeps the four stable terminal writes. Recovery re-applies the
label of the recorded comment unchanged. The receiver removes its own Relay
Label when its Attempt ends.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.inputs.port import TrackedInput
from coding_agent.ledger.store import AttemptPin, RunLedger
from coding_agent.runtime.relay import parse_relay_block
from coding_agent.runtime.step_finalisation import (
    FinalisationTarget,
    WorkflowEnded,
    finish_attempt,
    terminal_writes_for_outcome,
)
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

NUMBER = 7
SENDER_ATTEMPT = "triage#7/1"
RECEIVER_ATTEMPT = "implement#7/1"


def _workflow(
    workflow_id: str,
    label: str,
    *,
    relay_label: str | None = None,
    relays: dict[str, str] | None = None,
    outcomes: dict[str, tuple[str, ...]] | None = None,
) -> Workflow:
    return Workflow(
        id=workflow_id,
        model="sonnet",
        input=InputDecl(type="issue", label=label, relay_label=relay_label),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        outcomes=outcomes or {},
        relays=relays or {},
        start="work",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": RouteTarget(end="completed")},
            ),
        },
    )


TRIAGE = _workflow(
    "triage",
    "agent:triage",
    relays={"completed": "implement"},
    outcomes={"completed": ("triage-done",)},
)
IMPLEMENT = _workflow("implement", "ready-for-agent", relay_label="triaged")
ENDED = WorkflowEnded(
    category="completed",
    reason="ended at end completed after result done of step work",
    ending_step="work",
    step_result="done",
    workflow_end="completed",
)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _issue(*labels: str) -> TrackedInput:
    return TrackedInput(
        number=NUMBER,
        input_type="issue",
        title="t",
        body="b",
        is_open=True,
        labels=labels,
        assignees=("coding-agent",),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
        merged=False,
    )


def _open(
    ledger: RunLedger,
    attempt: str,
    workflow: Workflow,
    others: tuple[Workflow, ...],
    *,
    actor: str | None = "alice",
) -> None:
    ledger.open_attempt(
        attempt,
        effective_configuration=full_effective_configuration(
            workflow, other_workflows=others
        ),
    )
    ledger.record_attempt_pin(
        AttemptPin(
            attempt_id=attempt,
            workflow_id=workflow.id,
            input_type="issue",
            input_number=NUMBER,
            title="t",
            body="b",
            fingerprint="f",
            base_revision="a" * 40,
            selection_actor=actor,
            selection_actor_id=42 if actor else None,
            selection_actor_type="User" if actor else None,
            selection_actor_at="2026-11-28T10:00:00Z" if actor else None,
            identity_mode="dedicated",
        )
    )


def _finish(
    tracker: FakeStepTracker,
    ledger: RunLedger,
    attempt: str,
    workflow: Workflow,
) -> None:
    finish_attempt(
        inputs=tracker,
        code_host=tracker,
        ledger=ledger,
        attempt_id=attempt,
        target=FinalisationTarget(
            number=NUMBER, input_type="issue", selection_label=workflow.input.label
        ),
        decision=ENDED,
        end_labels_for={},
    )


def _terminal_body(tracker: FakeStepTracker) -> str:
    bodies = [c.body for c in tracker.comments if "kind=terminal" in c.body]
    assert len(bodies) == 1
    return bodies[0]


def test_relay_adds_receiver_label_with_end_labels_and_records_authorisation(
    ledger: RunLedger,
) -> None:
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue("agent:triage")
    _open(ledger, SENDER_ATTEMPT, TRIAGE, (IMPLEMENT,))

    _finish(tracker, ledger, SENDER_ATTEMPT, TRIAGE)

    labels = tracker.inputs[NUMBER].labels
    assert "triaged" in labels and "triage-done" in labels
    assert "agent:triage" not in labels
    record = parse_relay_block(_terminal_body(tracker))
    assert record is not None
    assert (record.source_attempt, record.source_workflow) == (SENDER_ATTEMPT, "triage")
    assert (record.target_workflow, record.issue, record.outcome) == (
        "implement", NUMBER, "completed",
    )
    assert record.relay_label == "triaged"
    assert (record.origin_actor, record.origin_actor_id) == ("alice", 42)
    assert record.origin_actor_at == "2026-11-28T10:00:00Z"
    # The Worker never applies a Selection Label, and the write list is unchanged.
    assert "ready-for-agent" not in labels
    assert len(terminal_writes_for_outcome()) == 4
    stored = ledger.get_outcome(SENDER_ATTEMPT)
    assert stored is not None and list(stored.writes) == list(terminal_writes_for_outcome())


def test_outcome_without_a_declared_relay_applies_no_relay_label(
    ledger: RunLedger,
) -> None:
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue("agent:triage")
    _open(ledger, SENDER_ATTEMPT, TRIAGE, (IMPLEMENT,))

    finish_attempt(
        inputs=tracker,
        code_host=tracker,
        ledger=ledger,
        attempt_id=SENDER_ATTEMPT,
        target=FinalisationTarget(
            number=NUMBER, input_type="issue", selection_label="agent:triage"
        ),
        decision=WorkflowEnded(
            category="failed",
            reason="ended at end failed",
            ending_step="work",
            step_result="error",
            workflow_end="failed",
        ),
        end_labels_for={},
    )

    assert "triaged" not in tracker.inputs[NUMBER].labels
    assert parse_relay_block(_terminal_body(tracker)) is None


def test_relay_is_not_recorded_without_a_carried_authorisation(
    ledger: RunLedger,
) -> None:
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue("agent:triage")
    _open(ledger, SENDER_ATTEMPT, TRIAGE, (IMPLEMENT,), actor=None)

    _finish(tracker, ledger, SENDER_ATTEMPT, TRIAGE)

    assert "triaged" not in tracker.inputs[NUMBER].labels
    body = _terminal_body(tracker)
    assert parse_relay_block(body) is None
    assert "no relay (missing-authorisation)" in body


def test_recovery_after_the_terminal_comment_finishes_the_relay(
    ledger: RunLedger,
) -> None:
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue("agent:triage")
    _open(ledger, SENDER_ATTEMPT, TRIAGE, (IMPLEMENT,))
    tracker.fail_on = "add_labels"
    with pytest.raises(RuntimeError):
        _finish(tracker, ledger, SENDER_ATTEMPT, TRIAGE)
    assert "triaged" not in tracker.inputs[NUMBER].labels
    comment_before = _terminal_body(tracker)

    tracker.fail_on = None
    _finish(tracker, ledger, SENDER_ATTEMPT, TRIAGE)

    assert "triaged" in tracker.inputs[NUMBER].labels
    assert _terminal_body(tracker) == comment_before
    assert parse_relay_block(comment_before) is not None


def test_receiver_removes_its_relay_label_when_its_attempt_ends(
    ledger: RunLedger,
) -> None:
    tracker = FakeStepTracker()
    tracker.inputs[NUMBER] = _issue("triaged", "ready-for-agent")
    _open(ledger, RECEIVER_ATTEMPT, IMPLEMENT, ())

    _finish(tracker, ledger, RECEIVER_ATTEMPT, IMPLEMENT)

    assert tracker.inputs[NUMBER].labels == ()


@pytest.mark.parametrize("identity", ["dedicated", "shared"])
def test_triage_completion_is_claimed_by_implement_with_no_human_action(
    ledger: RunLedger, identity: str
) -> None:
    """End to end (#716 acceptance): sender terminal write, receiver Claim."""
    from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim, sweep_and_claim
    from coding_agent.inputs.relay import RelayConfig

    tracker = FakeStepTracker()
    if identity == "shared":
        tracker.owner_id = tracker.agent_id  # type: ignore[assignment]
    tracker.inputs[NUMBER] = _issue("agent:triage")
    _open(ledger, SENDER_ATTEMPT, TRIAGE, (IMPLEMENT,))
    _finish(tracker, ledger, SENDER_ATTEMPT, TRIAGE)
    # The sender's Worker releases the assignment as its last terminal write.
    assert tracker.inputs[NUMBER].assignees == ()

    receiver_record = full_effective_configuration(IMPLEMENT, other_workflows=(TRIAGE,))
    claimed = sweep_and_claim(
        tracker,
        tracker,
        ledger,
        ClaimConfig(
            skip_open_agent_pr=False,
            workflow_id="implement",
            input_type="issue",
            selection_label="ready-for-agent",
            base_branch="main",
            agent_login=tracker.agent_login,
            agent_id=tracker.agent_id,
            effective_configuration=receiver_record,
        ),
        relay=RelayConfig(
            workflow_id="implement",
            relay_label="triaged",
            agent_login=tracker.agent_login,
            agent_id=tracker.agent_id,
            base_branch="main",
            effective_configuration=receiver_record,
        ),
    )

    assert isinstance(claimed, ConfirmedClaim)
    assert "ready-for-agent" not in tracker.inputs[NUMBER].labels
    pin = ledger.get_attempt_pin(claimed.attempt_id)
    assert pin is not None
    assert pin.relay_source_attempt == SENDER_ATTEMPT
    assert (pin.selection_actor, pin.selection_actor_id) == ("alice", 42)
