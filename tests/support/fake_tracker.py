"""A fake ``TrackerPort`` for the M1 primary test seam.

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (Selection:
candidates, guards, filters and order — selection writes nothing), §5 (the
Claim: one assignment confirmed by reading the issue back), §7
(readiness: the default Target Issue names its Acceptance Criteria and its
Seam Set, so the happy path resolves both), §13 (the pull-request write
sequence: the fake records the intent the way the ledger does — the node's
effect row — and here records the remote side: at most one pull request per
head branch opens, found by head branch plus Attempt Marker), §14 (the
terminal writes, recorded before they are performed) and §15 (the pull
request carries the Attempt Marker; it is never a draft).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-CLR-20, C-GRF-1, P-DEL-1, P-DEL-10,
L3-DEL-14, L3-DEL-21, P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1,
P-CLM-1, P-CLM-2.

The fake performs nothing remotely: every call is appended to ``calls`` and
acknowledged. ``on_first_write`` lets a test observe the exact moment the
graph first touches the tracker with a write — the M1 ordering proof is that
the ``outcomes`` row already exists then. ``fail_on`` names one operation
that raises instead, for the L3-ERR-2 path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from coding_agent.tracker.port import CandidateIssue, PullRequest, TrackedIssue
from coding_agent.tracker.selection import (
    parse_blocked_by_references,
    sort_candidates,
)

_FAKE_BASE_REVISION = "f" * 40
"""The Base Revision the fake reports for any branch (contract §5)."""

_READS = frozenset(
    {
        "fetch_issue",
        "find_pull_request",
        "list_candidates",
        "fetch_candidate",
        "has_open_blocker",
        "has_open_attempt_pull_request",
        "fetch_base_revision",
    }
)
"""Reads: lookups, selection and guard checks write nothing (§4, §13)."""


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
    """The Agent Identity's login: the only login ``assign_issue`` adds and
    ``release_assignment`` removes (contract §5)."""
    agent_id: int | None = None
    selection_label: str = "ready-for-agent"
    """The label ``remove_selection_label`` drops from a held candidate."""
    ready_for_human: str = "ready-for-human"
    """The label ``add_ready_for_human`` adds to a held candidate."""
    pull_requests: list[PullRequest] = field(default_factory=list)
    _next_pull_number: int = field(default=1, repr=False)
    candidates: dict[int, CandidateIssue] = field(default_factory=dict)
    """Selection candidates by issue number (contract §4)."""
    attempt_prs_open: set[int] = field(default_factory=set)
    """Issues with an open pull request carrying an Attempt Marker (§4)."""
    blockers_open: dict[int, bool] = field(default_factory=dict)
    """Explicit native-blocker answers by issue number, overriding the body.

    Where an issue has no entry, the ``Blocked by: #n`` fallback line in its
    body names the blockers and a referenced issue counts while it is open.
    """
    base_revision: str = _FAKE_BASE_REVISION
    on_assign: Callable[[int], None] | None = None
    """Hook run inside ``assign_issue`` after the assignment lands.

    Simulates a race before the Claim read-back: adding another assignee
    exercises P-CLM-2, adding a filter label exercises the C-SEL-1
    re-evaluation.
    """

    def _has_write(self) -> bool:
        return any(name not in _READS for name, _ in self.calls)

    def _record(self, name: str, *args: object) -> None:
        if (
            name not in _READS
            and self.on_first_write is not None
            and not self._has_write()
        ):
            self.on_first_write(name)
        self.calls.append((name, args))
        if self.fail_on == name:
            raise RuntimeError(f"fake tracker failure in {name}")

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        """The Target Issue: the selected candidate's own title and body
        where one is held, so the graph reads what the Claim fingerprinted
        (§5); otherwise the single default ``issue``."""
        self._record("fetch_issue", issue_number)
        candidate = self.candidates.get(issue_number)
        if candidate is not None:
            return TrackedIssue(
                number=candidate.number, title=candidate.title, body=candidate.body
            )
        return self.issue

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        self._record("post_terminal_comment", issue_number, body)

    def remove_selection_label(self, issue_number: int) -> None:
        self._record("remove_selection_label", issue_number)
        self._relabel(issue_number, remove=self.selection_label)

    def add_ready_for_human(self, issue_number: int) -> None:
        self._record("add_ready_for_human", issue_number)
        self._relabel(issue_number, add=self.ready_for_human)

    def _relabel(
        self, issue_number: int, *, add: str | None = None, remove: str | None = None
    ) -> None:
        """Apply a terminal label write to a held candidate, so a later
        sweep sees what GitHub would (§4, §14)."""
        candidate = self.candidates.get(issue_number)
        if candidate is None:
            return
        labels = tuple(label for label in candidate.labels if label != remove)
        if add is not None and add not in labels:
            labels = (*labels, add)
        self.candidates[issue_number] = replace(candidate, labels=labels)

    def release_assignment(self, issue_number: int) -> None:
        self._record("release_assignment", issue_number)
        candidate = self.candidates.get(issue_number)
        if candidate is not None:
            self.candidates[issue_number] = replace(
                candidate,
                assignees=tuple(
                    login for login in candidate.assignees if login != self.agent_login
                ),
            )

    def list_candidates(self, selection_label: str) -> list[CandidateIssue]:
        """The open candidates carrying ``selection_label`` (§4)."""
        self._record("list_candidates", selection_label)
        return sort_candidates(
            candidate
            for candidate in self.candidates.values()
            if candidate.is_open and selection_label in candidate.labels
        )

    def fetch_candidate(self, issue_number: int) -> CandidateIssue:
        """Re-read one candidate with every guard fact (§4, §5 read-back)."""
        self._record("fetch_candidate", issue_number)
        candidate = self.candidates.get(issue_number)
        if candidate is not None:
            return candidate
        return CandidateIssue(
            number=issue_number,
            title=self.issue.title,
            body=self.issue.body,
            is_open=True,
            assignees=(),
            labels=(),
            created_at="1970-01-01T00:00:00Z",
        )

    def has_open_blocker(self, issue_number: int) -> bool:
        """Whether the issue has an open native blocker, or an open issue in
        its ``Blocked by`` fallback line (§4)."""
        self._record("has_open_blocker", issue_number)
        forced = self.blockers_open.get(issue_number)
        if forced is not None:
            return forced
        candidate = self.candidates.get(issue_number)
        body = candidate.body if candidate is not None else self.issue.body
        for referenced in parse_blocked_by_references(body):
            other = self.candidates.get(referenced)
            if other is None or other.is_open:
                return True
        return False

    def has_open_attempt_pull_request(self, issue_number: int) -> bool:
        """Whether an open pull request carries an Attempt Marker for it (§4)."""
        self._record("has_open_attempt_pull_request", issue_number)
        return issue_number in self.attempt_prs_open

    def assign_issue(self, issue_number: int) -> None:
        """Assign the Agent Identity: the Claim's one write (§5, P-CLM-1)."""
        self._record("assign_issue", issue_number)
        candidate = self.candidates.get(issue_number)
        if candidate is not None and self.agent_login not in candidate.assignees:
            self.candidates[issue_number] = replace(
                candidate, assignees=(*candidate.assignees, self.agent_login)
            )
        if self.on_assign is not None:
            self.on_assign(issue_number)

    def fetch_base_revision(self, base_branch: str) -> str:
        """Resolve the Base Revision from the configured base branch (§5)."""
        self._record("fetch_base_revision", base_branch)
        return self.base_revision

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
