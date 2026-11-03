"""Selection guards, filters and order (contract §4).

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (candidates are the
open issues of the Target Repository that carry the Workflow's Selection
Label; the fixed guards exclude a candidate that is not open, has any
assignee, carries ``ready-for-human`` by its configured name, carries
``wontfix``, carries any ``wayfinder:*`` label, has an open native blocker,
has an empty body, or has an open pull request carrying an Attempt Marker
for it; the Workflow's ``require_all``, ``require_any`` and ``exclude``
filters narrow the set further and are re-evaluated at the Claim read-back,
never at a later Gate; the order is ``(created_at, number)`` ascending;
selection writes nothing).

MVP row ids: P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1.

This module is pure: it takes reported issue facts and answers which reason
excludes a candidate, or ``None`` where none does. It performs no network
call and no ledger write, which is what makes "selection writes nothing"
structural rather than promised.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from coding_agent.tracker.port import CandidateIssue


def exclusion_reason(
    candidate: CandidateIssue,
    *,
    ready_for_human: str,
    blocker_open: bool,
    attempt_pr_open: bool = False,
    require_all: Sequence[str] = (),
    require_any: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> str | None:
    """The reason ``candidate`` is excluded, or ``None`` where it is selectable.

    The fixed guards run in contract §4 order, then the Workflow's filters:
    ``require_all`` (every listed label present), ``require_any`` (at least
    one present) and ``exclude`` (none present). ``blocker_open`` reports
    whether the issue has an open native blocker and ``attempt_pr_open``
    whether an open pull request carries an Attempt Marker for it — both are
    read by the caller, never here, so this function stays pure.
    """
    if not candidate.is_open:
        return "closed"
    if candidate.assignees:
        return "assigned"
    if ready_for_human in candidate.labels:
        return "ready-for-human"
    if "wontfix" in candidate.labels:
        return "wontfix"
    if any(label.startswith("wayfinder:") for label in candidate.labels):
        return "wayfinder"
    if blocker_open:
        return "blocker"
    if not candidate.body.strip():
        return "empty-body"
    if attempt_pr_open:
        return "attempt-pr"
    labels = set(candidate.labels)
    if any(required not in labels for required in require_all):
        return "require_all"
    if require_any and not any(required in labels for required in require_any):
        return "require_any"
    if any(excluded in labels for excluded in exclude):
        return "exclude"
    return None


_BLOCKED_BY_RE = re.compile(r"(?im)^blocked by\s*:(?P<refs>.*)$")
_NUMBER_RE = re.compile(r"#(?P<number>\d+)")


def parse_blocked_by_references(body: str) -> tuple[int, ...]:
    """The issue numbers of a ``Blocked by: #n, #m`` fallback line (§4).

    Where native issue dependencies are unavailable, a ``Blocked by`` line at
    the top of the child body names the blockers
    (``docs/agents/issue-tracker.md``). A body without one names none.
    """
    match = _BLOCKED_BY_RE.search(body)
    if match is None:
        return ()
    return tuple(
        int(found.group("number"))
        for found in _NUMBER_RE.finditer(match.group("refs"))
    )


def sort_candidates(
    candidates: Iterable[CandidateIssue],
) -> list[CandidateIssue]:
    """The contract §4 order: ``(created_at, number)`` ascending."""
    return sorted(candidates, key=lambda candidate: (candidate.created_at, candidate.number))
