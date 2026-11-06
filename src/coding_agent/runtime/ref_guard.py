"""The Ref Guard: the Attempt's branch and HEAD belong to the runtime.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (after every tool
call that executes project code the runtime compares the Attempt's branch
and HEAD with their state before the call; where either moved it restores
both with ref plumbing only, records a Run Ledger event and says so in the
tool result; the guard gates nothing and never undoes working-tree changes).

The guard watches three values around each guarded call:

- (a) the name of the branch ``HEAD`` points at (``None`` where detached);
- (b) the commit ``HEAD`` resolves to;
- (c) the commit the Attempt branch's ref (``refs/heads/<branch>``) points to.

Where any differs afterwards, the guard restores all three with plumbing
only — ``update-ref`` for the branch ref, ``symbolic-ref`` for ``HEAD`` —
never with ``checkout`` or ``reset --hard``. The working tree and the index
stay exactly as the call left them: a stash, a ``checkout -- <path>`` or a
hard reset's file effects are tree changes and belong to the Execution
Footprint, not to the guard. The footprint itself never watches ``.git/``,
which is why the guard exists beside it.

A restoration records a Run Ledger event of kind ``ref_guard_restored`` and
adds one part to the tool result naming what moved and that commits are made
by the runtime. A call that moved nothing records no event and leaves the
result byte-for-byte unchanged. A restoration that fails (a stale
``.git/index.lock``, a missing object) is a git infrastructure failure: the
caller ends the Attempt ``failed`` / ``infrastructure-failure`` with class
``git`` and never carries on with a ``HEAD`` the runtime does not own.

MVP row ids: C-RG-1, C-RG-2, C-RG-3, C-RG-4, C-RG-5.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coding_agent.ledger.store import BudgetSnapshot, LedgerRecord, RunLedger
from coding_agent.runtime.git import (
    GitFailure,
    attempt_branch_sha,
    head_branch,
    head_revision,
    point_head_at_branch,
    restore_detached_head,
    update_branch_ref,
)

REF_GUARD_KIND = "ref_guard_restored"
"""Run Ledger event kind for one Ref Guard restoration (§9)."""


class RefGuardFailed(GitFailure):
    """A Ref Guard restoration that could not be completed (§9).

    A subclass of :class:`GitFailure` so the Attempt graph's uncaught-exception
    classification ends the Attempt ``failed`` / ``infrastructure-failure``
    with class ``git``: the Attempt never carries on with a branch or ``HEAD``
    the runtime does not own.
    """


@dataclass(frozen=True)
class RefState:
    """The three ref values the guard compares around one guarded call."""

    branch: str | None
    """(a) the branch ``HEAD`` points at, or ``None`` where detached."""

    head_sha: str | None
    """(b) the commit ``HEAD`` resolves to, or ``None`` where unreadable."""

    branch_sha: str | None
    """(c) the commit ``refs/heads/<attempt branch>`` points to, if any."""


def read_ref_state(workspace: Path, attempt_branch: str) -> RefState:
    """Read the guard's three values without changing anything.

    Never raises: an unreadable value reads as ``None``, which compares
    unequal to any readable value and therefore always triggers a restore
    attempt rather than silently keeping a ``HEAD`` the runtime did not
    read.
    """
    try:
        branch = head_branch(workspace)
    except (GitFailure, OSError):
        branch = None
    try:
        head_sha: str | None = head_revision(workspace)
    except (GitFailure, OSError):
        head_sha = None
    try:
        branch_sha: str | None = attempt_branch_sha(workspace, attempt_branch)
    except OSError:
        branch_sha = None
    return RefState(branch=branch, head_sha=head_sha, branch_sha=branch_sha)


def restore_ref_state(
    workspace: Path, *, attempt_branch: str, before: RefState
) -> None:
    """Restore all three values to ``before`` with ref plumbing only.

    Points the Attempt branch ref back at its previous commit and ``HEAD``
    back at the Attempt branch, leaving the working tree and the index
    exactly as the guarded call left them. Raises :class:`RefGuardFailed`
    where the restore cannot be completed.
    """
    try:
        # (c): the Attempt branch ref back to its previous commit.
        update_branch_ref(workspace, attempt_branch, before.branch_sha)
        # (a) and (b): HEAD back where it was — at the branch it pointed at,
        # or detached at its previous commit.
        if before.branch is None:
            if before.head_sha is None:
                raise RefGuardFailed(
                    "ref guard could not restore the Attempt refs: "
                    "the pre-call HEAD could not be read"
                )
            restore_detached_head(workspace, sha=before.head_sha)
        else:
            point_head_at_branch(workspace, before.branch)
    except RefGuardFailed:
        raise
    except (GitFailure, OSError) as exc:
        raise RefGuardFailed(f"ref guard could not restore the Attempt refs: {exc}") from exc


def format_ref_guard_suffix(tool: str, before: RefState, after: RefState) -> str:
    """The extra tool-result part naming a Ref Guard restoration (§9)."""
    return (
        "\n\nRef Guard: the branch and HEAD moved during this call and were "
        f"restored. Before: branch={before.branch!r} HEAD={before.head_sha!r} "
        f"refs/heads moved from {before.branch_sha!r} to {after.branch_sha!r}; "
        f"after the call: branch={after.branch!r} HEAD={after.head_sha!r}. "
        "Commits, the branch and HEAD are the runtime's: the model never "
        f"commits. ({tool} call)"
    )


def record_ref_guard_event(
    ledger: RunLedger,
    attempt_id: str,
    snapshot: BudgetSnapshot,
    *,
    tool: str,
    arg: str,
    before: RefState,
    after: RefState,
) -> LedgerRecord:
    """Record one Ref Guard restoration as a Run Ledger event (§9).

    The payload carries the tool name and its argument with the before and
    after values of (a), (b) and (c); absent values are stored as null.
    """
    return ledger.record(
        attempt_id,
        REF_GUARD_KIND,
        snapshot,
        detail=(
            f"ref guard restored the branch and HEAD after {tool} {arg!r}: "
            f"branch {after.branch!r} HEAD {after.head_sha!r}"
        ),
        payload={
            "tool": tool,
            "arg": arg,
            "before": {
                "branch": before.branch,
                "head_sha": before.head_sha,
                "branch_sha": before.branch_sha,
            },
            "after": {
                "branch": after.branch,
                "head_sha": after.head_sha,
                "branch_sha": after.branch_sha,
            },
        },
    )
