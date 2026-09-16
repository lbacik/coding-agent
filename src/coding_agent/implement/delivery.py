from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from coding_agent.identity.commit_identity import (
    attempt_branch_name,
    delivery_snapshot_message,
    noreply_author,
)
from coding_agent.identity.startup import Identity
from coding_agent.implement.git import (
    Workspace,
    commit_delivery_snapshot,
    create_attempt_branch,
    has_uncommitted_changes,
    GitFailure,
    push_branch,
    remote_branch_exists,
)

NO_CHANGE_PRODUCED: Literal["no-change-produced"] = "no-change-produced"
COMMITTED_AND_PUSHED: Literal["committed-and-pushed"] = "committed-and-pushed"
REMOTE_BRANCH_COLLISION: Literal["remote-branch-collision"] = "remote-branch-collision"
REMOTE_BRANCH_COLLISION_NEXT_ACTION = "choose a new attempt number or inspect the existing remote branch"

DeliveryOutcomeKind = Literal[
    "no-change-produced", "committed-and-pushed", "remote-branch-collision"
]


@dataclass(frozen=True)
class DeliverySnapshotOutcome:
    """`commit_snapshot` + `push_snapshot`, the two runtime-contract nodes
    right after `implement`."""

    kind: DeliveryOutcomeKind
    """The delivery result, including a non-mutating remote branch collision."""
    branch_name: str | None = None
    commit_sha: str | None = None
    next_action: str | None = None


def _remote_branch_collision(
    branch_name: str, *, commit_sha: str | None = None
) -> DeliverySnapshotOutcome:
    return DeliverySnapshotOutcome(
        kind=REMOTE_BRANCH_COLLISION,
        branch_name=branch_name,
        commit_sha=commit_sha,
        next_action=REMOTE_BRANCH_COLLISION_NEXT_ACTION,
    )


def deliver_snapshot(
    workspace: Workspace,
    *,
    remote_url: str,
    identity: Identity,
    issue_number: int,
    issue_title: str,
    attempt_number: int,
    redact: str | None = None,
) -> DeliverySnapshotOutcome:
    """A tree identical to the Base Revision reports `no-change-produced`
    and pushes nothing (`L3-IMP-10`). Otherwise the Delivery Snapshot is
    committed under the id-based noreply identity, carrying the
    `Attempt: #<issue>/<n>` trailer, onto `agent/<issue>/<n>-<slug>`
    (`L3-DEL-19`), and that branch is pushed."""
    if not has_uncommitted_changes(workspace):
        return DeliverySnapshotOutcome(kind=NO_CHANGE_PRODUCED)

    branch_name = attempt_branch_name(issue_number, attempt_number, issue_title)
    if remote_branch_exists(remote_url, branch_name, redact=redact):
        return _remote_branch_collision(branch_name)

    author_name, author_email = noreply_author(identity)
    message = delivery_snapshot_message(issue_number, issue_title, attempt_number)

    create_attempt_branch(workspace, branch_name)
    commit_sha = commit_delivery_snapshot(
        workspace, message=message, author_name=author_name, author_email=author_email
    )
    try:
        push_branch(workspace, remote_url, branch_name, redact=redact)
    except GitFailure:
        if remote_branch_exists(remote_url, branch_name, redact=redact):
            return _remote_branch_collision(branch_name, commit_sha=commit_sha)
        raise

    return DeliverySnapshotOutcome(
        kind=COMMITTED_AND_PUSHED, branch_name=branch_name, commit_sha=commit_sha
    )
