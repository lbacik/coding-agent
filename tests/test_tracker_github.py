"""Edge cases of the GitHub adapter of ``TrackerPort``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write, through this port), §13 (the write sequence; the
Selection Label removal is naturally idempotent — a ``404`` means the label
is already absent) and §14 (the bytes sent are the ones already recorded in
``outcomes``; a write that fails raises so the recorded outcome still
stands), and §18 (the two structural abstentions: no operation applies the
Selection Label and none edits the issue body).

MVP row ids: C-TRK-1, C-OUT-1.
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
    ):
        assert callable(getattr(GitHubTracker, name)), name
    assert not hasattr(GitHubTracker, "apply_selection_label")
    assert not hasattr(GitHubTracker, "edit_issue_body")
    assert not hasattr(GitHubTracker, "update_issue")
