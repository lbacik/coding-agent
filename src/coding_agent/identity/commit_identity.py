"""Branch and commit identity for every snapshot an Attempt pushes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (one branch per
Attempt, ``agent/<issue>/<n>-<slug>``) and §18 (author and committer are the
Agent Identity's id-based noreply address; every commit carries the
``Attempt: #<issue>/<n>`` trailer; commits are unsigned and never squashed;
force-push is never needed and the capability is never held; no branch is
ever deleted).

MVP row ids: L3-DEL-19, L3-DEL-20.
"""

from __future__ import annotations

import re

from coding_agent.identity.startup import Identity

ATTEMPT_BRANCH_PREFIX = "agent"
_SLUG_MAX_LENGTH = 40
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def noreply_author(identity: Identity) -> tuple[str, str]:
    """The id-based noreply `(name, email)` pair every Delivery Snapshot is
    authored and committed as: `<id>+<login>@users.noreply.github.com`,
    recognised by numeric account id rather than login (CONTEXT.md's
    Agent Identity)."""
    email = f"{identity.account_id}+{identity.login}@users.noreply.github.com"
    return identity.login, email


def slugify(text: str, *, max_length: int = _SLUG_MAX_LENGTH) -> str:
    """Lowercase, non-alphanumeric runs collapsed to one hyphen, trimmed to
    `max_length` and stripped of a trailing partial word — used for the
    branch name only, never for anything a human reads as prose."""
    slug = _NON_SLUG_CHARS.sub("-", text.lower()).strip("-")
    if len(slug) <= max_length:
        return slug
    return slug[:max_length].rsplit("-", 1)[0]


def attempt_branch_name(issue_number: int, attempt_number: int, issue_title: str) -> str:
    """`agent/<issue>/<n>-<slug>` (`L3-DEL-19`) — one branch per Attempt."""
    return f"{ATTEMPT_BRANCH_PREFIX}/{issue_number}/{attempt_number}-{slugify(issue_title)}"


def attempt_trailer(issue_number: int, attempt_number: int) -> str:
    """The Attempt Marker carried as a commit trailer (CONTEXT.md's Attempt
    Marker): tells the Worker's own commit apart from a human's."""
    return f"Attempt: #{issue_number}/{attempt_number}"


def delivery_snapshot_message(issue_number: int, issue_title: str, attempt_number: int) -> str:
    """The Delivery Snapshot's commit message: a one-line summary naming the
    Target Issue, then the Attempt Marker trailer on its own paragraph."""
    return (
        f"Implement #{issue_number}: {issue_title}\n\n"
        f"{attempt_trailer(issue_number, attempt_number)}"
    )


def review_snapshot_message(issue_number: int, issue_title: str, attempt_number: int) -> str:
    """The review snapshot's commit message (§10, ADR 0002).

    Like the Delivery Snapshot it names the Target Issue on its first line
    and carries the ``Attempt: #<issue>/<n>`` trailer on its own paragraph
    (L3-DEL-20); it is committed under the same id-based noreply identity,
    unsigned and never squashed, onto the same Attempt branch (L3-DEL-19).
    """
    return (
        f"Review snapshot #{issue_number}: {issue_title}\n\n"
        f"{attempt_trailer(issue_number, attempt_number)}"
    )
