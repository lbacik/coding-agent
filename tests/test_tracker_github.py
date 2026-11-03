"""Edge cases of the GitHub adapter of ``TrackerPort``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write, through this port), §13 (the write sequence; the
Selection Label removal is naturally idempotent — a ``404`` means the label
is already absent) and §14 (the bytes sent are the ones already recorded in
``outcomes``; a write that fails raises so the recorded outcome still
stands), and §18 (the two structural abstentions: no operation applies the
Selection Label and none edits the issue body).

MVP row ids: C-TRK-1, C-OUT-1, P-DEL-1, P-DEL-10, L3-DEL-21.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.github.issues import IssueFetchFailed
from coding_agent.tracker import github as github_adapter
from coding_agent.tracker.github import GitHubTracker, GitHubTrackerError

TEST_BASE_URL = "https://api.github.test"


def _tracker() -> GitHubTracker:
    return GitHubTracker(
        owner="octocat",
        repo="sandbox",
        client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
        agent_login="octocat",
    )


def test_fetch_issue_returns_the_number_title_and_body(requests_mock: Any) -> None:
    """C-TRK-1: ``fetch_issue`` reads the Target Issue (HTTP ``GET``)."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": "a body"},
    )

    issue = _tracker().fetch_issue(30)

    assert issue.number == 30
    assert issue.title == "a title"
    assert issue.body == "a body"


def test_fetch_issue_failure_raises(requests_mock: Any) -> None:
    """C-TRK-1: an unreadable Target Issue fails loudly, never silently."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    with pytest.raises(IssueFetchFailed):
        _tracker().fetch_issue(999)


def test_post_terminal_comment_sends_the_recorded_bytes(requests_mock: Any) -> None:
    """§14: the comment carries the already-recorded body verbatim."""
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/comments",
        json={"id": 101},
        status_code=201,
    )

    _tracker().post_terminal_comment(30, "recorded body")

    assert requests_mock.request_history[-1].json() == {"body": "recorded body"}


def test_remove_selection_label_treats_an_absent_label_as_success(
    requests_mock: Any,
) -> None:
    """§13: a ``404`` means the label is already absent — a successful no-op."""
    requests_mock.delete(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/labels/ready-for-agent",
        status_code=404,
        json={"message": "Not Found"},
    )

    _tracker().remove_selection_label(30)

    assert len(requests_mock.request_history) == 1


def test_release_assignment_removes_only_the_agent_login(
    requests_mock: Any,
) -> None:
    """§5: the release names the Agent Identity's login, never another assignee."""
    requests_mock.delete(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/assignees",
        json={"number": 30},
    )

    _tracker().release_assignment(30)

    assert requests_mock.request_history[-1].json() == {"assignees": ["octocat"]}


@pytest.mark.parametrize(
    ("operation", "method", "path", "status"),
    [
        (
            "post_terminal_comment",
            "POST",
            "/repos/octocat/sandbox/issues/30/comments",
            422,
        ),
        (
            "remove_selection_label",
            "DELETE",
            "/repos/octocat/sandbox/issues/30/labels/ready-for-agent",
            500,
        ),
        (
            "add_ready_for_human",
            "POST",
            "/repos/octocat/sandbox/issues/30/labels",
            422,
        ),
        (
            "release_assignment",
            "DELETE",
            "/repos/octocat/sandbox/issues/30/assignees",
            403,
        ),
    ],
)
def test_terminal_write_failure_raises(
    operation: str, method: str, path: str, status: int, requests_mock: Any
) -> None:
    """§14: a write that fails raises; the recorded outcome still stands."""
    register = getattr(requests_mock, method.lower())
    register(f"{TEST_BASE_URL}{path}", status_code=status, json={"message": "boom"})

    tracker = _tracker()
    with pytest.raises(GitHubTrackerError):
        if operation == "post_terminal_comment":
            tracker.post_terminal_comment(30, "recorded body")
        elif operation == "remove_selection_label":
            tracker.remove_selection_label(30)
        elif operation == "add_ready_for_human":
            tracker.add_ready_for_human(30)
        elif operation == "release_assignment":
            tracker.release_assignment(30)
        else:  # pragma: no cover - the params above are exhaustive
            raise AssertionError(f"unknown tracker operation {operation!r}")


def _pr_item(
    *,
    number: int,
    head: str = "agent/7/1-slug",
    body: str = "",
    state: str = "open",
    login: str = "octocat",
    user_id: int = 4242,
) -> dict[str, Any]:
    """One pull-request list entry as the GitHub API returns it (§13)."""
    return {
        "number": number,
        "html_url": f"{TEST_BASE_URL}/octocat/sandbox/pull/{number}",
        "title": "a title",
        "state": state,
        "body": body,
        "head": {"ref": head},
        "base": {"ref": "main"},
        "user": {"id": user_id, "login": login},
        "draft": False,
    }


