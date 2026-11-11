"""The Claim sequence: intent, assignment, Claim Comment, read-back, pin (§6).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §6 (Claim
and Attempt identity).

Row ids: INP-1, INP-2, INP-4 … INP-6, INP-12 … INP-14, INP-19, INP-20,
REC-10, REC-22.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    attempt_claim,
    check_eligibility,
    recover_unconfirmed_claim,
    sweep_and_claim,
)
from coding_agent.inputs.port import InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger, step_effect_key
from support.fake_step_tracker import FakeStepTracker

LABEL = "agent:implement"
PR_LABEL = "agent:review"


def _issue_input(
    *,
    number: int = 42,
    labels: tuple[str, ...] = (LABEL,),
    assignees: tuple[str, ...] = (),
    is_open: bool = True,
    title: str = "Add a greeting",
    body: str = "a body",
    created_at: str = "2026-10-01T00:00:00Z",
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=title,
        body=body,
        is_open=is_open,
        labels=labels,
        assignees=assignees,
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _pull_input(
    *,
    number: int = 42,
    labels: tuple[str, ...] = (PR_LABEL,),
    head_sha: str = "h" * 40,
    is_open: bool = True,
    merged: bool = False,
    draft: bool = False,
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="pull-request",
        title="Add a greeting",
        body="a body",
        is_open=is_open,
        labels=labels,
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
        draft=draft,
        merged=merged,
        head_sha=head_sha,
        head_branch="feature",
        head_repo="octocat/sandbox",
        base_branch="main",
    )


def _human_event(number: int, label: str = LABEL) -> LabeledEvent:
    return LabeledEvent(
        label=label,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )


def _config(
    tracker: FakeStepTracker,
    *,
    workflow_id: str = "implement",
    input_type: InputType = "issue",
    label: str = LABEL,
    skip_open_agent_pr: bool = False,
) -> ClaimConfig:
    effective: Mapping[str, object] = {"workflow": workflow_id}
    return ClaimConfig(
        workflow_id=workflow_id,
        input_type=input_type,
        selection_label=label,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration=effective,
        skip_open_agent_pr=skip_open_agent_pr,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> RunLedger:
    """A fresh Run Ledger per test."""
    return RunLedger(tmp_path / "ledger.db")


@pytest.fixture
def tracker() -> FakeStepTracker:
    """A dedicated-identity fake tracker with one labelled issue."""
    fake = FakeStepTracker()
    fake.inputs[42] = _issue_input()
    fake.labeled_events[(42, LABEL)] = _human_event(42)
    return fake


def test_issue_claim_confirms_and_pins_from_the_readback(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-14: intent, assignment, Claim Comment, read-back, pin, confirmed."""
    claimed = sweep_and_claim(tracker, ledger, _config(tracker))
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.attempt_id == "implement#42/1"
    assert claimed.n == 1
    assert claimed.pin.fingerprint
    assert claimed.pin.base_revision == tracker.base_tip
    assert claimed.pin.input_head is None
    assert claimed.pin.selection_actor == "human"
    assert claimed.pin.identity_mode == "dedicated"

    record = ledger.get_attempt("implement#42/1")
    assert record.state == "claim-confirmed"
    assert record.fingerprint == claimed.pin.fingerprint
    assert record.base_revision == claimed.pin.base_revision

    pin = ledger.get_attempt_pin("implement#42/1")
    assert pin is not None
    assert pin.selection_actor == "human"
    assert pin.identity_mode == "dedicated"
    assert ledger.get_outcome("implement#42/1") is None

    names = tracker.call_names()
    assert names.index("assign_input") < names.index("post_claim_comment")
    assert "remove_selection_label" not in names

    intent = ledger.get_step_effect("implement#42/1", "", 0, 1)
    assert intent is not None
    assert intent.status == "receipt"
    assert intent.remote_id is not None


