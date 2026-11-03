"""The Worker chooses its own work and claims it (contract §4, §5).

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (Selection: the
fixed guards, the Workflow filters and the ``(created_at, number)`` order;
selection writes nothing) and §5 (the Claim is an assignment read back:
the Attempt row exists before the assignment, a failed read-back releases
the Claim and moves to the next candidate).

MVP row ids: P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1, P-CLM-1,
P-CLM-2.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.ledger.store import RunLedger
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker.claim import ClaimConfig, select_and_claim
from support.fake_tracker import FakeTracker

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


def _candidate(
    number: int,
    *,
    body: str = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
    is_open: bool = True,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
    created_at: str = "2026-10-01T00:00:00Z",
) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title="Add a greeting",
        body=body,
        is_open=is_open,
        assignees=assignees,
        labels=labels,
        created_at=created_at,
    )


def _config(**overrides: object) -> ClaimConfig:
    values: dict[str, object] = {
        "selection_label": SELECTION_LABEL,
        "ready_for_human": READY_FOR_HUMAN,
        "base_branch": "main",
        "agent_login": "coding-agent",
        "agent_identity_id": 4242,
        "effective_configuration": {"repo": "example/test"},
    }
    values.update(overrides)
    return ClaimConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    holder = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        yield holder
    finally:
        holder.close()


def test_claims_the_only_eligible_candidate(
    ledger: RunLedger,
) -> None:
    """§5: the Claim confirms the eligible candidate's Attempt."""
    tracker = FakeTracker()
    tracker.candidates[31] = _candidate(31)

    claimed = select_and_claim(tracker=tracker, ledger=ledger, config=_config())

    assert claimed is not None
    assert (claimed.attempt_id, claimed.issue_number, claimed.attempt_number) == (
        "#31/1",
        31,
        1,
    )
    assert ledger.get_attempt("#31/1").state == "claim-confirmed"


def test_claim_assigns_and_writes_nothing_else(ledger: RunLedger) -> None:
    """P-CLM-1: the Claim assigns the Agent Identity — no label, no comment."""
    tracker = FakeTracker()
    tracker.candidates[31] = _candidate(31)

    select_and_claim(tracker=tracker, ledger=ledger, config=_config())

    assert tracker.candidates[31].assignees == ("coding-agent",)
    assert "assign_issue" in tracker.call_names()
    assert "post_terminal_comment" not in tracker.call_names()
    assert "add_ready_for_human" not in tracker.call_names()
    assert "remove_selection_label" not in tracker.call_names()


def test_older_candidate_claimed_first_with_number_tiebreak(
    ledger: RunLedger,
) -> None:
    """P-SEL-5: ``(created_at, number)`` ascending decides the first Claim."""
    tracker = FakeTracker()
    tracker.candidates[32] = _candidate(32, created_at="2026-10-02T00:00:00Z")
    tracker.candidates[31] = _candidate(31, created_at="2026-10-02T00:00:00Z")
    tracker.candidates[30] = _candidate(30, created_at="2026-10-01T00:00:00Z")

    claimed = select_and_claim(tracker=tracker, ledger=ledger, config=_config())

    assert claimed is not None
    assert claimed.issue_number == 30


def test_ready_for_human_candidate_never_claimed_nothing_written(
    ledger: RunLedger,
) -> None:
    """P-SEL-1: selection writes nothing, even for a doubly-labelled issue."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(
        30, labels=(SELECTION_LABEL, READY_FOR_HUMAN)
    )

    assert select_and_claim(tracker=tracker, ledger=ledger, config=_config()) is None

    assert tracker.call_names() == [
        "list_candidates",
        "has_open_blocker",
        "has_open_attempt_pull_request",
    ]


def test_assigned_candidate_never_claimed(ledger: RunLedger) -> None:
    """P-SEL-2: any assignee excludes — the Agent Identity with no open Attempt too."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30, assignees=("someone-else",))
    tracker.candidates[31] = _candidate(31, assignees=("coding-agent",))

    assert select_and_claim(tracker=tracker, ledger=ledger, config=_config()) is None

    assert "assign_issue" not in tracker.call_names()


