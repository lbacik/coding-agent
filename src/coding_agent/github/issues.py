from __future__ import annotations

from dataclasses import dataclass

from coding_agent.github.client import GitHubClient


class IssueFetchFailed(Exception):
    """The Target Issue could not be fetched."""


@dataclass(frozen=True)
class TargetIssue:
    number: int
    title: str
    body: str


def fetch_issue(client: GitHubClient, owner: str, repo: str, number: int) -> TargetIssue:
    """`GET /repos/{owner}/{repo}/issues/{number}`, read for its title and body."""
    response = client.get(f"/repos/{owner}/{repo}/issues/{number}")
    if response.status_code != 200:
        raise IssueFetchFailed(
            f"GET /repos/{owner}/{repo}/issues/{number} returned "
            f"{response.status_code}: {response.text}"
        )
    body = response.json()
    return TargetIssue(number=number, title=body["title"], body=body.get("body") or "")
