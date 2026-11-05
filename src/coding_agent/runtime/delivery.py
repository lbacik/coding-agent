"""The Delivery Snapshot commit and push after the implementation stage.

Contract: ``docs/contract/mvp-runtime-contract.md`` §12
(``delivery_snapshot`` runs for every Implementation Outcome: when the tree
differs from the Attempt's last commit it commits the whole tree as the
Delivery Snapshot, then pushes the branch whenever the branch has commits
the remote lacks; commit and push precede the final checks,
[ADR 0002](../adr/0002-commit-and-push-before-review.md)), §13 (one branch
per Attempt, ``agent/<issue>/<n>-<slug>``; the remote ref is looked up with
``ls-remote`` — fast-forward only, never force, so a remote ref holding
any other value is a foreign change: no push, reported as a non-mutating
remote-branch collision; the write follows the same effects sequence as the
review snapshot, keyed by ``(attempt_id, write_kind)``) and §18 (author and
committer are the Agent Identity's id-based
noreply address; every commit carries the ``Attempt: #<issue>/<n>``
trailer; commits are unsigned and never squashed; no branch is ever
deleted). The review-snapshot push behind ``activate_code_review`` (§10)
lives in :mod:`coding_agent.runtime.review_snapshot`.

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (credentials reach
git through a credential helper rather than a remote URL, and every failure
message passes through the redaction registry).

MVP row ids: C-PUB-1, L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from coding_agent.identity.commit_identity import (
    attempt_branch_name,
    delivery_snapshot_message,
    noreply_author,
)
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import (
    Workspace,
    commit_delivery_snapshot,
    commit_touches_workflows_path,
    create_attempt_branch,
    ensure_attempt_branch,
    has_uncommitted_changes,
    head_revision,
    local_branch_exists,
    tree_touches_workflows_path,
    GitFailure,
    push_branch,
    remote_branch_exists,
    remote_ref_sha,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.runtime.review_snapshot import (
    WORKFLOWS_FAILURE_REASON,
    WORKFLOWS_PATH,
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
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> DeliverySnapshotOutcome:
    """A tree identical to the Base Revision reports `no-change-produced`
    and pushes nothing (`L3-IMP-10`). Otherwise the Delivery Snapshot is
    committed under the id-based noreply identity, carrying the
    `Attempt: #<issue>/<n>` trailer, onto `agent/<issue>/<n>-<slug>`
    (`L3-DEL-19`), and that branch is pushed.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (git
    authenticates through the credential helper, never a token in the URL;
    every failure message passes through the redaction registry).

    MVP row ids: L3-ERR-11.
    """
    if not has_uncommitted_changes(workspace):
        return DeliverySnapshotOutcome(kind=NO_CHANGE_PRODUCED)

    branch_name = attempt_branch_name(issue_number, attempt_number, issue_title)
    if remote_branch_exists(remote_url, branch_name, redact=redact, helper=helper):
        return _remote_branch_collision(branch_name)

    author_name, author_email = noreply_author(identity)
    message = delivery_snapshot_message(issue_number, issue_title, attempt_number)

    create_attempt_branch(workspace, branch_name)
    commit_sha = commit_delivery_snapshot(
        workspace, message=message, author_name=author_name, author_email=author_email
    )
    try:
        push_branch(workspace, remote_url, branch_name, redact=redact, helper=helper)
    except GitFailure:
        if remote_branch_exists(remote_url, branch_name, redact=redact, helper=helper):
            return _remote_branch_collision(branch_name, commit_sha=commit_sha)
        raise

    return DeliverySnapshotOutcome(
        kind=COMMITTED_AND_PUSHED, branch_name=branch_name, commit_sha=commit_sha
    )


DELIVERY_SNAPSHOT_WRITE_KIND = "delivery-snapshot-push"
"""The ``effects`` write kind for the Delivery Snapshot push (§13).

Unlike the review-snapshot push the Delivery Snapshot runs once per
Attempt, so the kind carries no ordinal: one ``(attempt_id, write_kind)``
row holds its intent and receipt. MVP row ids: C-PUB-1.
"""


@dataclass(frozen=True)
class DeliverySnapshotPublished:
    """The result of one Delivery Snapshot commit and push (§12, §13)."""

    branch_name: str | None
    """The Attempt branch, or ``None`` where nothing was committed or pushed."""

    commit_sha: str | None
    """The Delivery Snapshot commit, or ``None`` where the tree was clean."""

    remote_sha: str | None
    """The remote ref after the write, or the foreign ref where one collided."""

    pushed: bool
    """Whether this call pushed the branch."""

    foreign: bool = False
    """Whether the remote held a foreign change, so nothing was pushed (§13).

    ``True`` means the remote ref was neither absent nor one of this
    Attempt's own shas: the branch was left untouched and the caller —
    the Attempt graph's ``delivery_snapshot`` node — ends the Attempt
    ``failed`` / ``interrupted`` with a note, rather than continuing to
    final checks and publication. ``False`` covers both the pushed and
    the already-satisfied cases.
    """


DELIVERY_SNAPSHOT_COLLISION_KIND = "delivery-snapshot-collision"
"""Run Ledger event kind for a foreign remote Attempt branch (§13, §15).

