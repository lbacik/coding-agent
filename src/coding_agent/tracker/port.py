"""The ``TrackerPort`` Protocol the Attempt graph programs against.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (layers: the Attempt
graph owns every external write), §5 (the Claim is an assignment read back),
§12 (the pull request opens only for a ready Publication Gate verdict),
§13 (the write sequence; pull-request identity is the head branch plus the
Attempt Marker in the body; the remote lookup filters by the Agent Identity's
id), §14 ("Recording before publishing": the Terminal Outcome is recorded
before the terminal writes) and §18 (identity and authorization).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-GRF-1, P-DEL-1, P-DEL-10,
L3-DEL-14, L3-DEL-21.

M1 scope: only the operations the walking skeleton needs. ``claim`` fetches
the Target Issue; ``terminal_writes`` posts the terminal comment, removes the
Selection Label, adds ``ready-for-human`` on outcomes without a pull request,
and releases the Worker's own assignment. There is deliberately no operation
that applies the Selection Label and none that edits the issue body — those
two structural abstentions (§18) hold because the port cannot express them,
not because any caller promises restraint. GitHub mutation tools for the
model do not exist for the same reason (§1); the same forbidden set is
asserted structurally by ``tests/test_mvp_attempt_graph.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TrackedIssue:
    """The Target Issue as the tracker reports it at Claim time."""

    number: int
    title: str
    body: str


@dataclass(frozen=True)
class PullRequest:
    """One pull request as the tracker reports it (§13, §15).

    ``marker_present`` records whether the body carries the Attempt Marker
    the caller looked up: the rule reads in one direction only (L3-DEL-21),
    so a pull request from an ``agent/`` branch whose body carries no marker
    is never ours. ``draft`` is always ``False`` — no code path creates a
    draft pull request (P-DEL-10).
    """

    number: int
    url: str
    title: str
    head_branch: str
    base_branch: str
    body: str
    state: str
    author_login: str
    author_id: int | None = None
    marker_present: bool = False
    draft: bool = False


class TrackerPort(Protocol):
    """The tracker operations the Attempt graph may call."""

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        """Read the Target Issue's number, title and body."""
        ...

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        """Post the one terminal comment carrying the Attempt Marker (§15)."""
        ...

    def remove_selection_label(self, issue_number: int) -> None:
        """Remove the Workflow's Selection Label after the terminal comment."""
        ...

    def add_ready_for_human(self, issue_number: int) -> None:
        """Add ``ready-for-human`` on every outcome without a pull request."""
        ...

    def release_assignment(self, issue_number: int) -> None:
        """Remove the Worker's own assignment, only when recorded as ours."""
        ...

    def find_pull_request(self, head_branch: str, marker: str) -> PullRequest | None:
        """Look a pull request up by head branch and Attempt Marker (§13).

        Lists pull requests with ``head`` equal to ``head_branch`` and
        returns the one whose body carries ``marker`` and whose author is the
        Agent Identity — open preferred where both states match. A pull
        request from the same branch whose body carries no marker is not
        ours (L3-DEL-21): the lookup never adopts it and returns ``None``
        instead. A pull request by another account that carries our marker
        is not our receipt either (§13, §18).
        """
        ...

    def open_pull_request(
        self, *, title: str, head_branch: str, base_branch: str, body: str
    ) -> PullRequest:
        """Open one non-draft pull request from ``head_branch`` (§13, §15).

        The title is the Target Issue's title and the body is the recorded
        §15 body carrying ``Closes #N`` and the Attempt Marker. Never a
        draft (P-DEL-10): this signature has no draft parameter, so no
        caller can request one.
        """
        ...
