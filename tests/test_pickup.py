"""Round Handoff pickup: the Pickup Label ahead of the Selection Label (§8).

Issue #612, acceptance bullet 4. A pushed offer is claimed under the same
Agent Identity before model work: the sweep requires the newest trusted
terminal record for the Workflow and Target Issue, a supported version, a
matching marker, the numeric Agent Identity, the Pickup Label signal,
revision SHAs and counter evidence, then revalidates the same source at
the Claim read-back. Invalid, stale, replaced or exhausted signals stay
ineligible — including with both labels present.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    sweep_and_claim,
)
from coding_agent.inputs.pickup import (
    PickupConfig,
    attempt_pickup_claim,
    newest_pickup_offer,
    pickup_exclusion_reason,
)
from coding_agent.inputs.port import InputComment, InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.round_handoff import (
    ROUND_HANDOFF_FENCE_CLOSE,
    RoundHandoffOffer,
    render_handoff_section,
)
from coding_agent.runtime.step_finalisation import render_terminal_comment
from support.fake_step_tracker import FakeStepTracker

WORKFLOW = "implement"
SELECTION_LABEL = "agent:implement"
PICKUP_LABEL = "round-finished"
AGENT_ID = 1001
WORK = "a" * 40
BASE = "b" * 40
TIP = "c" * 40


def _offer(**overrides: object) -> RoundHandoffOffer:
    fields: dict[str, object] = {
        "workflow_id": WORKFLOW,
        "issue": 9,
        "attempt": f"{WORKFLOW}#9/1",
        "work_commit": WORK,
        "ref": "agent/implement-9-1-work",
        "base_revision": BASE,
        "note_kind": "hard",
        "note": "did the thing",
        "pickup_label": PICKUP_LABEL,
        "used": 0,
        "limit": 2,
        "selection_actor": "human",
        "selection_actor_id": 7,
        "selection_actor_type": "User",
        "selection_actor_at": "2026-11-01T00:00:00Z",
    }
    fields.update(overrides)
    return RoundHandoffOffer(**fields)  # type: ignore[arg-type]


def _terminal_body(attempt: str, offer: RoundHandoffOffer | None) -> str:
    section = render_handoff_section(offer) if offer is not None else ""
    return render_terminal_comment(
        attempt_id=attempt,
        outcome="unfinished",
        ending_step="publish",
        step_result="pushed",
        workflow_end="unfinished",
        handoff_section=section,
    )


def _trusted_comment(
    tracker: FakeStepTracker, attempt: str, offer: RoundHandoffOffer | None
) -> InputComment:
    comment = InputComment(
        comment_id=tracker._next_comment_id,
        body=_terminal_body(attempt, offer),
        author_login=tracker.agent_login,
        author_id=tracker.agent_id,
    )
    tracker._next_comment_id += 1
    tracker.comments.append(comment)
    return comment


def _issue(
    *,
    number: int = 9,
    labels: tuple[str, ...] = (SELECTION_LABEL, PICKUP_LABEL),
    assignees: tuple[str, ...] = (),
    created_at: str = "2026-11-02T00:00:00Z",
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=True,
        labels=labels,
        assignees=assignees,
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _claim_config(tracker: FakeStepTracker) -> ClaimConfig:
    effective: Mapping[str, object] = {"workflow": WORKFLOW}
    return ClaimConfig(
        workflow_id=WORKFLOW,
        input_type="issue",
        selection_label=SELECTION_LABEL,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration=effective,
    )


def _pickup_config(tracker: FakeStepTracker, **overrides: object) -> PickupConfig:
    effective: Mapping[str, object] = {"workflow": WORKFLOW}
    fields: dict[str, object] = {
        "workflow_id": WORKFLOW,
        "pickup_label": PICKUP_LABEL,
        "limit": 2,
        "agent_login": tracker.agent_login,
        "agent_id": tracker.agent_id,
        "base_branch": "main",
        "effective_configuration": effective,
    }
    fields.update(overrides)
    return PickupConfig(**fields)  # type: ignore[arg-type]


@pytest.fixture
def ledger(tmp_path: Path) -> RunLedger:
    """A fresh Run Ledger per test."""
    return RunLedger(tmp_path / "ledger.db")


@pytest.fixture
def tracker() -> FakeStepTracker:
    """A dedicated-identity fake tracker with a pickup-signalled issue."""
    fake = FakeStepTracker()
    fake.base_tip = TIP
    fake.inputs[9] = _issue()
    fake.labeled_events[(9, SELECTION_LABEL)] = LabeledEvent(
        label=SELECTION_LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-11-02T00:00:00Z",
    )
    return fake


def _seed_offer(
    tracker: FakeStepTracker, offer: RoundHandoffOffer, attempt: str | None = None
) -> InputComment:
    return _trusted_comment(tracker, attempt or offer.attempt, offer)


def test_newest_offer_wins_over_older_record(tracker: FakeStepTracker) -> None:
    """The newest trusted terminal record for the Workflow and issue wins."""
    first = _seed_offer(tracker, _offer(work_commit="d" * 40, used=0))
    second = _seed_offer(tracker, _offer(work_commit=WORK, used=1))
    found = newest_pickup_offer(
        tracker.list_comments(9, "issue"),
        agent_id=tracker.agent_id,
        workflow_id=WORKFLOW,
        issue_number=9,
    )
    assert found is not None
    offer, comment = found
    assert offer.work_commit == WORK
    assert offer.used == 1
    assert comment.comment_id == second.comment_id
    assert first.comment_id < second.comment_id


def test_replaced_source_reads_as_absent(tracker: FakeStepTracker) -> None:
    """A newer ordinary trusted terminal supersedes the offer (replaced)."""
    _seed_offer(tracker, _offer())
    _trusted_comment(tracker, f"{WORKFLOW}#9/2", None)
    assert (
        newest_pickup_offer(
            tracker.list_comments(9, "issue"),
            agent_id=tracker.agent_id,
            workflow_id=WORKFLOW,
            issue_number=9,
        )
        is None
    )


def test_mismatched_record_reads_as_absent(tracker: FakeStepTracker) -> None:
    """A record naming another attempt than its trusted marker never verifies."""
    _trusted_comment(tracker, f"{WORKFLOW}#9/1", _offer(attempt=f"{WORKFLOW}#9/2"))
    assert (
        newest_pickup_offer(
            tracker.list_comments(9, "issue"),
            agent_id=tracker.agent_id,
            workflow_id=WORKFLOW,
            issue_number=9,
        )
        is None
    )


def test_foreign_author_and_unknown_identity_trust_nothing(
    tracker: FakeStepTracker,
) -> None:
    """Trust needs the Agent Identity's numeric author id; ``None`` trusts nothing."""
    comment = _seed_offer(tracker, _offer())
    forged = replace(comment, author_id=7, author_login="human")
    tracker.comments[-1] = forged
    assert (
        newest_pickup_offer(
            tracker.list_comments(9, "issue"),
            agent_id=tracker.agent_id,
            workflow_id=WORKFLOW,
            issue_number=9,
        )
        is None
    )
    _seed_offer(tracker, _offer())
    assert (
        newest_pickup_offer(
            tracker.list_comments(9, "issue"),
            agent_id=None,
            workflow_id=WORKFLOW,
            issue_number=9,
        )
        is None
    )


