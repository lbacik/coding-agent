"""Selection guards, filters and order (contract §4).

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (candidates are the
open issues carrying the Workflow's Selection Label; the fixed guards exclude
a candidate that is not open, has any assignee, carries ``ready-for-human``
(by its configured name), carries ``wontfix``, carries any ``wayfinder:*``
label, has an open native blocker, has an empty body, or has an open pull
request carrying an Attempt Marker for it; the Workflow's ``require_all``,
``require_any`` and ``exclude`` filters narrow the set further; the order is
``(created_at, number)`` ascending; selection writes nothing).

MVP row ids: P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1.
"""

from __future__ import annotations

from coding_agent.tracker.port import CandidateIssue
from coding_agent.tracker.selection import (
    exclusion_reason,
    parse_blocked_by_references,
    sort_candidates,
)


def _candidate(
    *,
    number: int = 7,
    title: str = "Add a greeting",
    body: str = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
    is_open: bool = True,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = ("ready-for-agent",),
    created_at: str = "2026-10-01T00:00:00Z",
) -> CandidateIssue:
    """One eligible candidate: open, unassigned, labelled, with a body."""
    return CandidateIssue(
        number=number,
        title=title,
        body=body,
        is_open=is_open,
        assignees=assignees,
        labels=labels,
        created_at=created_at,
    )


def test_eligible_candidate_has_no_exclusion_reason() -> None:
    """§4: an open, unassigned, labelled issue with a body is selectable."""
    assert (
        exclusion_reason(
            _candidate(), ready_for_human="ready-for-human", blocker_open=False
        )
        is None
    )


def test_closed_issue_is_excluded() -> None:
    """P-SEL-4: a closed issue is never claimed."""
    assert (
        exclusion_reason(
            _candidate(is_open=False),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "closed"
    )


def test_assigned_issue_is_excluded() -> None:
    """P-SEL-2: an issue with any assignee is never claimed."""
    assert (
        exclusion_reason(
            _candidate(assignees=("someone-else",)),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "assigned"
    )


def test_agent_identity_assignment_without_open_attempt_is_excluded() -> None:
    """P-SEL-2: the Agent Identity's own assignment excludes when no Attempt
    of ours is open for the issue."""
    assert (
        exclusion_reason(
            _candidate(assignees=("coding-agent",)),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "assigned"
    )


def test_empty_body_is_excluded() -> None:
    """P-SEL-4: an issue with an empty body is never claimed."""
    assert (
        exclusion_reason(
            _candidate(body=""),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "empty-body"
    )


def test_selection_label_plus_ready_for_human_is_excluded() -> None:
    """P-SEL-1: an issue carrying both the Selection Label and
    ``ready-for-human`` is never claimed."""
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "ready-for-human")),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "ready-for-human"
    )


def test_configured_ready_for_human_name_is_excluded() -> None:
    """P-SEL-1: the exclusion follows the configured ``ready-for-human`` name."""
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "needs-a-human")),
            ready_for_human="needs-a-human",
            blocker_open=False,
        )
        == "ready-for-human"
    )
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "ready-for-human")),
            ready_for_human="needs-a-human",
            blocker_open=False,
        )
        is None
    )


def test_wontfix_is_excluded() -> None:
    """P-SEL-4: an issue labelled ``wontfix`` is never claimed."""
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "wontfix")),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "wontfix"
    )


def test_wayfinder_label_is_excluded() -> None:
    """P-SEL-4: an issue carrying any ``wayfinder:*`` label is never claimed."""
    for labels in (
        ("ready-for-agent", "wayfinder:task"),
        ("ready-for-agent", "wayfinder:map"),
    ):
        assert (
            exclusion_reason(
                _candidate(labels=labels),
                ready_for_human="ready-for-human",
                blocker_open=False,
            )
            == "wayfinder"
        )


def test_open_blocker_is_excluded() -> None:
    """P-SEL-3: an issue with an open native blocker is skipped."""
    assert (
        exclusion_reason(
            _candidate(), ready_for_human="ready-for-human", blocker_open=True
        )
        == "blocker"
    )


def test_open_marked_delivery_pr_is_excluded() -> None:
    """P-SEL-4: an issue with an open marked delivery pull request is never
    claimed."""
    assert (
        exclusion_reason(
            _candidate(),
            ready_for_human="ready-for-human",
            blocker_open=False,
            attempt_pr_open=True,
        )
        == "attempt-pr"
    )


def test_whitespace_only_body_is_excluded() -> None:
    """P-SEL-4: a body of whitespace alone carries no task and is empty."""
    assert (
        exclusion_reason(
            _candidate(body="  \n "),
            ready_for_human="ready-for-human",
            blocker_open=False,
        )
        == "empty-body"
    )


def test_require_all_narrows_selection() -> None:
    """C-SEL-1: a candidate missing a ``require_all`` label is not claimed."""
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent",)),
            ready_for_human="ready-for-human",
            blocker_open=False,
            require_all=("surveyed",),
        )
        == "require_all"
    )
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "surveyed")),
            ready_for_human="ready-for-human",
            blocker_open=False,
            require_all=("surveyed",),
        )
        is None
    )


def test_require_any_narrows_selection() -> None:
    """C-SEL-1: a candidate carrying none of the ``require_any`` labels is not
    claimed."""
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent",)),
            ready_for_human="ready-for-human",
            blocker_open=False,
            require_any=("surveyed", "audited"),
        )
        == "require_any"
    )
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "audited")),
            ready_for_human="ready-for-human",
            blocker_open=False,
            require_any=("surveyed", "audited"),
        )
        is None
    )


def test_exclude_narrows_selection() -> None:
    """C-SEL-1: a candidate carrying an ``exclude`` label is not claimed."""
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent", "needs-design")),
            ready_for_human="ready-for-human",
            blocker_open=False,
            exclude=("needs-design",),
        )
        == "exclude"
    )
    assert (
        exclusion_reason(
            _candidate(labels=("ready-for-agent",)),
            ready_for_human="ready-for-human",
            blocker_open=False,
            exclude=("needs-design",),
        )
        is None
    )


def test_blocked_by_line_lists_referenced_issues() -> None:
    """§4: where native dependencies are unavailable, a ``Blocked by: #n``
    line at the top of the child body names the blockers."""
    assert parse_blocked_by_references("Blocked by: #12, #34\n\nDo the thing.\n") == (
        12,
        34,
    )
    assert parse_blocked_by_references("## Acceptance criteria\n- [ ] done\n") == ()


def test_selection_orders_by_created_at_then_number() -> None:
    """P-SEL-5: of two eligible issues, the older ``created_at`` is claimed
    first; ties are broken by number."""
    older = _candidate(number=9, created_at="2026-10-01T00:00:00Z")
    newer = _candidate(number=8, created_at="2026-10-02T00:00:00Z")
    assert sort_candidates([newer, older]) == [older, newer]

    tied_early = _candidate(number=8, created_at="2026-10-01T00:00:00Z")
    tied_late = _candidate(number=9, created_at="2026-10-01T00:00:00Z")
    assert sort_candidates([tied_late, tied_early]) == [tied_early, tied_late]