def _tracker_with_id() -> GitHubTracker:
    """The adapter bound to the Agent Identity's numeric id (§13, §18)."""
    return GitHubTracker(
        owner="octocat",
        repo="sandbox",
        client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
        agent_login="octocat",
        agent_id=4242,
    )


def test_find_pull_request_returns_the_marked_open_pull_request(
    requests_mock: Any,
) -> None:
    """§13: the lookup filters by head branch, marker and author id.

    A marked pull request on another branch, an unmarked one on ours
    (L3-DEL-21) and a marked one by another account are never ours; where
    both states match, the open pull request is the receipt.
    """
    marker = "<!-- coding-agent: attempt #7/1 kind=delivery -->"
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        json=[
            _pr_item(number=1, head="agent/9/9-other", body=f"bytes\n{marker}\n"),
            _pr_item(number=2, body="a human's pull request without our marker"),
            _pr_item(
                number=5,
                body=f"borrowed bytes\n{marker}\n",
                login="mallory",
                user_id=9999,
            ),
            _pr_item(number=3, body=f"Closes #7\n{marker}\n", state="closed"),
            _pr_item(number=4, body=f"Closes #7\n{marker}\n", state="open"),
        ],
    )

    found = _tracker_with_id().find_pull_request("agent/7/1-slug", marker)

    assert found is not None
    assert (found.number, found.state) == (4, "open")
    assert found.url == f"{TEST_BASE_URL}/octocat/sandbox/pull/4"
    history = requests_mock.request_history
    assert len(history) == 1
    assert history[0].qs == {
        "head": ["octocat:agent/7/1-slug"],
        "state": ["all"],
        "per_page": ["100"],
    }


def test_find_pull_request_ignores_an_unmarked_branch_pull_request(
    requests_mock: Any,
) -> None:
    """L3-DEL-21: the marker rule reads in one direction only."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        json=[_pr_item(number=7, body="a human's pull request")],
    )

    assert (
        _tracker_with_id().find_pull_request(
            "agent/7/1-slug", "<!-- coding-agent: attempt #7/1 kind=delivery -->"
        )
        is None
    )


def test_open_pull_request_sends_no_draft_field(requests_mock: Any) -> None:
    """P-DEL-10: the open sends no ``draft`` field, so no draft can result."""
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        json={
            "number": 12,
            "html_url": f"{TEST_BASE_URL}/octocat/sandbox/pull/12",
            "title": "a title",
            "state": "open",
            "body": "recorded body",
            "head": {"ref": "agent/7/1-slug"},
            "base": {"ref": "main"},
            "user": {"id": 4242, "login": "octocat"},
            "draft": False,
        },
        status_code=201,
    )

    created = _tracker().open_pull_request(
        title="a title",
        head_branch="agent/7/1-slug",
        base_branch="main",
        body="recorded body",
    )

    assert requests_mock.request_history[-1].json() == {
        "title": "a title",
        "head": "agent/7/1-slug",
        "base": "main",
        "body": "recorded body",
    }
    assert created.number == 12
    assert created.draft is False


def test_pull_request_lookup_failure_raises(requests_mock: Any) -> None:
    """§14: a failed pull-request lookup raises; the recorded outcome stands."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        status_code=500,
        json={"message": "boom"},
    )

    with pytest.raises(GitHubTrackerError):
        _tracker().find_pull_request("agent/7/1-slug", "marker")


def test_pull_request_open_failure_raises(requests_mock: Any) -> None:
    """§14: a failed pull-request open raises; the recorded outcome stands."""
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        status_code=422,
        json={"message": "boom"},
    )

    with pytest.raises(GitHubTrackerError):
        _tracker().open_pull_request(
            title="a title",
            head_branch="agent/7/1-slug",
            base_branch="main",
            body="recorded body",
        )


def test_adapter_exposes_no_selection_label_apply_or_issue_body_edit() -> None:
    """§18: the adapter cannot apply the Selection Label or edit the issue body."""
    source = inspect.getsource(github_adapter)
    assert "apply_selection_label" not in source
    assert "edit_issue" not in source
    assert "update_issue" not in source
    for name in (
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
        "find_pull_request",
        "open_pull_request",
        "list_candidates",
        "fetch_candidate",
        "has_open_blocker",
        "has_open_attempt_pull_request",
        "assign_issue",
        "fetch_base_revision",
    ):
        assert callable(getattr(GitHubTracker, name)), name
    assert not hasattr(GitHubTracker, "apply_selection_label")
    assert not hasattr(GitHubTracker, "edit_issue_body")
    assert not hasattr(GitHubTracker, "update_issue")


