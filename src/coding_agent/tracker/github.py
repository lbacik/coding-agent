"""The GitHub adapter of ``TrackerPort``: the terminal writes reach GitHub.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write, through this port), §5 (the Claim is an assignment
read back; the Worker removes only an assignment the ledger records as
``assigned_by_us`` — the node consults the ledger before calling
``release_assignment``, so this adapter only ever removes the Agent Identity's
own login), §13 (the write sequence; the rendered bodies are recorded in
``outcomes`` before this adapter sends them, so a retried write sends the
same bytes) and §14 (the terminal writes in C-OUT-1 order: the comment, the
Selection Label removal, ``ready-for-human``, then the assignment removal),
and §18 (the two structural abstentions: there is no operation that applies
the Selection Label and none that edits the issue body — the port cannot
express them, so this adapter cannot perform them either).

MVP row ids: C-TRK-1, C-OUT-1.

M6 scope: this adapter implements exactly the port as it stands —
``fetch_issue`` plus the four terminal writes. Selection, the Claim and the
pull request arrive with their own tickets and extend the suite then.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from coding_agent.github.client import GitHubClient
from coding_agent.github.issues import fetch_issue
from coding_agent.tracker.port import TrackedIssue

DEFAULT_SELECTION_LABEL = "ready-for-agent"
"""The Selection Label removed after the terminal comment (§4, §14).

The parity Workflow's selector label; ``agent implement --config --workflow``
pins the configured one instead.
"""

DEFAULT_READY_FOR_HUMAN = "ready-for-human"
"""The lifecycle label added on every outcome without a pull request (§14)."""


class GitHubTrackerError(Exception):
    """A GitHub terminal write failed; the recorded outcome still stands."""


@dataclass(frozen=True)
class GitHubTracker:
    """A ``TrackerPort`` that performs the terminal writes on GitHub.

    ``selection_label`` is the Workflow's Selection Label, removed after the
    terminal comment; ``ready_for_human`` is the lifecycle label added on
    every outcome without a pull request (contract §2.1: only its name is
    configurable). ``agent_login`` is the Agent Identity's login, the only
    assignee ``release_assignment`` ever removes — never another assignee.
    """

    owner: str
    repo: str
    client: GitHubClient
    agent_login: str
    selection_label: str = DEFAULT_SELECTION_LABEL
    ready_for_human: str = DEFAULT_READY_FOR_HUMAN

    def _issue_path(self, issue_number: int) -> str:
        return f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        """Read the Target Issue's number, title and body (HTTP ``GET``)."""
        fetched = fetch_issue(self.client, self.owner, self.repo, issue_number)
        return TrackedIssue(
            number=fetched.number, title=fetched.title, body=fetched.body
        )

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        """Post the one terminal comment, with the bytes already recorded in
        ``outcomes`` (§14): a retried write sends the same ``body``."""
        response = self.client.post(
            f"{self._issue_path(issue_number)}/comments", json={"body": body}
        )
        if response.status_code != 201:
            raise GitHubTrackerError(
                f"POST {self._issue_path(issue_number)}/comments returned "
                f"{response.status_code}: {response.text}"
            )

    def remove_selection_label(self, issue_number: int) -> None:
        """Remove the Selection Label when still present (C-OUT-1).

        A ``404`` means the label is already absent — the write is naturally
        idempotent (§13) — so it is the successful no-op, not a failure.
        """
        label = quote(self.selection_label, safe="")
        response = self.client.delete(
            f"{self._issue_path(issue_number)}/labels/{label}"
        )
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"DELETE {self._issue_path(issue_number)}/labels/{label} returned "
                f"{response.status_code}: {response.text}"
            )

    def add_ready_for_human(self, issue_number: int) -> None:
        """Add ``ready-for-human`` on every outcome without a pull request."""
        response = self.client.post(
            f"{self._issue_path(issue_number)}/labels",
            json={"labels": [self.ready_for_human]},
        )
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"POST {self._issue_path(issue_number)}/labels returned "
                f"{response.status_code}: {response.text}"
            )

    def release_assignment(self, issue_number: int) -> None:
        """Remove the Worker's own assignment: the Agent Identity's login only.

        The caller (the ``terminal_writes`` node) performs this write only
        when the ledger records the assignment as ``assigned_by_us`` (§5); an
        assignment with no open Attempt is a human's and is never removed.
        """
        response = self.client.delete(
            f"{self._issue_path(issue_number)}/assignees",
            json={"assignees": [self.agent_login]},
        )
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"DELETE {self._issue_path(issue_number)}/assignees returned "
                f"{response.status_code}: {response.text}"
            )
