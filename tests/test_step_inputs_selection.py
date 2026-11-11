"""Step-workflow selection guards, filters and order (contract §5.2–§5.4).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5.2 (the
mandatory guards) and §5.3 (filters, drafts, ``skip-open-agent-pr``).

Row ids: INP-4, INP-8 … INP-12.
"""

from __future__ import annotations

from coding_agent.inputs.selection import (
    exclusion_reason,
    input_author_matches,
    open_agent_pr_for_issue,
    sort_inputs,
)
from coding_agent.inputs.port import TrackedInput


def _candidate(
    *,
    number: int = 7,
    input_type: str = "issue",
    is_open: bool = True,
    labels: tuple[str, ...] = ("agent:implement",),
    assignees: tuple[str, ...] = (),
    draft: bool = False,
    head_repo: str | None = None,
    author_login: str = "human",
    author_id: int | None = 7,
    body: str = "a body",
    created_at: str = "2026-10-01T00:00:00Z",
) -> TrackedInput:
    """One eligible candidate: open, labelled, unassigned, authorised."""
    assert input_type in ("issue", "pull-request")
    return TrackedInput(
        number=number,
        input_type=input_type,  # type: ignore[arg-type]
        title="Add a greeting",
        body=body,
        is_open=is_open,
        labels=labels,
        assignees=assignees,
        created_at=created_at,
        author_login=author_login,
        author_id=author_id,
        draft=draft,
        head_repo=head_repo,
    )


def _eligible_reason(candidate: TrackedInput) -> str | None:
    return exclusion_reason(
        candidate,
        selection_label="agent:implement",
        label_authorised=True,
        blocker_open=False,
        claim_in_force=False,
        target_repo="octocat/sandbox",
    )


def test_eligible_candidate_has_no_exclusion_reason() -> None:
    """§5.2: an open, labelled, unassigned input is selectable."""
    assert _eligible_reason(_candidate()) is None


def test_closed_input_is_excluded() -> None:
    """INP-9: a closed input is never claimed."""
    assert (
        _eligible_reason(_candidate(is_open=False)) == "closed"
    )


def test_missing_selection_label_is_excluded() -> None:
    """INP-9: an input without the Selection Label is never claimed."""
    assert _eligible_reason(_candidate(labels=("other",))) == "selection-label"


def test_unauthorised_label_application_is_excluded() -> None:
    """INP-6: a label the actor test refuses excludes at the read-back."""
    assert (
        exclusion_reason(
            _candidate(),
            selection_label="agent:implement",
            label_authorised=False,
            blocker_open=False,
            claim_in_force=False,
            target_repo="octocat/sandbox",
        )
        == "label-authorisation"
    )


def test_any_assignee_excludes() -> None:
    """INP-9: any assignee excludes, the Agent Identity included."""
    assert _eligible_reason(_candidate(assignees=("coding-agent",))) == "assigned"


def test_open_blocker_excludes() -> None:
    """INP-9: an open native blocker excludes."""
    assert (
        exclusion_reason(
            _candidate(),
            selection_label="agent:implement",
            label_authorised=True,
            blocker_open=True,
            claim_in_force=False,
            target_repo="octocat/sandbox",
        )
        == "blocker"
    )


def test_claim_in_force_excludes() -> None:
    """INP-5: another Workflow's Claim in force excludes."""
    assert (
        exclusion_reason(
            _candidate(),
            selection_label="agent:implement",
            label_authorised=True,
            blocker_open=False,
            claim_in_force=True,
            target_repo="octocat/sandbox",
        )
        == "claim"
    )


def test_fork_pull_request_is_never_eligible() -> None:
    """INP-8: a fork pull request is never eligible."""
    assert (
        _eligible_reason(
            _candidate(
                input_type="pull-request",
                head_repo="someone-else/sandbox",
            )
        )
        == "fork"
    )


def test_same_repository_pull_request_is_eligible() -> None:
    """INP-1: a same-repository pull request is selectable."""
    assert (
        _eligible_reason(
            _candidate(input_type="pull-request", head_repo="octocat/sandbox")
        )
        is None
    )


def test_draft_excluded_by_default() -> None:
    """INP-10: drafts are excluded by default."""
    assert (
        _eligible_reason(_candidate(input_type="pull-request", draft=True))
        == "draft"
    )


def test_draft_included_where_configured() -> None:
    """INP-10: ``drafts: include`` makes draft pull requests eligible."""
    assert (
        exclusion_reason(
            _candidate(input_type="pull-request", draft=True),
            selection_label="agent:implement",
            label_authorised=True,
            blocker_open=False,
            claim_in_force=False,
            target_repo="octocat/sandbox",
            drafts="include",
        )
        is None
    )


