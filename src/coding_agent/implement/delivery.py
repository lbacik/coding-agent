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
    push_branch,
)

NO_CHANGE_PRODUCED: Literal["no-change-produced"] = "no-change-produced"
COMMITTED_AND_PUSHED: Literal["committed-and-pushed"] = "committed-and-pushed"

DeliveryOutcomeKind = Literal["no-change-produced", "committed-and-pushed"]


@dataclass(frozen=True)
class DeliverySnapshotOutcome:
    """`commit_snapshot` + `push_snapshot`, the two runtime-contract nodes
    right after `implement`."""

    kind: DeliveryOutcomeKind
    """`"no-change-produced"` (`L3-IMP-10`) or `"committed-and-pushed"`."""
    branch_name: str | None = None
    commit_sha: str | None = None


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
    author_name, author_email = noreply_author(identity)
    message = delivery_snapshot_message(issue_number, issue_title, attempt_number)

    create_attempt_branch(workspace, branch_name)
    commit_sha = commit_delivery_snapshot(
        workspace, message=message, author_name=author_name, author_email=author_email
    )
    push_branch(workspace, remote_url, branch_name, redact=redact)

    return DeliverySnapshotOutcome(
        kind=COMMITTED_AND_PUSHED, branch_name=branch_name, commit_sha=commit_sha
    )
