"""Relay intake: the Relay Label ahead of the Selection Label (#716, §22).

A relayed Target Issue is claimed before model work only where the sender's
Relay record is trusted: authored by the Agent Identity's numeric id,
carrying the canonical terminal marker of its exact source Attempt, naming
its source Workflow, the receiving Workflow and the Target Issue, being the
newest terminal comment of any Workflow, and declared by the receiver's own
pinned configuration. A label alone, a forged or superseded record or an
undeclared Relay is diagnosed with no Claim and no write.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    sweep_and_claim,
)
from coding_agent.inputs.port import InputComment, LabeledEvent, TrackedInput
from coding_agent.inputs.relay import RelayConfig, relay_exclusion_reason
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.relay import RelayRecord, render_relay_section
from coding_agent.runtime.step_finalisation import render_terminal_comment
from support.fake_step_tracker import FakeStepTracker
from support.markers import terminal_marker_text

WORKFLOW = "implement"
SELECTION_LABEL = "ready-for-agent"
RELAY_LABEL = "triaged"
SOURCE_ATTEMPT = "triage#9/1"
NUMBER = 9
TIP = "c" * 40

EFFECTIVE: Mapping[str, object] = {
    "workflow": {
        "id": WORKFLOW,
        "input": {"relay_label": RELAY_LABEL},
        "relay_sources": {"triage": ["completed"]},
    }
}
UNDECLARED: Mapping[str, object] = {
    "workflow": {"id": WORKFLOW, "input": {"relay_label": RELAY_LABEL}}
}


def _record(**overrides: object) -> RelayRecord:
    fields: dict[str, object] = {
        "source_attempt": SOURCE_ATTEMPT,
        "source_workflow": "triage",
        "target_workflow": WORKFLOW,
        "issue": NUMBER,
        "outcome": "completed",
        "relay_label": RELAY_LABEL,
        "origin_actor": "alice",
        "origin_actor_id": 42,
        "origin_actor_type": "User",
        "origin_actor_at": "2026-11-01T00:00:00Z",
        "identity_mode": "dedicated",
    }
    fields.update(overrides)
    return RelayRecord(**fields)  # type: ignore[arg-type]


def _terminal(attempt: str, record: RelayRecord | None) -> str:
    return render_terminal_comment(
        attempt_id=attempt,
        outcome="completed",
        marker_text=terminal_marker_text(attempt),
        ending_step="work",
        step_result="done",
        workflow_end="completed",
        handoff_section=render_relay_section(record) if record else "",
    )


def _post(
    tracker: FakeStepTracker,
    attempt: str,
    record: RelayRecord | None,
    *,
    author_id: int | None = None,
) -> InputComment:
    comment = InputComment(
        comment_id=tracker._next_comment_id,
        body=_terminal(attempt, record),
        author_login=tracker.agent_login if author_id is None else "mallory",
        author_id=tracker.agent_id if author_id is None else author_id,
    )
    tracker._next_comment_id += 1
    tracker.comments.append(comment)
    return comment


def _issue(*labels: str, assignees: tuple[str, ...] = ()) -> TrackedInput:
    return TrackedInput(
        number=NUMBER,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=True,
        labels=labels,
        assignees=assignees,
        created_at="2026-11-02T00:00:00Z",
        author_login="human",
        author_id=7,
    )


def _claim_config(tracker: FakeStepTracker) -> ClaimConfig:
    return ClaimConfig(
        skip_open_agent_pr=False,
        workflow_id=WORKFLOW,
        input_type="issue",
        selection_label=SELECTION_LABEL,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration={"workflow": WORKFLOW},
    )


def _relay_config(
    tracker: FakeStepTracker, effective: Mapping[str, object] = EFFECTIVE
) -> RelayConfig:
    return RelayConfig(
        workflow_id=WORKFLOW,
        relay_label=RELAY_LABEL,
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        base_branch="main",
        effective_configuration=effective,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "ledger.db")


@pytest.fixture(params=["dedicated", "shared"])
def tracker(request: pytest.FixtureRequest) -> FakeStepTracker:
    """A fake tracker under dedicated and shared Agent Identity."""
    fake = FakeStepTracker()
    fake.base_tip = TIP
    if request.param == "shared":
        fake.owner_id = fake.agent_id  # type: ignore[assignment]
    fake.inputs[NUMBER] = _issue(RELAY_LABEL)
    return fake


def _sweep(
    tracker: FakeStepTracker,
    ledger: RunLedger,
    effective: Mapping[str, object] = EFFECTIVE,
) -> tuple[ConfirmedClaim | ReleasedClaim | None, list[tuple[int, str]]]:
    diagnostics: list[tuple[int, str]] = []
    claimed = sweep_and_claim(
        tracker,
        tracker,
        ledger,
        _claim_config(tracker),
        relay=_relay_config(tracker, effective),
        relay_diagnose=lambda issue, reason: diagnostics.append((issue, reason)),
    )
    return claimed, diagnostics


def _no_writes(tracker: FakeStepTracker) -> None:
    writes = {
        "assign_input", "post_claim_comment", "post_comment", "add_labels",
        "remove_label", "unassign_input", "delete_comment",
    }
    assert not writes & set(tracker.call_names())


def test_valid_relay_is_claimed_with_no_human_action_under_either_identity(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    _post(tracker, SOURCE_ATTEMPT, _record())

    claimed, diagnostics = _sweep(tracker, ledger)

    assert isinstance(claimed, ConfirmedClaim)
    assert diagnostics == []
    assert claimed.workflow_id == WORKFLOW
    # No labeled-event actor test ran: the Relay Label's actor is the Agent Identity.
    assert not any(name == "read_labeled_event" for name in tracker.call_names())
    stored = ledger.get_attempt_pin(claimed.attempt_id)
    assert stored is not None
    assert stored.relay_source_attempt == SOURCE_ATTEMPT
    assert (stored.selection_actor, stored.selection_actor_id) == ("alice", 42)
    assert stored.selection_actor_at == "2026-11-01T00:00:00Z"


def test_label_without_a_record_authorises_nothing(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    claimed, diagnostics = _sweep(tracker, ledger)

    assert claimed is None
    assert diagnostics == [(NUMBER, "relay-replaced")]
    _no_writes(tracker)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda t: _post(t, SOURCE_ATTEMPT, _record(), author_id=555),
            id="foreign-author",
        ),
        pytest.param(
            lambda t: _post(t, SOURCE_ATTEMPT, _record(source_attempt="triage#9/9")),
            id="record-names-another-attempt",
        ),
        pytest.param(
            lambda t: _post(t, SOURCE_ATTEMPT, _record(source_workflow="other")),
            id="record-names-another-source-workflow",
        ),
        pytest.param(
            lambda t: _post(t, SOURCE_ATTEMPT, _record(target_workflow="other")),
            id="record-names-another-receiver",
        ),
        pytest.param(
            lambda t: _post(t, SOURCE_ATTEMPT, _record(issue=10)),
            id="record-names-another-issue",
        ),
        pytest.param(
            lambda t: _post(t, SOURCE_ATTEMPT, _record(relay_label="other-label")),
            id="record-names-another-relay-label",
        ),
        pytest.param(lambda t: _post(t, SOURCE_ATTEMPT, None), id="terminal-without-record"),
    ],
)
def test_forged_or_foreign_records_are_replaced_with_no_claim_and_no_write(
    tracker: FakeStepTracker,
    ledger: RunLedger,
    build: Callable[[FakeStepTracker], object],
) -> None:
    build(tracker)

    claimed, diagnostics = _sweep(tracker, ledger)

    assert claimed is None
    assert diagnostics == [(NUMBER, "relay-replaced")]
    _no_writes(tracker)


def test_a_later_terminal_comment_of_any_workflow_supersedes_the_record(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    _post(tracker, SOURCE_ATTEMPT, _record())
    _post(tracker, "other-workflow#9/1", None)

    claimed, diagnostics = _sweep(tracker, ledger)

    assert claimed is None
    assert diagnostics == [(NUMBER, "relay-replaced")]
    _no_writes(tracker)


def test_a_relay_the_receiver_no_longer_declares_is_undeclared(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    _post(tracker, SOURCE_ATTEMPT, _record())

    claimed, diagnostics = _sweep(tracker, ledger, UNDECLARED)

    assert claimed is None
    assert diagnostics == [(NUMBER, "relay-undeclared")]
    _no_writes(tracker)


def test_valid_record_wins_over_the_selection_label(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    tracker.inputs[NUMBER] = _issue(RELAY_LABEL, SELECTION_LABEL)
    _post(tracker, SOURCE_ATTEMPT, _record())

    claimed, _ = _sweep(tracker, ledger)

    assert isinstance(claimed, ConfirmedClaim)
    stored = ledger.get_attempt_pin(claimed.attempt_id)
    assert stored is not None and stored.relay_source_attempt == SOURCE_ATTEMPT


def test_without_a_valid_record_the_selection_lane_and_its_actor_test_apply(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    tracker.inputs[NUMBER] = _issue(RELAY_LABEL, SELECTION_LABEL)
    tracker.labeled_events[(NUMBER, SELECTION_LABEL)] = LabeledEvent(
        label=SELECTION_LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-11-02T00:00:00Z",
    )

    claimed, diagnostics = _sweep(tracker, ledger)

    assert diagnostics == [(NUMBER, "relay-replaced")]
    assert isinstance(claimed, ConfirmedClaim)
    stored = ledger.get_attempt_pin(claimed.attempt_id)
    assert stored is not None and stored.relay_source_attempt is None
    assert stored.selection_actor == "human"


def test_a_record_superseded_between_sweep_and_readback_releases_the_claim(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    _post(tracker, SOURCE_ATTEMPT, _record())

    def supersede(_: int) -> None:
        _post(tracker, "other-workflow#9/1", None)

    tracker.on_assign = supersede

    claimed, _ = _sweep(tracker, ledger)

    # A released Claim opens no Attempt: the Worker's own Claim Comment is
    # deleted and no model work follows.
    assert claimed is None
    assert "delete_comment" in tracker.call_names()
    assert not ledger.has_unfinished_attempt()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"record": None}, "relay-replaced"),
        ({"declared": False}, "relay-undeclared"),
        ({"claim_in_force": True}, "claim"),
        ({"blocker_open": True}, "blocker"),
        ({"agent_pr_open": True}, "agent-pr"),
    ],
)
def test_exclusion_reason_order(kwargs: dict[str, object], expected: str | None) -> None:
    params: dict[str, object] = {
        "record": _record(),
        "declared": True,
        "relay_label": RELAY_LABEL,
        "blocker_open": False,
        "claim_in_force": False,
    }
    params.update(kwargs)

    assert relay_exclusion_reason(_issue(RELAY_LABEL), **params) == expected  # type: ignore[arg-type]
    assert (
        relay_exclusion_reason(_issue(), **params) == "relay-label"  # type: ignore[arg-type]
    )
    assigned = relay_exclusion_reason(
        _issue(RELAY_LABEL, assignees=("x",)), **params  # type: ignore[arg-type]
    )
    assert assigned in ("assigned", "relay-replaced", "relay-undeclared")


def test_receiver_filters_apply_in_the_relay_lane(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    from dataclasses import replace

    tracker.inputs[NUMBER] = _issue(RELAY_LABEL, "ready-for-human")
    _post(tracker, SOURCE_ATTEMPT, _record())
    claim = replace(_claim_config(tracker), exclude=("ready-for-human",))

    claimed = sweep_and_claim(
        tracker, tracker, ledger, claim, relay=_relay_config(tracker)
    )

    assert claimed is None
    _no_writes(tracker)