Recorded by the Attempt graph when :func:`publish_delivery_snapshot`
reports ``foreign=True``: the branch, the local commit and the foreign
remote sha, so the terminal comment can name the collision and its
human action from durable state rather than memory.
"""


def _is_hex_sha(value: object) -> bool:
    """Whether ``value`` looks like a git object sha (contract §13)."""
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


#: Payload keys carrying one of this Attempt's own shas (§13, REC-INT-3).
_OURS_SHA_KEYS = ("commit_sha", "remote_sha", "head_sha")


def _is_push_write_kind(write_kind: str) -> bool:
    """Whether an ``effects`` write kind records a branch push (§13)."""
    # Mirrors ``interrupted._is_push_write_kind`` without the import cycle:
    # ``interrupted`` imports this module's write kind, so this module
    # cannot import that helper at load time.
    from coding_agent.runtime.review_snapshot import REVIEW_SNAPSHOT_KIND_PREFIX

    from coding_agent.runtime.interrupted import INTERRUPTED_PUSH_KIND

    return (
        write_kind == INTERRUPTED_PUSH_KIND
        or write_kind == DELIVERY_SNAPSHOT_WRITE_KIND
        or write_kind.startswith(REVIEW_SNAPSHOT_KIND_PREFIX)
    )


def _delivery_ours_shas(ledger: RunLedger, attempt_id: str) -> set[str]:
    """Every sha the ledger attributes to this Attempt's own pushes (§13).

    The remote ref is ours when it equals the last receipt's sha or an
    open push intent's sha: intents carry the sha about to be pushed,
    receipts the remote sha after it — in the effect's ``remote_id`` and
    under the sha-carrying payload keys — so a remote head matching any
    of them is recognised as ours and anything else is a foreign change.
    Only push write kinds count, so a non-push intent whose payload
    happened to carry a ``branch`` is never mistaken for a push.
    """
    shas: set[str] = set()
    for effect in ledger.list_effects(attempt_id):
        if not _is_push_write_kind(effect.write_kind):
            continue
        if effect.remote_id:
            shas.add(effect.remote_id)
        payload = effect.payload
        if isinstance(payload, dict):
            for key in _OURS_SHA_KEYS:
                value = payload.get(key)
                if _is_hex_sha(value):
                    assert isinstance(value, str)
                    shas.add(value)
    return shas


class DeliverySnapshotRefused(Exception):
    """A Delivery Snapshot push the Worker refuses itself (§13).

    A tree touching ``.github/workflows/`` is refused before any commit, as
    is a commit touching it (classified by write kind and target path, never
    by error string, L3-ERR-9). The node maps this to ``failed`` /
    ``workflows-path``. Any other push failure propagates as
    :class:`GitFailure` for the graph's infrastructure-class mapping, never
    as this refusal.
    """

    def __init__(self, write_kind: str, target_path: str) -> None:
        super().__init__(
            f"delivery snapshot {write_kind} refused: {WORKFLOWS_FAILURE_REASON} ({target_path})"
        )
        self.write_kind = write_kind
        self.target_path = target_path
        self.reason = WORKFLOWS_FAILURE_REASON


def publish_delivery_snapshot(
    *,
    workspace: Workspace,
    ledger: RunLedger,
    attempt_id: str,
    issue_number: int,
    attempt_number: int,
    issue_title: str,
    identity: Identity,
    remote_url: str,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> DeliverySnapshotPublished:
    """Commit the whole tree and push the Attempt's branch (§12, §13).

    Runs for every Implementation Outcome: when the tree differs from the
    Attempt's last commit it commits the whole tree as the Delivery Snapshot
    (author and committer are the Agent Identity's id-based noreply address,
    carrying the ``Attempt: #<issue>/<n>`` trailer, unsigned and never
    squashed, L3-DEL-20), then pushes the branch whenever the branch has
    commits the remote lacks. ``already satisfied`` (no diff, no branch)
    commits nothing, pushes nothing and creates no branch.

    The write follows the same effects sequence as the review snapshot (§13):
    record the intent keyed by ``(attempt_id, write_kind)``, look the remote
    ref up with ``ls-remote``, push only when it is absent or one of this
    Attempt's own shas, and record the receipt carrying the remote sha.
    Re-entering a ``write_kind`` whose receipt exists pushes nothing and
    returns the recorded sha. A remote ref holding any other value is a
    foreign change: no push is attempted, and the returned
    :class:`DeliverySnapshotPublished` carries ``foreign=True`` with the
    foreign sha for the Attempt graph to end ``failed`` / ``interrupted``
    before the Delivery Intent (C-PUB-1 owns only the commit and the push,
    not the verdict).
    """
    branch_name = attempt_branch_name(issue_number, attempt_number, issue_title)

    existing = ledger.get_effect(attempt_id, DELIVERY_SNAPSHOT_WRITE_KIND)
    if existing is not None and existing.status == "receipt":
        stored = dict(existing.payload)
        commit = stored.get("commit_sha")
        branch = stored.get("branch")
        return DeliverySnapshotPublished(
            branch_name=str(branch) if branch is not None else branch_name,
            commit_sha=str(commit) if commit is not None else None,
            remote_sha=existing.remote_id,
            pushed=False,
        )

    if not has_uncommitted_changes(workspace) and not local_branch_exists(
        workspace.path, branch_name
    ):
        return DeliverySnapshotPublished(
            branch_name=None, commit_sha=None, remote_sha=None, pushed=False
        )

    ensure_attempt_branch(workspace, branch_name)
    if has_uncommitted_changes(workspace):
        if tree_touches_workflows_path(workspace.path):
            raise DeliverySnapshotRefused(DELIVERY_SNAPSHOT_WRITE_KIND, WORKFLOWS_PATH)
        author_name, author_email = noreply_author(identity)
        message = delivery_snapshot_message(issue_number, issue_title, attempt_number)
        commit_delivery_snapshot(
            workspace, message=message, author_name=author_name, author_email=author_email
        )
        if commit_touches_workflows_path(workspace.path):
            raise DeliverySnapshotRefused(DELIVERY_SNAPSHOT_WRITE_KIND, WORKFLOWS_PATH)

    local_sha = head_revision(workspace.path)
    payload: dict[str, object] = {
        "branch": branch_name,
        "commit_sha": local_sha,
        "attempt_id": attempt_id,
    }
    ledger.record_effect_intent(
        attempt_id, DELIVERY_SNAPSHOT_WRITE_KIND, remote_id=local_sha, payload=payload
    )

    remote_sha = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper)
    if remote_sha == local_sha:
        receipt = ledger.record_effect_receipt(
            attempt_id, DELIVERY_SNAPSHOT_WRITE_KIND, remote_id=local_sha, payload=payload
        )
        return DeliverySnapshotPublished(
            branch_name=branch_name,
            commit_sha=local_sha,
            remote_sha=receipt.remote_id or local_sha,
            pushed=False,
        )

    if (
        remote_sha is not None
        and remote_sha != local_sha
        and remote_sha not in _delivery_ours_shas(ledger, attempt_id)
    ):
        return DeliverySnapshotPublished(
            branch_name=branch_name,
            commit_sha=local_sha,
            remote_sha=remote_sha,
            pushed=False,
            foreign=True,
        )

    try:
        push_branch(workspace, remote_url, branch_name, redact=redact, helper=helper)
    except GitFailure:
        try:
            current = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper)
        except GitFailure:
            raise
        if current is None:
            raise
        if current == local_sha:
            receipt = ledger.record_effect_receipt(
                attempt_id,
                DELIVERY_SNAPSHOT_WRITE_KIND,
                remote_id=current,
                payload={**payload, "remote_sha": current},
            )
            return DeliverySnapshotPublished(
                branch_name=branch_name,
                commit_sha=local_sha,
                remote_sha=receipt.remote_id or current,
                pushed=True,
            )
        if current in _delivery_ours_shas(ledger, attempt_id):
            raise
        return DeliverySnapshotPublished(
            branch_name=branch_name,
            commit_sha=local_sha,
            remote_sha=current,
            pushed=False,
            foreign=True,
        )

    after = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper) or local_sha
    ledger.record_effect_receipt(
        attempt_id,
        DELIVERY_SNAPSHOT_WRITE_KIND,
        remote_id=after,
        payload={**payload, "remote_sha": after},
    )
    return DeliverySnapshotPublished(
        branch_name=branch_name, commit_sha=local_sha, remote_sha=after, pushed=True
    )

