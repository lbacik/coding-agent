from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.token import (
    DEFAULT_EXPIRATION_THRESHOLD,
    TokenExpiration,
    assert_fine_grained_token,
    assert_no_oauth_scopes,
    assert_token_expiration,
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
    token_expiration: TokenExpiration
    push_allowed: bool
    rate_limit_remaining: int
    rate_limit_limit: int


def resolve_identity(
    client: GitHubClient,
    *,
    expiration_threshold: timedelta = DEFAULT_EXPIRATION_THRESHOLD,
) -> tuple[Identity, TokenExpiration, requests.Response]:
    """`GET /user`, applying both live token assertions to the response."""
    response = client.get("/user")
    if response.status_code != 200:
        raise StartupCheckFailed(f"GET /user returned {response.status_code}: {response.text}")

    assert_no_oauth_scopes(response.headers)
    expiration = assert_token_expiration(response.headers, threshold=expiration_threshold)

    body = response.json()
    return Identity(account_id=body["id"], login=body["login"]), expiration, response


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
    expiration_threshold: timedelta = DEFAULT_EXPIRATION_THRESHOLD,
    rate_limit_minimum: int = DEFAULT_RATE_LIMIT_MINIMUM,
) -> StartupCheckResult:
    """Every non-mutating check contract §10 "Worker startup" requires.

    No write is performed. A capability the token turns out to lack is not
    caught here — it surfaces as a permission refusal when it bites (§6).
    """
    assert_fine_grained_token(token)

    identity, expiration, user_response = resolve_identity(
        client, expiration_threshold=expiration_threshold
    )
    remaining, limit = check_rate_limit_headroom(user_response, minimum=rate_limit_minimum)
    push_allowed = check_push_permission(client, owner, repo)

    return StartupCheckResult(
        identity=identity,
        token_expiration=expiration,
        push_allowed=push_allowed,
        rate_limit_remaining=remaining,
        rate_limit_limit=limit,
    )