def test_other_workflow_or_issue_records_do_not_match(
    tracker: FakeStepTracker,
) -> None:
    """Records for another Workflow or Target Issue never verify here."""
    _seed_offer(tracker, _offer(workflow_id="other"))
    _seed_offer(tracker, _offer(issue=10, attempt=f"{WORKFLOW}#10/1"))
    assert (
        newest_pickup_offer(
            tracker.list_comments(9, "issue"),
            agent_id=tracker.agent_id,
            workflow_id=WORKFLOW,
            issue_number=9,
        )
        is None
    )


def test_newer_writer_version_reads_as_absent(tracker: FakeStepTracker) -> None:
    """Only the supported record version verifies; a newer writer reads as absent."""
    comment = _seed_offer(tracker, _offer())
    body = comment.body.replace("round-handoff-record v1", "round-handoff-record v2")
    assert ROUND_HANDOFF_FENCE_CLOSE in body
    tracker.comments[-1] = replace(comment, body=body)
    assert (
        newest_pickup_offer(
            tracker.list_comments(9, "issue"),
            agent_id=tracker.agent_id,
            workflow_id=WORKFLOW,
            issue_number=9,
        )
        is None
    )


def test_exclusion_reasons_cover_label_record_and_counters() -> None:
    """The lane names a missing signal, a stale record and a spent counter."""
    candidate = _issue()
    assert (
        pickup_exclusion_reason(
            replace(candidate, labels=(SELECTION_LABEL,)),
            offer=_offer(),
            pickup_label=PICKUP_LABEL,
            blocker_open=False,
            claim_in_force=False,
        )
        == "pickup-label"
    )
    assert (
        pickup_exclusion_reason(
            candidate,
            offer=None,
            pickup_label=PICKUP_LABEL,
            blocker_open=False,
            claim_in_force=False,
        )
        == "pickup-replaced"
    )
    assert (
        pickup_exclusion_reason(
            candidate,
            offer=_offer(pickup_label="other-signal"),
            pickup_label=PICKUP_LABEL,
            blocker_open=False,
            claim_in_force=False,
        )
        == "pickup-replaced"
    )
    assert (
        pickup_exclusion_reason(
            candidate,
            offer=_offer(used=2, limit=2, exhausted=True),
            pickup_label=PICKUP_LABEL,
            blocker_open=False,
            claim_in_force=False,
        )
        == "pickup-exhausted"
    )
    assert (
        pickup_exclusion_reason(
            candidate,
            offer=_offer(used=2, limit=2),
            pickup_label=PICKUP_LABEL,
            blocker_open=False,
            claim_in_force=False,
        )
        == "pickup-exhausted"
    )
    assert (
        pickup_exclusion_reason(
            candidate,
            offer=_offer(),
            pickup_label=PICKUP_LABEL,
            blocker_open=False,
            claim_in_force=False,
        )
        is None
    )


