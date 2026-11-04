"""The GitHub adapter classifies every response by the §13 table.

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 ("Rate limits and
refusals": a 401 is a Credential Failure; a 403 with
``x-ratelimit-remaining > 0`` is a permission refusal with no retry; primary
and secondary rate limits are retried in place within the bound).

Found by the first real delivery (#154): ``request_with_retry`` had no
production caller, so a revoked token surfaced from ``list_candidates`` as an
unclassified ``GitHubTrackerError`` and the Worker died on a traceback rather
than halting on a Credential Failure.

MVP row ids: P-WRK-1, L3-ERR-3, L3-ERR-4, L3-ERR-5.
"""

from __future__ import annotations

from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.github.errors import CredentialFailure, PermissionRefusal
from coding_agent.tracker.github import GitHubTracker

TEST_BASE_URL = "https://api.github.test"
ISSUES = f"{TEST_BASE_URL}/repos/octocat/sandbox/issues"


def _tracker(slept: list[float] | None = None) -> GitHubTracker:
    record = slept if slept is not None else []
    return GitHubTracker(
        owner="octocat",
        repo="sandbox",
        client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
        agent_login="octocat",
        sleep=record.append,
        now=lambda: 1_000.0,
    )


BAD_CREDENTIALS = {"status_code": 401, "json": {"message": "Bad credentials"}}


def test_a_revoked_token_during_selection_is_a_credential_failure(
    requests_mock: Any,
) -> None:
    """P-WRK-1: the idle Worker's sweep sees the 401 as a Credential Failure."""
    requests_mock.get(ISSUES, **BAD_CREDENTIALS)

    with pytest.raises(CredentialFailure):
        _tracker().list_candidates("ready-for-agent")


def test_a_revoked_token_reading_the_target_issue_is_a_credential_failure(
    requests_mock: Any,
) -> None:
    """P-WRK-1: inside an Attempt, the graph classifies it as credential-failure."""
    requests_mock.get(f"{ISSUES}/30", **BAD_CREDENTIALS)

    with pytest.raises(CredentialFailure):
        _tracker().fetch_issue(30)


def test_a_revoked_token_on_a_terminal_write_is_a_credential_failure(
    requests_mock: Any,
) -> None:
    """P-WRK-1: a write gets the same classification as a read."""
    requests_mock.post(f"{ISSUES}/30/comments", **BAD_CREDENTIALS)

    with pytest.raises(CredentialFailure):
        _tracker().post_terminal_comment(30, "recorded body")


def test_a_permission_shaped_403_is_refused_without_a_retry(
    requests_mock: Any,
) -> None:
    """L3-ERR-3: 403 with remaining > 0 is a permission refusal, sent once."""
    requests_mock.post(
        f"{ISSUES}/30/assignees",
        status_code=403,
        headers={"x-ratelimit-remaining": "4000"},
        json={"message": "Resource not accessible"},
    )

    with pytest.raises(PermissionRefusal):
        _tracker().assign_issue(30)
    assert requests_mock.call_count == 1


def test_a_secondary_rate_limit_is_retried_in_place(requests_mock: Any) -> None:
    """L3-ERR-5: ``Retry-After`` is honoured, then the write succeeds."""
    requests_mock.post(
        f"{ISSUES}/30/assignees",
        [
            {"status_code": 403, "headers": {"Retry-After": "2"}, "json": {}},
            {"status_code": 201, "json": {"assignees": [{"login": "octocat"}]}},
        ],
    )
    slept: list[float] = []

    _tracker(slept).assign_issue(30)

    assert slept == [2.0]
    assert requests_mock.call_count == 2


def test_a_primary_rate_limit_waits_until_the_reset(requests_mock: Any) -> None:
    """L3-ERR-4: remaining 0 waits to ``x-ratelimit-reset``, then retries."""
    requests_mock.get(
        ISSUES,
        [
            {
                "status_code": 429,
                "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"},
                "json": {},
            },
            {"status_code": 200, "json": []},
        ],
    )
    slept: list[float] = []

    assert _tracker(slept).list_candidates("ready-for-agent") == []
    assert slept == [30.0]


def test_an_absent_label_is_still_the_adapters_own_404(requests_mock: Any) -> None:
    """§13: the adapter keeps reading its own statuses (404 = already absent)."""
    requests_mock.delete(
        f"{ISSUES}/30/labels/ready-for-agent",
        status_code=404,
        json={"message": "Label does not exist"},
    )

    _tracker().remove_selection_label(30)
