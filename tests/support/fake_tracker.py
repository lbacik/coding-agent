"""A fake ``TrackerPort`` for the M1 primary test seam.

Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Claim) and §14
(the terminal writes, recorded before they are performed).

MVP row ids: L3-SEL-11, L3-CLR-19, C-GRF-1.

The fake performs nothing remotely: every call is appended to ``calls`` and
acknowledged. ``on_first_call`` lets a test observe the exact moment the
graph first touches the tracker — the M1 ordering proof is that the
``outcomes`` row already exists then. ``fail_on`` names one operation that
raises instead, for the L3-ERR-2 path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from coding_agent.tracker.port import TrackedIssue


@dataclass
class FakeTracker:
    """In-process ``TrackerPort`` double; never selectable by configuration."""

    issue: TrackedIssue = field(
        default_factory=lambda: TrackedIssue(
            number=7,
            title="Add a greeting",
            body="## Acceptance criteria\n- [ ] greet\n",
        )
    )
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    on_first_write: Callable[[str], None] | None = None
    fail_on: str | None = None

    def _has_write(self) -> bool:
        return any(name != "fetch_issue" for name, _ in self.calls)

    def _record(self, name: str, *args: object) -> None:
        if (
            name != "fetch_issue"
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