def test_blocked_candidate_skipped_then_claimed_once_unblocked(
    ledger: RunLedger,
) -> None:
    """P-SEL-3: an open native blocker excludes until it closes."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30)
    tracker.blockers_open[30] = True

    assert select_and_claim(tracker=tracker, ledger=ledger, config=_config()) is None

    tracker.blockers_open[30] = False

    claimed = select_and_claim(tracker=tracker, ledger=ledger, config=_config())
    assert claimed is not None
    assert claimed.issue_number == 30


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(40, is_open=False),
        _candidate(41, body="   \n"),
        _candidate(42, labels=(SELECTION_LABEL, "wontfix")),
        _candidate(43, labels=(SELECTION_LABEL, "wayfinder:explore")),
    ],
)
def test_each_remaining_guard_excludes(
    ledger: RunLedger, candidate: CandidateIssue
) -> None:
    """P-SEL-4: closed, empty, ``wontfix`` and ``wayfinder:*`` are never claimed."""
    tracker = FakeTracker()
    tracker.candidates[candidate.number] = candidate

    assert select_and_claim(tracker=tracker, ledger=ledger, config=_config()) is None

    assert "assign_issue" not in tracker.call_names()


def test_open_marked_pull_request_excludes(ledger: RunLedger) -> None:
    """P-SEL-4: an open pull request carrying an Attempt Marker excludes."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30)
    tracker.attempt_prs_open.add(30)

    assert select_and_claim(tracker=tracker, ledger=ledger, config=_config()) is None

    assert "assign_issue" not in tracker.call_names()


def test_workflow_filters_narrow_selection(ledger: RunLedger) -> None:
    """C-SEL-1: ``require_all``, ``require_any`` and ``exclude`` narrow the set."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30, labels=(SELECTION_LABEL, "small"))
    tracker.candidates[31] = _candidate(31, labels=(SELECTION_LABEL, "small", "fast"))

    claimed = select_and_claim(
        tracker=tracker,
        ledger=ledger,
        config=_config(require_all=("small", "fast"), exclude=("slow",)),
    )

    assert claimed is not None
    assert claimed.issue_number == 31


def test_filter_failure_at_read_back_releases_the_claim(
    ledger: RunLedger,
) -> None:
    """C-SEL-1: filters are re-evaluated at the read-back; failure releases."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30, labels=(SELECTION_LABEL, "small"))
    tracker.candidates[31] = _candidate(31, labels=(SELECTION_LABEL, "small"))
    tracker.on_assign = lambda number: tracker.candidates.__setitem__(
        number,
        CandidateIssue(
            number=number,
            title=tracker.candidates[number].title,
            body=tracker.candidates[number].body,
            is_open=True,
            assignees=tracker.candidates[number].assignees,
            labels=(*tracker.candidates[number].labels, "slow"),
            created_at=tracker.candidates[number].created_at,
        ),
    )

    claimed = select_and_claim(
        tracker=tracker,
        ledger=ledger,
        config=_config(exclude=("slow",)),
    )

    assert claimed is None
    assert ledger.get_attempt("#30/1").state == "released"
    assert ledger.get_attempt("#31/1").state == "released"
    assert tracker.call_names().count("assign_issue") == 2
    assert tracker.call_names().count("release_assignment") == 2


def test_other_assignee_before_read_back_releases_and_moves_on(
    ledger: RunLedger,
) -> None:
    """P-CLM-2: a rival assignment releases the Claim; the next candidate wins."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30)
    tracker.candidates[31] = _candidate(31)

    def race(number: int) -> None:
        if number == 30:
            current = tracker.candidates[number]
            tracker.candidates[number] = CandidateIssue(
                number=number,
                title=current.title,
                body=current.body,
                is_open=current.is_open,
                assignees=(*current.assignees, "human"),
                labels=current.labels,
                created_at=current.created_at,
            )

    tracker.on_assign = race

    claimed = select_and_claim(tracker=tracker, ledger=ledger, config=_config())

    assert claimed is not None
    assert claimed.issue_number == 31
    assert ledger.get_attempt("#30/1").state == "released"
    assert ledger.get_attempt("#31/1").state == "claim-confirmed"
    assert tracker.candidates[30].assignees == ("human",)
    assert tracker.call_names().count("release_assignment") == 1


def test_attempt_row_exists_before_the_assignment(ledger: RunLedger) -> None:
    """§5: recovery relies on it — the Attempt row predates the assignment."""
    tracker = FakeTracker()
    tracker.candidates[30] = _candidate(30)
    seen: dict[str, object] = {}

    def observe(name: str) -> None:
        if name == "assign_issue":
            record = ledger.get_attempt("#30/1")
            seen["state"] = record.state
            seen["assigned_by_us"] = record.assigned_by_us

    tracker.on_first_write = observe

    select_and_claim(tracker=tracker, ledger=ledger, config=_config())

    assert seen == {"state": "open", "assigned_by_us": None}


def test_unfinished_attempt_blocks_intake(ledger: RunLedger) -> None:
    """§3: while an Attempt is unfinished, the Worker selects no new work."""
    ledger.open_attempt(
        "#30/1", effective_configuration={"repo": "example/test"}
    )
    tracker = FakeTracker()
    tracker.candidates[31] = _candidate(31)

    assert select_and_claim(tracker=tracker, ledger=ledger, config=_config()) is None

    assert "assign_issue" not in tracker.call_names()
