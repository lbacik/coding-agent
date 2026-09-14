from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.github.issues import IssueFetchFailed, fetch_issue

TEST_BASE_URL = "https://api.github.test"


def test_fetch_issue_returns_title_and_body(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "S3.1: agent implement skeleton", "body": "## What to build\n..."},
    )

    issue = fetch_issue(client, "octocat", "sandbox", 30)

    assert issue.number == 30
    assert issue.title == "S3.1: agent implement skeleton"
    assert issue.body == "## What to build\n..."


def test_fetch_issue_treats_a_null_body_as_empty(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/31",
        json={"number": 31, "title": "no body", "body": None},
    )

    issue = fetch_issue(client, "octocat", "sandbox", 31)

    assert issue.body == ""


def test_fetch_issue_raises_on_missing_issue(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    with pytest.raises(IssueFetchFailed):
        fetch_issue(client, "octocat", "sandbox", 999)


def test_fetch_issue_raises_on_unreachable_repo(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/gone/issues/1",
        status_code=404,
        json={"message": "Not Found"},
    )

    with pytest.raises(IssueFetchFailed):
        fetch_issue(client, "octocat", "gone", 1)
