"""The temporary ``local`` tracker adapter: same ``TrackerPort``, no writes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write), §5 (the Claim), §14 (the Terminal Outcome is
recorded before the terminal writes) and §18 (the Worker never edits the
Target Issue's body and never applies the Selection Label — structurally,
because this port cannot express them).

MVP row ids: L3-SEL-11, L3-CLR-19.
"""

from __future__ import annotations

from typing import Any

from coding_agent.github.client import GitHubClient
from coding_agent.tracker.local import LocalTracker
from coding_agent.tracker.port import TrackedIssue, TrackerPort

TEST_BASE_URL = "https://api.github.test"


def _tracker() -> LocalTracker:
    return LocalTracker(
        owner="octocat",
        repo="sandbox",
        client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
    )


def _mock_issue(requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": "a body"},
    )


def test_local_tracker_fetches_the_issue_with_a_read_only_request(
    requests_mock: Any,
) -> None:
    _mock_issue(requests_mock)

    issue = _tracker().fetch_issue(30)

    assert issue == TrackedIssue(number=30, title="a title", body="a body")
    assert {request.method for request in requests_mock.request_history} == {"GET"}


def test_local_tracker_terminal_writes_perform_no_github_write(
    requests_mock: Any,
) -> None:
    _mock_issue(requests_mock)
    tracker = _tracker()
    tracker.fetch_issue(30)

    tracker.post_terminal_comment(30, "Attempt #30/1: delivered (verified-completion)")
    tracker.remove_selection_label(30)
    tracker.add_ready_for_human(30)
    tracker.release_assignment(30)
    assert tracker.find_pull_request("agent/30/1-slug", "<!-- marker -->") is None
    opened = tracker.open_pull_request(
        title="a title",
        head_branch="agent/30/1-slug",
        base_branch="main",
        body="a body",
    )
    assert opened.draft is False

    assert {request.method for request in requests_mock.request_history} == {"GET"}


def test_local_tracker_satisfies_the_tracker_port(requests_mock: Any) -> None:
    _mock_issue(requests_mock)
    tracker = _tracker()
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
        assert callable(getattr(tracker, name)), name
    members = {name for name in dir(TrackerPort) if not name.startswith("_")}
    assert members == {
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
    }
    assert "apply_selection_label" not in members
