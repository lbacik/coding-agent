"""The Selection and Claim operations behave the same on every side.

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (candidates are the
open issues carrying the Workflow's Selection Label; selection writes
nothing) and §5 (the Claim assigns the Agent Identity — no label, no
comment).

MVP row ids: P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1, P-CLM-1,
P-CLM-2.

The suite runs against the fake tracker in ``tests/support/``. Reads assert
reported facts; the Claim tests assert the write order and that selection
itself never writes.
"""

from __future__ import annotations

from coding_agent.tracker.port import CandidateIssue
from support.fake_tracker import FakeTracker

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


def _candidate(
    number: int,
    *,
    title: str = "Add a greeting",
    body: str = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
    is_open: bool = True,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
    created_at: str = "2026-10-01T00:00:00Z",
) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title=title,
        body=body,
        is_open=is_open,
        assignees=assignees,
        labels=labels,
        created_at=created_at,
    )


def _fake_tracker() -> FakeTracker:
    tracker = FakeTracker(agent_login="octocat")
    tracker.candidates[31] = _candidate(31, created_at="2026-10-01T00:00:00Z")
    tracker.candidates[30] = _candidate(
        30,
        assignees=("someone-else",),
        labels=(SELECTION_LABEL, READY_FOR_HUMAN),
        created_at="2026-09-01T00:00:00Z",
    )
    return tracker


def test_list_candidates_reports_labeled_issues() -> None:
    """§4: the candidates are the open issues carrying the Selection Label."""
    tracker = _fake_tracker()
    listed = tracker.list_candidates(SELECTION_LABEL)

    assert [candidate.number for candidate in listed] == [30, 31]
    eligible = next(item for item in listed if item.number == 31)
    assert eligible.title == "Add a greeting"
    assert eligible.body.startswith("## Acceptance criteria")
    assert eligible.is_open is True
    assert eligible.assignees == ()
    assert SELECTION_LABEL in eligible.labels
    assert eligible.created_at == "2026-10-01T00:00:00Z"


def test_fetch_candidate_reports_every_guard_fact() -> None:
    """§4: the read-back carries assignees, labels, state and body."""
    tracker = _fake_tracker()
    fetched = tracker.fetch_candidate(30)

    assert fetched.is_open is True
    assert fetched.assignees == ("someone-else",)
    assert READY_FOR_HUMAN in fetched.labels


def test_selection_writes_nothing_for_ready_for_human() -> None:
    """P-SEL-1: a doubly-labelled issue is never claimed; selection writes nothing."""
    tracker = _fake_tracker()
    calls_before = list(tracker.calls)
    tracker.list_candidates(SELECTION_LABEL)
    tracker.fetch_candidate(30)

    assert tracker.calls[len(calls_before):] == [
        ("list_candidates", (SELECTION_LABEL,)),
        ("fetch_candidate", (30,)),
    ]


def test_claim_assigns_without_label_or_comment() -> None:
    """P-CLM-1: the Claim assigns the Agent Identity — no label, no comment."""
    tracker = _fake_tracker()
    labels_before = tracker.candidates[31].labels
    tracker.assign_issue(31)

    assert tracker.candidates[31].assignees == (tracker.agent_login,)
    assert tracker.candidates[31].labels == labels_before
    assert "post_terminal_comment" not in tracker.call_names()
    assert "add_ready_for_human" not in tracker.call_names()
    assert "remove_selection_label" not in tracker.call_names()

    tracker.release_assignment(31)
