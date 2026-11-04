"""Reading the Target Issue from GitHub.

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 ("Rate limits and
refusals": the read is classified by status and header values -- a 401
confirmed by the identity probe is a Credential Failure, an unconfirmed 401
is infrastructure-class ``github`` (C-ERR-2), a permission-shaped 403 a
permission refusal, rate limits are retried in place within the bound).

MVP row ids: C-TRK-1, P-WRK-1, L3-ERR-3, L3-ERR-4, L3-ERR-5, L3-ERR-6,
C-ERR-2.
"""

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
    confirmed by the identity probe raises `CredentialFailure`, an
    unconfirmed 401 is infrastructure-class `github` (C-ERR-2), a
    permission-shaped 403 raises `PermissionRefusal`, and rate limits are
    retried in place; any other failure is `IssueFetchFailed`.
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
