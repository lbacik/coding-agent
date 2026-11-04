from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from coding_agent.github.client import GitHubClient
from coding_agent.github.errors import request_with_retry


class IssueFetchFailed(Exception):
    """The Target Issue could not be fetched."""


@dataclass(frozen=True)
class TargetIssue:
    number: int
    title: str
    body: str


def fetch_issue(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> TargetIssue:
    """`GET /repos/{owner}/{repo}/issues/{number}`, read for its title and body.

    Classified by the contract's §13 table (`request_with_retry`): a 401
    raises `CredentialFailure`, a permission-shaped 403 `PermissionRefusal`,
    and rate limits are retried in place; any other failure is
    `IssueFetchFailed`.
    """
    response = request_with_retry(
        client,
        "GET",
        f"/repos/{owner}/{repo}/issues/{number}",
        sleep=sleep,
        now=now,
        raise_other=False,
    )
    if response.status_code != 200:
        raise IssueFetchFailed(
            f"GET /repos/{owner}/{repo}/issues/{number} returned "
            f"{response.status_code}: {response.text}"
        )
    body = response.json()
    return TargetIssue(number=number, title=body["title"], body=body.get("body") or "")
