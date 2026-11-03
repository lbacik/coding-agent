"""The temporary ``local`` tracker adapter for the M1 walking skeleton.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write), §5 (the Claim is an assignment read back), §13
(the write sequence; over this adapter the pull request stays recorded but
unperformed — the lookup finds nothing and the open records nothing
remotely), §14 ("Recording before publishing": the Terminal Outcome is
recorded before the terminal writes) and §18 (the two structural
abstentions: the Worker never edits the Target Issue's body and never
applies the Selection Label).

MVP row ids: L3-SEL-11, L3-CLR-19, P-DEL-1, P-DEL-10, L3-DEL-14, L3-DEL-21.

M1–M5 scope: this adapter implements the same ``TrackerPort`` as the GitHub
adapter that replaces it in M6, but it reads the Target Issue from GitHub
through the existing issue fetch and writes nothing to GitHub. Every
tracker write the Attempt graph performs (the pull request, the terminal
comment, the Selection Label removal, ``ready-for-human``, the assignment
release) is a local no-op: the Run Ledger's ``outcomes`` row is the durable
record of the Attempt, and the Terminal Outcome printed to stdout is its
publication. M4 fills the rendering of those writes; over this adapter they
stay recorded but unperformed.
"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.github.client import GitHubClient
from coding_agent.github.issues import fetch_issue
from coding_agent.tracker.port import PullRequest, TrackedIssue


@dataclass(frozen=True)
class LocalTracker:
    """A read-only ``TrackerPort``: one GET per issue read, zero writes.

    The ``client`` carries the credential, but only ``fetch_issue`` ever
    touches the wire, and only with ``GET``. The write operations exist
    because the port requires them; each is a deliberate no-op, which is
    what makes "writes nothing to GitHub" structural rather than promised.
    The pull-request lookup finds nothing (there is no remote pull request
    this adapter could own) and opening one records nothing remotely: the
    caller prints the rendered body instead.
    """

    owner: str
    repo: str
    client: GitHubClient

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        """Read the Target Issue's number, title and body (HTTP ``GET``)."""
        fetched = fetch_issue(self.client, self.owner, self.repo, issue_number)
        return TrackedIssue(
            number=fetched.number, title=fetched.title, body=fetched.body
        )

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        """Recorded in the ledger, unperformed on GitHub until M6."""
        _ = (issue_number, body)

    def remove_selection_label(self, issue_number: int) -> None:
        """Recorded in the ledger, unperformed on GitHub until M6."""
        _ = issue_number

    def add_ready_for_human(self, issue_number: int) -> None:
        """Recorded in the ledger, unperformed on GitHub until M6."""
        _ = issue_number

    def release_assignment(self, issue_number: int) -> None:
        """Recorded in the ledger, unperformed on GitHub until M6."""
        _ = issue_number

    def find_pull_request(self, head_branch: str, marker: str) -> PullRequest | None:
        """No remote pull request is ever ours over this adapter (§13)."""
        _ = (head_branch, marker)
        return None

    def open_pull_request(
        self, *, title: str, head_branch: str, base_branch: str, body: str
    ) -> PullRequest:
        """Recorded in the ledger, unperformed on GitHub until M6.

        Never a draft (P-DEL-10): the returned record carries
        ``draft=False``, like every pull request any adapter opens.
        """
        _ = (title, head_branch, base_branch)
        return PullRequest(
            number=0,
            url="",
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
            state="open",
            author_login="",
            author_id=None,
            marker_present=True,
            draft=False,
        )