def _selection_issue_payload(number: int, **overrides: object) -> dict[str, Any]:
    """One issue as the API reports it, with every §4 guard fact present."""
    payload: dict[str, Any] = {
        "number": number,
        "title": "a title",
        "body": "## Acceptance criteria\n- [ ] probe\n",
        "state": "open",
        "assignees": [],
        "labels": [{"name": "ready-for-agent"}],
        "created_at": "2026-10-01T00:00:00Z",
        "issue_dependencies_summary": {"blocked_by": 0, "blocking": 0},
    }
    payload.update(overrides)
    return payload


def test_has_open_blocker_reads_the_native_summary(requests_mock: Any) -> None:
    """§4: ``blocked_by`` counts open blockers only — no further read needed."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json=_selection_issue_payload(
            30, issue_dependencies_summary={"blocked_by": 2, "blocking": 0}
        ),
    )

    assert _tracker().has_open_blocker(30) is True

    assert len(requests_mock.request_history) == 1


def test_has_open_blocker_false_without_blockers(requests_mock: Any) -> None:
    """§4: an unblocked issue reports no open blocker."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json=_selection_issue_payload(30),
    )

    assert _tracker().has_open_blocker(30) is False


def test_has_open_blocker_falls_back_to_the_blocked_by_line(
    requests_mock: Any,
) -> None:
    """§4: without native dependencies, an open issue in ``Blocked by`` blocks."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json=_selection_issue_payload(30, body="Blocked by: #31\n\nSome body."),
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/31",
        json=_selection_issue_payload(31, state="open"),
    )

    assert _tracker().has_open_blocker(30) is True


def test_has_open_blocker_false_when_the_referenced_issue_closed(
    requests_mock: Any,
) -> None:
    """§4: a closed issue in ``Blocked by`` no longer blocks."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json=_selection_issue_payload(30, body="Blocked by: #31"),
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/31",
        json=_selection_issue_payload(31, state="closed"),
    )

    assert _tracker().has_open_blocker(30) is False


def test_has_open_blocker_ignores_a_dangling_reference(
    requests_mock: Any,
) -> None:
    """§4: a ``Blocked by`` reference to a missing issue is not a blocker."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json=_selection_issue_payload(30, body="Blocked by: #999"),
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    assert _tracker().has_open_blocker(30) is False


def test_has_open_attempt_pull_request_scans_open_pulls(
    requests_mock: Any,
) -> None:
    """§4: an open pull request carrying an Attempt Marker for the issue blocks."""
    marker = "<!-- coding-agent attempt=#30/1 kind=delivery -->"
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        json=[
            _pr_item(number=11, head="agent/31/1-slug"),
            _pr_item(number=12, head="agent/30/1-slug", body=marker),
        ],
    )

    assert _tracker().has_open_attempt_pull_request(30) is True
    assert _tracker().has_open_attempt_pull_request(31) is False

    for request in requests_mock.request_history:
        assert request.qs["state"] == ["open"]


def test_has_open_attempt_pull_request_paginates(requests_mock: Any) -> None:
    """§4: the pull-request scan reads every page before reporting none."""
    marker = "<!-- coding-agent attempt=#30/2 kind=delivery -->"
    page = [_pr_item(number=index, head=f"agent/99/{index}-slug") for index in range(100)]
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        [
            {"json": page, "status_code": 200},
            {
                "json": [_pr_item(number=200, head="agent/30/2-slug", body=marker)],
                "status_code": 200,
            },
        ],
    )

    assert _tracker().has_open_attempt_pull_request(30) is True

    assert len(requests_mock.request_history) == 2


def test_assign_issue_posts_only_the_agent_login(requests_mock: Any) -> None:
    """P-CLM-1: the Claim's one write assigns the Agent Identity — nothing else."""
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/assignees",
        json={"number": 30},
        status_code=201,
    )

    _tracker().assign_issue(30)

    history = requests_mock.request_history
    assert len(history) == 1
    assert history[0].json() == {"assignees": ["octocat"]}


def test_assign_issue_failure_raises(requests_mock: Any) -> None:
    """P-CLM-2's honest side: a refused Claim write fails loudly, never silently."""
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/assignees",
        status_code=403,
        json={"message": "Forbidden"},
    )

    with pytest.raises(GitHubTrackerError):
        _tracker().assign_issue(30)


def test_fetch_base_revision_resolves_the_branch_head(
    requests_mock: Any,
) -> None:
    """§5: the Base Revision is the configured base branch's head sha."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/branches/main",
        json={"name": "main", "commit": {"sha": "a" * 40}},
    )

    assert _tracker().fetch_base_revision("main") == "a" * 40


def test_fetch_base_revision_failure_raises(requests_mock: Any) -> None:
    """§5: an unresolvable base branch fails loudly, never silently."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/branches/nope",
        status_code=404,
        json={"message": "Not Found"},
    )

    with pytest.raises(GitHubTrackerError):
        _tracker().fetch_base_revision("nope")