def test_pickup_claim_pins_recorded_revision_and_evidence(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """The pickup Claim retains the recorded Base Revision past a moved tip."""
    source = _seed_offer(tracker, _offer())
    assert tracker.base_tip != BASE
    claimed = attempt_pickup_claim(
        tracker,
        ledger,
        _claim_config(tracker),
        _pickup_config(tracker),
        _issue(),
        identity_mode="dedicated",
        offer=_offer(),
        source_comment=source,
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.attempt_id == f"{WORKFLOW}#9/2"
    assert claimed.n == 2
    assert claimed.pickup_offer is not None
    assert claimed.pickup_offer.work_commit == WORK
    assert claimed.pin.base_revision == BASE
    assert claimed.pin.base_tip == TIP
    assert claimed.pin.selection_actor == "human"
    assert claimed.pin.selection_actor_id == 7
    row = ledger.get_attempt_pin(claimed.attempt_id)
    assert row is not None
    assert row.base_revision == BASE


def test_pickup_sweep_runs_ahead_of_selection(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """The Pickup Label wins even where a selection-only issue sorts first."""
    tracker.inputs[7] = _issue(
        number=7,
        labels=(SELECTION_LABEL,),
        created_at="2026-11-01T00:00:00Z",
    )
    _seed_offer(tracker, _offer())
    claimed = sweep_and_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        pickup=_pickup_config(tracker),
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 9
    assert claimed.pickup_offer is not None


def test_exhausted_offer_falls_back_to_selection(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """An exhausted signal stays ineligible with both labels present."""
    _seed_offer(tracker, _offer(used=2, limit=2, exhausted=True))
    claimed = sweep_and_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        pickup=_pickup_config(tracker),
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 9
    assert claimed.pickup_offer is None
    assert claimed.pin.base_revision == TIP


def test_replaced_offer_falls_back_to_selection(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A replaced signal stays ineligible with both labels present."""
    _seed_offer(tracker, _offer())
    _trusted_comment(tracker, f"{WORKFLOW}#9/2", None)
    claimed = sweep_and_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        pickup=_pickup_config(tracker),
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pickup_offer is None


def test_readback_replacement_releases_the_claim(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A terminal record landing between listing and read-back releases."""
    source = _seed_offer(tracker, _offer())
    calls = 0
    real_list = tracker.list_comments

    def _appending_list(
        number: int, input_type: InputType
    ) -> list[InputComment]:
        nonlocal calls
        calls += 1
        comments = real_list(number, input_type)
        if calls == 2:
            _trusted_comment(tracker, f"{WORKFLOW}#9/3", None)
            comments = real_list(number, input_type)
        return comments

    tracker.list_comments = _appending_list  # type: ignore[method-assign]
    try:
        claimed = attempt_pickup_claim(
            tracker,
            ledger,
            _claim_config(tracker),
            _pickup_config(tracker),
            _issue(),
            identity_mode="dedicated",
            offer=_offer(),
            source_comment=source,
        )
    finally:
        tracker.list_comments = real_list  # type: ignore[method-assign]
    assert isinstance(claimed, ReleasedClaim)
    assert claimed.reason == "pickup-replaced"


def test_pickup_ignored_for_pull_request_inputs(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """The pickup lane never runs for pull-request Workflows."""
    tracker.inputs[9] = replace(_issue(), input_type="pull-request")
    config = _claim_config(tracker)
    config = replace(config, input_type="pull-request")
    claimed = sweep_and_claim(
        tracker, tracker, ledger, config, pickup=_pickup_config(tracker)
    )
    assert claimed is None


def test_misconfigured_pickup_keeps_selection(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A non-positive pickup limit leaves the ordinary selection path alone."""
    _seed_offer(tracker, _offer())
    claimed = sweep_and_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        pickup=_pickup_config(tracker, limit=0),
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pickup_offer is None


def test_pickup_priority_issue_still_confirms(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A ``next issue N`` reorder over the pickup issue still confirms it."""
    _seed_offer(tracker, _offer())
    claimed = sweep_and_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        priority=9,
        pickup=_pickup_config(tracker),
    )
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 9
    assert claimed.pickup_offer is not None
