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
