from typing import Any

import pytest
import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import (
    StartupCheckFailed,
    check_push_permission,
    check_rate_limit_headroom,
    run_startup_checks,
)
from coding_agent.identity.token import TokenRejected

TEST_BASE_URL = "https://api.github.test"


def _user_headers(**overrides: str) -> dict[str, str]:
    headers = {
        "github-authentication-token-expiration": "2027-01-01 00:00:00 UTC",
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-limit": "5000",
    }
    headers.update(overrides)
    return headers


def test_run_startup_checks_happy_path(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        json={"id": 12345, "login": "octocat"},
        headers=_user_headers(),
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox",
        json={"permissions": {"push": True}},
    )

    result = run_startup_checks(client, "github_pat_abc", "octocat", "sandbox")

    assert result.identity.account_id == 12345
    assert result.identity.login == "octocat"
    assert result.push_allowed is True
    assert result.rate_limit_remaining == 4999
    assert result.rate_limit_limit == 5000


def test_run_startup_checks_refuses_classic_token(client: GitHubClient, requests_mock: Any) -> None:
    with pytest.raises(TokenRejected):
        run_startup_checks(client, "ghp_classic", "octocat", "sandbox")


def test_run_startup_checks_refuses_oauth_scopes_header(
    client: GitHubClient, requests_mock: Any
) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        json={"id": 12345, "login": "octocat"},
        headers=_user_headers(**{"X-OAuth-Scopes": "repo"}),
    )

    with pytest.raises(TokenRejected):
        run_startup_checks(client, "github_pat_abc", "octocat", "sandbox")


def test_run_startup_checks_refuses_missing_expiration_header(
    client: GitHubClient, requests_mock: Any
) -> None:
    headers = _user_headers()
    del headers["github-authentication-token-expiration"]
    requests_mock.get(
        f"{TEST_BASE_URL}/user", json={"id": 12345, "login": "octocat"}, headers=headers
    )

    with pytest.raises(TokenRejected):
        run_startup_checks(client, "github_pat_abc", "octocat", "sandbox")


def test_run_startup_checks_refuses_401(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(f"{TEST_BASE_URL}/user", status_code=401, json={"message": "Bad credentials"})

    with pytest.raises(StartupCheckFailed):
        run_startup_checks(client, "github_pat_abc", "octocat", "sandbox")


def test_check_push_permission_false(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox", json={"permissions": {"push": False}}
    )
    assert check_push_permission(client, "octocat", "sandbox") is False


def test_check_rate_limit_headroom_below_minimum() -> None:
    response = requests.Response()
    response.headers["x-ratelimit-remaining"] = "5"
    response.headers["x-ratelimit-limit"] = "5000"

    with pytest.raises(StartupCheckFailed):
        check_rate_limit_headroom(response, minimum=100)


def test_check_rate_limit_headroom_missing_headers() -> None:
    response = requests.Response()

    with pytest.raises(StartupCheckFailed):
        check_rate_limit_headroom(response)
