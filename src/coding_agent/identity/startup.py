from __future__ import annotations

from dataclasses import dataclass

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.token import (
    assert_fine_grained_token,
    assert_no_oauth_scopes,
)

DEFAULT_RATE_LIMIT_MINIMUM = 100


class StartupCheckFailed(Exception):
    """A non-mutating startup check failed; the Worker must not open an Attempt."""


@dataclass(frozen=True)
class Identity:
    account_id: int
    login: str


@dataclass(frozen=True)
class StartupCheckResult:
    identity: Identity
    push_allowed: bool
    rate_limit_remaining: int
    rate_limit_limit: int


def resolve_identity(client: GitHubClient) -> tuple[Identity, requests.Response]:
    """`GET /user`, applying the live token assertion to the response.

    The token's expiration is not checked: whether a token expires, and
    when, is the operator's concern, not the agent's (contract §18).
    """
    response = client.get("/user")
    if response.status_code != 200:
        raise StartupCheckFailed(f"GET /user returned {response.status_code}: {response.text}")

    assert_no_oauth_scopes(response.headers)

    body = response.json()
    return Identity(account_id=body["id"], login=body["login"]), response


def check_push_permission(client: GitHubClient, owner: str, repo: str) -> bool:
    """`GET /repos/{owner}/{repo}` for `permissions.push`."""
    response = client.get(f"/repos/{owner}/{repo}")
    if response.status_code != 200:
        raise StartupCheckFailed(
            f"GET /repos/{owner}/{repo} returned {response.status_code}: {response.text}"
        )
    body = response.json()
    return bool(body.get("permissions", {}).get("push", False))


def check_rate_limit_headroom(
    response: requests.Response, *, minimum: int = DEFAULT_RATE_LIMIT_MINIMUM
) -> tuple[int, int]:
    """Rate-limit headroom read from any response's standard headers."""
    remaining = response.headers.get("x-ratelimit-remaining")
    limit = response.headers.get("x-ratelimit-limit")
    if remaining is None or limit is None:
        raise StartupCheckFailed("response carried no rate-limit headers")
    remaining_i, limit_i = int(remaining), int(limit)
    if remaining_i < minimum:
        raise StartupCheckFailed(
            f"rate-limit headroom {remaining_i}/{limit_i} is below the minimum of {minimum}"
        )
    return remaining_i, limit_i


def run_startup_checks(
    client: GitHubClient,
    token: str,
    owner: str,
    repo: str,
    *,
    rate_limit_minimum: int = DEFAULT_RATE_LIMIT_MINIMUM,
) -> StartupCheckResult:
    """Every non-mutating check contract §10 "Worker startup" requires.

    No write is performed. A capability the token turns out to lack is not
    caught here — it surfaces as a permission refusal when it bites (§6).
    """
    assert_fine_grained_token(token)

    identity, user_response = resolve_identity(client)
    remaining, limit = check_rate_limit_headroom(user_response, minimum=rate_limit_minimum)
    push_allowed = check_push_permission(client, owner, repo)

    return StartupCheckResult(
        identity=identity,
        push_allowed=push_allowed,
        rate_limit_remaining=remaining,
        rate_limit_limit=limit,
    )
