"""A fake ``TrackerPort`` for the M1 primary test seam.

Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Claim), §7
(readiness: the default Target Issue names its Acceptance Criteria and its
Seam Set, so the happy path resolves both), §13 (the pull-request write
sequence: the fake records the intent the way the ledger does — the node's
effect row — and here records the remote side: at most one pull request per
head branch opens, found by head branch plus Attempt Marker), §14 (the
terminal writes, recorded before they are performed) and §15 (the pull
request carries the Attempt Marker; it is never a draft).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-CLR-20, C-GRF-1, P-DEL-1, P-DEL-10,
L3-DEL-14, L3-DEL-21.

The fake performs nothing remotely: every call is appended to ``calls`` and
acknowledged. ``on_first_call`` lets a test observe the exact moment the
graph first touches the tracker — the M1 ordering proof is that the
``outcomes`` row already exists then. ``fail_on`` names one operation that
raises instead, for the L3-ERR-2 path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from coding_agent.tracker.port import PullRequest, TrackedIssue


@dataclass
class FakeTracker:
    """In-process ``TrackerPort`` double; never selectable by configuration."""

    issue: TrackedIssue = field(
        default_factory=lambda: TrackedIssue(
            number=7,
            title="Add a greeting",
            body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        )
    )
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    on_first_write: Callable[[str], None] | None = None
    fail_on: str | None = None
    agent_login: str = "coding-agent"
    agent_id: int | None = None
    pull_requests: list[PullRequest] = field(default_factory=list)
    _next_pull_number: int = field(default=1, repr=False)

    _READS = frozenset({"fetch_issue", "find_pull_request"})
    """Tracker calls that look up remote state without changing it (§13).

    The pull-request lookup never opens anything; only the writes prove the
    "recorded before performed" order, so the first-write observer ignores
    it the way it ignores the Claim fetch.
    """

    def _has_write(self) -> bool:
        return any(name not in self._READS for name, _ in self.calls)

    def _record(self, name: str, *args: object) -> None:
        if (
            name not in self._READS
            and self.on_first_write is not None
            and not self._has_write()
        ):
            self.on_first_write(name)
        self.calls.append((name, args))
        if self.fail_on == name:
            raise RuntimeError(f"fake tracker failure in {name}")

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        self._record("fetch_issue", issue_number)
        return self.issue

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        self._record("post_terminal_comment", issue_number, body)

    def remove_selection_label(self, issue_number: int) -> None:
        self._record("remove_selection_label", issue_number)

    def add_ready_for_human(self, issue_number: int) -> None:
        self._record("add_ready_for_human", issue_number)

    def release_assignment(self, issue_number: int) -> None:
        self._record("release_assignment", issue_number)

    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def _author_matches(self, candidate: PullRequest) -> bool:
        """Whether ``candidate`` is authored by the Agent Identity (§13, §18).

        Where the numeric id is known it decides; otherwise the login does.
        Either way a pull request by another account is not our receipt,
        even when its body carries our marker.
        """
        if self.agent_id is not None:
            return candidate.author_id == self.agent_id
        return candidate.author_login == self.agent_login

    def find_pull_request(self, head_branch: str, marker: str) -> PullRequest | None:
        """Look a pull request up by head branch and Attempt Marker (§13).

        The rule reads in one direction only (L3-DEL-21): a pull request
        from the same branch whose body carries no marker is never adopted.
        Open matches are preferred over closed ones.
        """
        self._record("find_pull_request", head_branch, marker)
        matching = [
            candidate
            for candidate in self.pull_requests
            if candidate.head_branch == head_branch
            and marker in candidate.body
            and self._author_matches(candidate)
        ]
        for candidate in matching:
            if candidate.state == "open":
                return candidate
        return matching[0] if matching else None

    def open_pull_request(
        self, *, title: str, head_branch: str, base_branch: str, body: str
    ) -> PullRequest:
        """Open one non-draft pull request, recording it as the remote holds it.

        Never a draft (P-DEL-10): the fake records ``draft=False`` on every
        pull request it opens, so the suite can assert no code path creates
        one.
        """
        self._record(
            "open_pull_request",
            title,
            head_branch,
            base_branch,
            body,
        )
        opened = PullRequest(
            number=self._next_pull_number,
            url=f"https://tracker.fake/pulls/{self._next_pull_number}",
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
            state="open",
            author_login=self.agent_login,
            author_id=self.agent_id,
            marker_present=True,
            draft=False,
        )
        self._next_pull_number += 1
        self.pull_requests.append(opened)
        return opened

    def human_close_pull_request(self, number: int) -> PullRequest:
        """A human closes the pull request: a withdrawal, not an absence (§13).

        The closed pull request stays recorded so the lookup still finds it;
        the Attempt ends ``abandoned`` and the pull request is never
        reopened (L3-DEL-14).
        """
        for index, candidate in enumerate(self.pull_requests):
            if candidate.number == number:
                closed = PullRequest(
                    number=candidate.number,
                    url=candidate.url,
                    title=candidate.title,
                    head_branch=candidate.head_branch,
                    base_branch=candidate.base_branch,
                    body=candidate.body,
                    state="closed",
                    author_login=candidate.author_login,
                    author_id=candidate.author_id,
                    marker_present=candidate.marker_present,
                    draft=candidate.draft,
                )
                self.pull_requests[index] = closed
                return closed
        raise AssertionError(f"no fake pull request #{number}")