def test_filters_narrow_never_authorise() -> None:
    """INP-10: ``require-all``, ``require-any`` and ``exclude`` narrow."""
    base = dict(
        selection_label="agent:implement",
        label_authorised=True,
        blocker_open=False,
        claim_in_force=False,
        target_repo="octocat/sandbox",
    )
    assert (
        exclusion_reason(
            _candidate(labels=("agent:implement", "small"),
            ),
            require_all=("agent:implement", "small"),
            **base,  # type: ignore[arg-type]
        )
        is None
    )
    assert (
        exclusion_reason(
            _candidate(labels=("agent:implement",)),
            require_all=("agent:implement", "small"),
            **base,  # type: ignore[arg-type]
        )
        == "require-all"
    )
    assert (
        exclusion_reason(
            _candidate(labels=("agent:implement",)),
            require_any=("small", "large"),
            **base,  # type: ignore[arg-type]
        )
        == "require-any"
    )
    assert (
        exclusion_reason(
            _candidate(labels=("agent:implement", "wontfix")),
            exclude=("wontfix",),
            **base,  # type: ignore[arg-type]
        )
        == "exclude"
    )


def test_agent_pr_excludes_where_configured() -> None:
    """INP-4: ``skip-open-agent-pr`` skips the issue while its agent PR is open."""
    assert (
        exclusion_reason(
            _candidate(),
            selection_label="agent:implement",
            label_authorised=True,
            blocker_open=False,
            claim_in_force=False,
            target_repo="octocat/sandbox",
            agent_pr_open=True,
        )
        == "agent-pr"
    )


def test_open_agent_pr_lookup_needs_our_marker_and_author() -> None:
    """INP-11: only our open PR bearing that issue's marker excludes."""

    def _pr(
        *,
        number: int = 11,
        body: str = "",
        author_login: str = "coding-agent",
        author_id: int | None = 1001,
        is_open: bool = True,
    ) -> TrackedInput:
        return _candidate(
            number=number,
            input_type="pull-request",
            body=body,
            author_login=author_login,
            author_id=author_id,
            is_open=is_open,
        )

    own = _pr(body="implements <!-- coding-agent attempt=implement#42/1 -->")
    assert (
        open_agent_pr_for_issue(
            [own], 42, agent_login="coding-agent", agent_id=1001
        )
        is own
    )
    foreign = _pr(body="quotes <!-- coding-agent attempt=implement#42/1 -->")
    foreign = TrackedInput(
        number=foreign.number,
        input_type=foreign.input_type,
        title=foreign.title,
        body=foreign.body,
        is_open=foreign.is_open,
        labels=foreign.labels,
        assignees=foreign.assignees,
        created_at=foreign.created_at,
        author_login="human",
        author_id=424242,
    )
    assert (
        open_agent_pr_for_issue(
            [foreign], 42, agent_login="coding-agent", agent_id=1001
        )
        is None
    )
    other_issue = _pr(body="<!-- coding-agent attempt=implement#43/1 -->")
    assert (
        open_agent_pr_for_issue(
            [other_issue], 42, agent_login="coding-agent", agent_id=1001
        )
        is None
    )
    closed = _pr(body="<!-- coding-agent attempt=implement#42/1 -->", is_open=False)
    assert (
        open_agent_pr_for_issue(
            [closed], 42, agent_login="coding-agent", agent_id=1001
        )
        is None
    )


def test_author_matches_by_id_where_known() -> None:
    """§14: the numeric id decides where it is known."""
    candidate = _candidate(author_login="renamed", author_id=1001)
    assert input_author_matches(candidate, agent_login="coding-agent", agent_id=1001)
    other = _candidate(author_login="human", author_id=424242)
    assert not input_author_matches(other, agent_login="coding-agent", agent_id=1001)


def test_author_matches_by_login_where_id_unknown() -> None:
    """§14: the login decides where the numeric id is unknown."""
    candidate = _candidate(author_login="coding-agent", author_id=None)
    assert input_author_matches(candidate, agent_login="coding-agent", agent_id=None)
    other = _candidate(author_login="human", author_id=None)
    assert not input_author_matches(other, agent_login="coding-agent", agent_id=None)


def test_order_is_created_at_then_number() -> None:
    """INP-12: ordered ``(created_at, number)`` ascending."""
    late = _candidate(number=9, created_at="2026-10-03T00:00:00Z")
    early = _candidate(number=8, created_at="2026-10-01T00:00:00Z")
    same_time_high = _candidate(number=12, created_at="2026-10-02T00:00:00Z")
    same_time_low = _candidate(number=3, created_at="2026-10-02T00:00:00Z")
    assert sort_inputs([late, same_time_high, early, same_time_low]) == [
        early,
        same_time_low,
        same_time_high,
        late,
    ]