def test_claim_comment_carries_attempt_id_and_intent_key(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-5, OP-14: the Claim Comment bears kind=claim and the intent key."""
    claimed = sweep_and_claim(tracker, ledger, _config(tracker))
    assert isinstance(claimed, ConfirmedClaim)
    body = tracker.comments[-1].body
    assert "implement#42/1" in body
    assert "kind=claim" in body
    assert "implement#42/1/<claim>/0/1" in body


def test_second_attempt_counts_up_and_never_reuses(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-13: ``implement#42/1``, ``implement#42/2``; ``review#42/1`` apart."""
    first = sweep_and_claim(tracker, ledger, _config(tracker))
    assert isinstance(first, ConfirmedClaim)
    ledger.set_attempt_state(first.attempt_id, "finished")
    tracker.inputs[42] = _issue_input(assignees=())
    second = sweep_and_claim(tracker, ledger, _config(tracker))
    assert isinstance(second, ConfirmedClaim)
    assert second.attempt_id == "implement#42/2"

    review_tracker = FakeStepTracker()
    review_tracker.inputs[42] = _pull_input()
    review_tracker.labeled_events[(42, PR_LABEL)] = LabeledEvent(
        label=PR_LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )
    review = sweep_and_claim(
        review_tracker,
        ledger,
        _config(
            review_tracker,
            workflow_id="review",
            input_type="pull-request",
            label=PR_LABEL,
        ),
        merge_base_resolver=lambda head, tip: "m" * 40,
    )
    assert isinstance(review, ConfirmedClaim)
    assert review.attempt_id == "review#42/1"
    assert review.pin.input_head == "h" * 40
    assert review.pin.base_revision == "m" * 40


def test_pull_request_claim_pins_head_and_merge_base(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-1: the read-back pins the Input Head and the merge-base revision."""
    tracker.inputs[42] = _pull_input(labels=(LABEL,))
    tracker.labeled_events[(42, LABEL)] = _human_event(42)
    claimed = attempt_claim(
        tracker,
        ledger,
        _config(tracker),
        tracker.inputs[42],
        identity_mode="dedicated",
        merge_base_resolver=lambda head, tip: "m" * 40,
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pin.input_head == "h" * 40
    assert claimed.pin.base_revision == "m" * 40
    assert claimed.pin.base_branch == "main"


def test_readback_uses_fresh_facts_not_the_listing(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-14: a body change between listing and read-back pins the read-back."""

    def _rewrite(number: int) -> None:
        held = tracker.inputs[number]
        tracker.inputs[number] = TrackedInput(
            number=held.number,
            input_type=held.input_type,
            title=held.title,
            body="the edited body",
            is_open=held.is_open,
            labels=held.labels,
            assignees=held.assignees,
            created_at=held.created_at,
            author_login=held.author_login,
            author_id=held.author_id,
        )

    tracker.on_assign = _rewrite
    claimed = sweep_and_claim(tracker, ledger, _config(tracker))
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pin.body == "the edited body"


def test_bot_label_refused_under_shared_identity(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-6, INP-19: a bot's label is refused at Claim under shared identity."""
    tracker.owner_id = 1001
    tracker.labeled_events[(42, LABEL)] = LabeledEvent(
        label=LABEL,
        actor_login="some-bot",
        actor_id=4321,
        actor_type="Bot",
        created_at="2026-10-01T00:00:00Z",
    )
    assert sweep_and_claim(tracker, ledger, _config(tracker)) is None
    assert tracker.call_names().count("assign_input") == 0


def test_agent_identity_label_accepted_under_shared_identity(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-19: the owner's own label application is accepted when shared."""
    tracker.owner_id = 1001
    tracker.labeled_events[(42, LABEL)] = LabeledEvent(
        label=LABEL,
        actor_login="coding-agent",
        actor_id=1001,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )
    claimed = sweep_and_claim(tracker, ledger, _config(tracker))
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pin.identity_mode == "shared-identity"


def test_agent_identity_label_refused_when_dedicated(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-20: the Agent Identity's own label is refused when dedicated."""
    tracker.labeled_events[(42, LABEL)] = LabeledEvent(
        label=LABEL,
        actor_login="coding-agent",
        actor_id=1001,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )
    assert sweep_and_claim(tracker, ledger, _config(tracker)) is None


def test_competing_claim_lowest_comment_id_wins(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-5: two Workers, one Agent Identity — the lowest comment id wins."""
    first = attempt_claim(
        tracker, ledger, _config(tracker), tracker.inputs[42], identity_mode="dedicated"
    )
    assert isinstance(first, ConfirmedClaim)

    second_ledger = RunLedger(ledger.path.parent / "ledger-b.db")
    loser = attempt_claim(
        tracker,
        second_ledger,
        _config(tracker),
        tracker.inputs[42],
        identity_mode="dedicated",
    )
    assert isinstance(loser, ReleasedClaim)
    assert loser.reason == "claim-lost"
    assert second_ledger.get_attempt("implement#42/1").state == "released"
    assert second_ledger.get_outcome("implement#42/1") is None
    assert [comment.comment_id for comment in tracker.comments] == [1]
    assert tracker.inputs[42].assignees == ("coding-agent",)


def test_readback_failure_releases_and_cleans_up(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-6: a closed input at read-back releases with no terminal comment."""
    tracker.inputs[42] = _issue_input()
    tracker.labeled_events[(42, LABEL)] = _human_event(42)

    def _close(number: int) -> None:
        held = tracker.inputs[number]
        tracker.inputs[number] = TrackedInput(
            number=held.number,
            input_type=held.input_type,
            title=held.title,
            body=held.body,
            is_open=False,
            labels=held.labels,
            assignees=held.assignees,
            created_at=held.created_at,
            author_login=held.author_login,
            author_id=held.author_id,
        )

    tracker.on_assign = _close
    claimed = attempt_claim(
        tracker,
        ledger,
        _config(tracker),
        tracker.inputs[42],
        identity_mode="dedicated",
    )
    assert isinstance(claimed, ReleasedClaim)
    assert claimed.reason == "closed"
    assert tracker.comments == []
    assert tracker.inputs[42].assignees == ()
    record = ledger.get_attempt("implement#42/1")
    assert record.state == "released"
    assert ledger.get_outcome("implement#42/1") is None


def test_other_assignee_at_readback_releases(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§6.2: another assignee at read-back releases the Claim."""

    def _race(number: int) -> None:
        held = tracker.inputs[number]
        tracker.inputs[number] = TrackedInput(
            number=held.number,
            input_type=held.input_type,
            title=held.title,
            body=held.body,
            is_open=held.is_open,
            labels=held.labels,
            assignees=(*held.assignees, "human"),
            created_at=held.created_at,
            author_login=held.author_login,
            author_id=held.author_id,
        )

    tracker.on_assign = _race
    claimed = attempt_claim(
        tracker,
        ledger,
        _config(tracker),
        tracker.inputs[42],
        identity_mode="dedicated",
    )
    assert isinstance(claimed, ReleasedClaim)
    assert claimed.reason == "assigned"


def test_skip_open_agent_pr_skips_the_issue(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-4: with ``skip-open-agent-pr`` the issue waits while its agent PR is open."""
    tracker.open_pull_requests.append(
        TrackedInput(
            number=11,
            input_type="pull-request",
            title="Implement #42",
            body="<!-- coding-agent attempt=implement#42/1 kind=claim -->",
            is_open=True,
            labels=(),
            assignees=(),
            created_at="2026-10-02T00:00:00Z",
            author_login="coding-agent",
            author_id=1001,
        )
    )
    skipping = _config(tracker, skip_open_agent_pr=True)
    assert sweep_and_claim(tracker, ledger, skipping) is None
    assert tracker.call_names().count("assign_input") == 0
    plain = _config(tracker, skip_open_agent_pr=False)
    claimed = sweep_and_claim(tracker, ledger, plain)
    assert isinstance(claimed, ConfirmedClaim)


def test_priority_input_claimed_first_under_same_guards(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-12: ``next issue N`` passes the same guards and claims first."""
    tracker.inputs[7] = _issue_input(
        number=7, created_at="2026-09-01T00:00:00Z", body="older"
    )
    tracker.labeled_events[(7, LABEL)] = _human_event(7)
    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=42)
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 42


def test_guarded_priority_falls_back_to_order(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-12: a guarded priority resolves without claiming; the sweep continues."""
    tracker.inputs[7] = _issue_input(
        number=7, created_at="2026-09-01T00:00:00Z", body="older"
    )
    tracker.labeled_events[(7, LABEL)] = _human_event(7)
    tracker.labeled_events[(42, LABEL)] = LabeledEvent(
        label=LABEL,
        actor_login="some-bot",
        actor_id=4321,
        actor_type="Bot",
        created_at="2026-10-01T00:00:00Z",
    )
    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=42)
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 7


def test_check_eligibility_applies_the_same_guards(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """INP-12: eligibility reads fresh and never claims."""
    assert (
        check_eligibility(tracker, ledger, _config(tracker), 42, identity_mode="dedicated")
        is None
    )
    assert tracker.call_names().count("assign_input") == 0
    assert (
        check_eligibility(tracker, ledger, _config(tracker), 404, identity_mode="dedicated")
        == "selection-label"
    )


def test_unconfirmed_claim_recovered_by_key(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """REC-10: restart deletes the own Claim Comment by key and releases."""
    tracker.inputs[42] = _issue_input(assignees=("coding-agent",))
    key = step_effect_key("implement#42/1", "", 0, 1)
    tracker.post_claim_comment(42, "issue", f"stale claim {key}")
    ledger.open_attempt(
        "implement#42/1",
        effective_configuration={"workflow": "implement"},
        agent_identity_id=1001,
    )
    ledger.set_assigned_by_us("implement#42/1", True)
    recover_unconfirmed_claim(tracker, ledger, "implement#42/1", 42, "issue")
    assert tracker.comments == []
    assert tracker.inputs[42].assignees == ()
    assert ledger.get_attempt("implement#42/1").state == "released"


def test_recovery_keeps_assignment_while_another_claim_holds(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """REC-10: the assignment stays while another Claim Comment is in force."""
    tracker.inputs[42] = _issue_input(assignees=("coding-agent",))
    tracker.post_claim_comment(42, "issue", "other worker claim kind=claim marker")
    ledger.open_attempt(
        "implement#42/1",
        effective_configuration={"workflow": "implement"},
        agent_identity_id=1001,
    )
    recover_unconfirmed_claim(tracker, ledger, "implement#42/1", 42, "issue")
    assert tracker.inputs[42].assignees == ("coding-agent",)
    assert ledger.get_attempt("implement#42/1").state == "released"


def test_next_n_rebuilt_from_marks_where_ledger_lost(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """REC-22: the Claim takes the max of ledger, branches and markers."""
    tracker.open_pull_requests.append(
        TrackedInput(
            number=11,
            input_type="pull-request",
            title="Implement #42",
            body="<!-- coding-agent attempt=implement#42/3 kind=terminal -->",
            is_open=True,
            labels=(),
            assignees=(),
            created_at="2026-10-02T00:00:00Z",
            author_login="coding-agent",
            author_id=1001,
        )
    )
    claimed = attempt_claim(
        tracker,
        ledger,
        _config(tracker),
        tracker.inputs[42],
        identity_mode="dedicated",
        known_markers=["<!-- coding-agent attempt=implement#42/3 kind=terminal -->"],
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.attempt_id == "implement#42/4"
