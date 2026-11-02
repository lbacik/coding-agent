"""The ``TrackerPort`` Protocol the Attempt graph programs against.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (layers: the Attempt
graph owns every external write), §5 (the Claim is an assignment read back),
§14 ("Recording before publishing": the Terminal Outcome is recorded before
the terminal writes) and §18 (identity and authorization).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-GRF-1.

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


class TrackerPort(Protocol):
    """The tracker operations the M1 Attempt graph may call."""

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
