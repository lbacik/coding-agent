"""The claim-confirmed finalization of an interrupted Attempt, shared live and at recovery.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Before the
Delivery Intent, the Attempt then takes the claim-confirmed path of the
recovery table (§17) while the process is still alive, within
``shutdown_grace_seconds``: local commits are pushed fast-forward, the
dirty tree is not committed, and the outcome is ``failed`` / ``interrupted``,
or ``abandoned`` if the issue was cancelled meanwhile") and §17 (the
recovery table row "Claim confirmed, no outcome recorded": preserve — push
local commits that descend from the remote head, fast-forward only; do not
commit the dirty tree, the comment states how many uncommitted files were not
preserved; decide — re-read the issue once, closed or Selection Label gone
is ``abandoned``, otherwise ``failed`` / ``interrupted``; record the outcome,
then perform its terminal writes).

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (credentials reach
git through a credential helper rather than a remote URL, and every failure
message passes through the redaction registry).

MVP row ids: P-CAN-3, REC-SIG-1, REC-INT-1, REC-INT-2, REC-INT-3, P-REC-1,
P-REC-4, L3-ERR-11.

The live SIGTERM path calls :func:`finalize_interrupted_attempt` from the
Attempt graph before the Delivery Intent; M8's "Recovery of interrupted
Attempts" calls the same routine at startup. That includes not pushing over
a foreign push: the remote ref is ours only when it equals the last
receipt's sha or an open push intent's sha (contract §13 effect identity),
verified from the ledger — never from the payload — so a foreign head means
no push and a note in the comment (REC-INT-3, P-REC-4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from coding_agent.identity.commit_identity import attempt_branch_name
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.final_checks import dirty_working_tree_files
from coding_agent.runtime.git import (
    GitFailure,
    attempt_branch_sha,
    head_revision,
    push_ref,
    remote_branch_exists,
    remote_ref_sha,
)
from coding_agent.runtime.publication import (
    compose_terminal_snapshot,
    render_snapshot_block,
    render_terminal_comment,
    terminal_next_action,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.runtime.shutdown import (
    ABANDONED_CANCELLED_REASON,
    ABANDONED_OUTCOME,
    FAILED_OUTCOME,
    INTERRUPTED_REASON,
)
from coding_agent.tracker.port import TrackerPort

INTERRUPTED_PUSH_KIND = "interrupted-push"
"""The ``effects`` write kind for the interrupted finalization's push (§13).

Recorded through the same intent/receipt sequence as every other external
write, so a second signal followed by M8 recovery re-enters idempotently
instead of pushing twice.
"""

INTERRUPTED_EVENT_KIND = "interrupted"
"""Run Ledger event kind carrying the finalization facts (§15, §17).

The payload holds what the terminal comment reports: whether local commits
were pushed (and to which sha), whether the remote head was foreign, and
how many uncommitted files were not preserved.
"""

MAX_LISTED_UNCOMMITTED_FILES = 20
"""How many uncommitted paths the ledger event names; the comment states the count."""


@dataclass(frozen=True)
class InterruptedPush:
    """What the push half preserved (§17): local commits only, never the tree."""

    pushed: bool
    remote_sha: str | None
    foreign: bool
    uncommitted_count: int


@dataclass(frozen=True)
class InterruptedResult:
    """The finalization's decision: the Terminal Outcome the Attempt ends with."""

    outcome: str
    reason: str
    pushed: bool
    remote_sha: str | None
    foreign: bool
    uncommitted_count: int


def _is_hex_sha(value: object) -> TypeGuard[str]:
    """Whether ``value`` looks like a git object sha (contract §13)."""
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


#: Payload keys that carry one of this Attempt's own shas (§13, REC-INT-3).
_OURS_SHA_KEYS = ("commit_sha", "remote_sha", "head_sha")


def _ours_shas(ledger: RunLedger, attempt_id: str) -> set[str]:
    """Every sha the ledger attributes to this Attempt's own pushes (§13).

    The remote ref is ours when it equals the last receipt's sha or an open
    push intent's sha: intents carry the sha about to be pushed, receipts the
    remote sha after it, in the effect's ``remote_id`` and under the
    sha-carrying payload keys — so a remote head matching any of them is
    recognised as ours (REC-INT-4) and anything else is a foreign change
    (REC-INT-3). A 40-hex string under any other payload key (a quoted sha in
    a comment body, a fingerprint) never counts as ours.
    """
    shas: set[str] = set()
    for effect in ledger.list_effects(attempt_id):
        if effect.remote_id:
            shas.add(effect.remote_id)
        payload = effect.payload
        if isinstance(payload, dict):
            for key in _OURS_SHA_KEYS:
                value = payload.get(key)
                if _is_hex_sha(value):
                    shas.add(value)
    return shas


def push_interrupted_commits(
    *,
    workspace_path: Path,
    branch_name: str,
    remote_url: str | None,
    ledger: RunLedger,
    attempt_id: str,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> InterruptedPush:
    """Push the Attempt branch's local commits fast-forward, or push nothing (§17).

    The dirty tree is never committed: the push moves exactly the branch
    ref's sha. Where the branch has no local commits there is nothing to
    preserve; where the remote ref is ours (absent, equal, or a ledger-known
    sha) the branch is pushed fast-forward; where it is foreign there is no
    push and the comment says so (REC-INT-3). A re-entered receipt pushes
    nothing and returns the recorded sha.
    """
    try:
        dirty = dirty_working_tree_files(workspace_path)
    except Exception:
        dirty = ()
    uncommitted_count = len(dirty)
    if remote_url is None:
        return InterruptedPush(
            pushed=False, remote_sha=None, foreign=False, uncommitted_count=uncommitted_count
        )
    local_sha = attempt_branch_sha(workspace_path, branch_name)
    if local_sha is None:
        return InterruptedPush(
            pushed=False, remote_sha=None, foreign=False, uncommitted_count=uncommitted_count
        )
    existing = ledger.get_effect(attempt_id, INTERRUPTED_PUSH_KIND)
    if existing is not None and existing.status == "receipt":
        return InterruptedPush(
            pushed=False,
            remote_sha=existing.remote_id,
            foreign=False,
            uncommitted_count=uncommitted_count,
        )
    payload: dict[str, object] = {
        "branch": branch_name,
        "commit_sha": local_sha,
        "attempt_id": attempt_id,
    }
    try:
        remote_sha = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper)
    except GitFailure:
        return InterruptedPush(
            pushed=False, remote_sha=None, foreign=False, uncommitted_count=uncommitted_count
        )
    if remote_sha is not None and remote_sha != local_sha and remote_sha not in _ours_shas(
        ledger, attempt_id
    ):
        return InterruptedPush(
            pushed=False,
            remote_sha=remote_sha,
            foreign=True,
            uncommitted_count=uncommitted_count,
        )
    ledger.record_effect_intent(
        attempt_id, INTERRUPTED_PUSH_KIND, remote_id=local_sha, payload=payload
    )
    if remote_sha == local_sha:
        receipt = ledger.record_effect_receipt(
            attempt_id, INTERRUPTED_PUSH_KIND, remote_id=local_sha, payload=payload
        )
        return InterruptedPush(
            pushed=False,
            remote_sha=receipt.remote_id or local_sha,
            foreign=False,
            uncommitted_count=uncommitted_count,
        )
    try:
        push_ref(workspace_path, remote_url, local_sha, branch_name, redact=redact, helper=helper)
    except GitFailure:
        if remote_branch_exists(remote_url, branch_name, redact=redact, helper=helper):
            try:
                current = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper)
            except GitFailure:
                current = None
            return InterruptedPush(
                pushed=False,
                remote_sha=current,
                foreign=True,
                uncommitted_count=uncommitted_count,
            )
        raise
    try:
        after = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper) or local_sha
    except GitFailure:
        after = local_sha
    ledger.record_effect_receipt(
        attempt_id,
        INTERRUPTED_PUSH_KIND,
        remote_id=after,
        payload={**payload, "remote_sha": after},
    )
    return InterruptedPush(
        pushed=True, remote_sha=after, foreign=False, uncommitted_count=uncommitted_count
    )


def decide_interrupted_outcome(
    *, tracker: TrackerPort, issue_number: int, selection_label: str
) -> tuple[str, str]:
    """Re-read the issue once: cancelled is ``abandoned``, else ``interrupted`` (§17).

    Closed, or the Selection Label gone, is ``abandoned`` / ``cancelled``;
    otherwise ``failed`` / ``interrupted``. Read-only: the decision writes
    nothing.
    """
    state = tracker.read_issue_state(issue_number)
    if not state.is_open or selection_label not in state.labels:
        return (ABANDONED_OUTCOME, ABANDONED_CANCELLED_REASON)
    return (FAILED_OUTCOME, INTERRUPTED_REASON)


def _event_budget(ledger: RunLedger, attempt_id: str) -> BudgetSnapshot:
    """The Attempt-wide budget for the finalization's ledger event (§11)."""
    try:
        elapsed = ledger.attempt_elapsed_seconds(attempt_id)
    except Exception:
        elapsed = 0.0
    return BudgetSnapshot(elapsed_seconds=elapsed, usage=UsageTotals())


def finalize_interrupted_attempt(
    *,
    ledger: RunLedger,
    tracker: TrackerPort,
    workspace_path: Path,
    attempt_id: str,
    issue_number: int,
    attempt_number: int,
    issue_title: str,
    remote_url: str | None,
    base_revision: str,
    workflow_id: str,
    configuration_digest: str,
    selection_label: str,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
    changed_files: list[str] | None = None,
    commands: list[dict[str, str]] | None = None,
    validation_evidence: list[dict[str, object]] | None = None,
) -> InterruptedResult:
    """Run the claim-confirmed finalization and record its Terminal Outcome (§17).

    Pushes local commits fast-forward without committing the dirty tree,
    re-reads the issue once to decide ``abandoned`` / ``cancelled`` versus
    ``failed`` / ``interrupted``, composes the deterministic terminal
    Continuation Snapshot with no model call, renders the terminal comment
    once, and records the outcome before any tracker write ("Recording
    before publishing": the live tail performs the recorded writes; M8
    performs them via :func:`perform_recorded_terminal_writes`). Returns the
    outcome and reason for the graph node to report. Where an outcome is
    already recorded, it is returned as-is and nothing is pushed or rendered
    twice.
    """
    stored = ledger.get_outcome(attempt_id)
    if stored is not None:
        push = push_interrupted_commits(
            workspace_path=workspace_path,
            branch_name=attempt_branch_name(issue_number, attempt_number, issue_title),
            remote_url=remote_url,
            ledger=ledger,
            attempt_id=attempt_id,
            redact=redact,
            helper=helper,
        )
        return InterruptedResult(
            outcome=stored.outcome,
            reason=stored.reason,
            pushed=push.pushed,
            remote_sha=push.remote_sha,
            foreign=push.foreign,
            uncommitted_count=push.uncommitted_count,
        )
    branch_name = attempt_branch_name(issue_number, attempt_number, issue_title)
    push = push_interrupted_commits(
        workspace_path=workspace_path,
        branch_name=branch_name,
        remote_url=remote_url,
        ledger=ledger,
        attempt_id=attempt_id,
        redact=redact,
        helper=helper,
    )
    outcome, reason = decide_interrupted_outcome(
        tracker=tracker, issue_number=issue_number, selection_label=selection_label
    )
    try:
        dirty = dirty_working_tree_files(workspace_path)
    except Exception:
        dirty = ()
    if changed_files is not None:
        snapshot_files = list(changed_files)
    else:
        snapshot_files = list(dirty)
    try:
        head = head_revision(workspace_path)
    except GitFailure:
        head = base_revision
    commit = push.remote_sha or head
    composed = compose_terminal_snapshot(
        attempt_id=attempt_id,
        base_revision=base_revision,
        branch=branch_name,
        commit=commit,
        changed_files=snapshot_files,
        commands=[dict(entry) for entry in (commands or [])],
        validation_evidence=[dict(entry) for entry in (validation_evidence or [])],
    )
    snapshot_block: str | None = None
    if composed is not None:
        snapshot_json, snapshot_digest, snapshot_version = composed
        snapshot_block = render_snapshot_block(
            snapshot_json=snapshot_json,
            digest=snapshot_digest,
            version=snapshot_version,
        )
    details: list[str] = []
    if push.foreign:
        details.append(
            "the remote branch held commits this Attempt did not make "
            "(foreign push); nothing was pushed."
        )
    if push.uncommitted_count:
        details.append(
            f"{push.uncommitted_count} uncommitted file(s) were not committed "
            "and were not preserved."
        )
    comment_body = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=outcome,
        reason=reason,
        pull_request_url=None,
        branch_name=branch_name,
        workflow_id=workflow_id,
        configuration_digest=configuration_digest,
        unmet_conditions=(),
        next_action=terminal_next_action(reason),
        detail=" ".join(details) if details else None,
        snapshot_block=snapshot_block,
    )
    ledger.record(
        attempt_id,
        INTERRUPTED_EVENT_KIND,
        _event_budget(ledger, attempt_id),
        detail=f"interrupted finalization: {outcome} ({reason})",
        payload={
            "outcome": outcome,
            "reason": reason,
            "pushed": push.pushed,
            "remote_sha": push.remote_sha,
            "foreign": push.foreign,
            "uncommitted_count": push.uncommitted_count,
            "uncommitted_files": list(dirty)[:MAX_LISTED_UNCOMMITTED_FILES],
        },
    )
    ledger.record_terminal_outcome(
        attempt_id,
        outcome=outcome,
        reason=reason,
        pull_request_body="",
        terminal_comment_body=comment_body,
        writes=terminal_writes_for_outcome(outcome=outcome),
    )
    return InterruptedResult(
        outcome=outcome,
        reason=reason,
        pushed=push.pushed,
        remote_sha=push.remote_sha,
        foreign=push.foreign,
        uncommitted_count=push.uncommitted_count,
    )


def perform_recorded_terminal_writes(
    *, ledger: RunLedger, tracker: TrackerPort, attempt_id: str, issue_number: int
) -> None:
    """Perform the recorded terminal writes, in §14 order, for M8 recovery (§17).

    Reads the outcome :func:`finalize_interrupted_attempt` recorded — the
    comment, the Selection Label removal, ``ready-for-human`` on every outcome
    without a pull request, then the Worker's own assignment removal — and
    performs each once. The live Attempt graph's ``terminal_writes`` node is
    this function's in-graph counterpart; recovery calls this one at startup
    after the shared finalization.
    """
    stored = ledger.get_outcome(attempt_id)
    if stored is None:
        raise ValueError(f"no recorded outcome for {attempt_id}")
    body = stored.terminal_comment_body
    for write_kind in stored.writes:
        if write_kind == "terminal-comment":
            tracker.post_terminal_comment(issue_number, body)
        elif write_kind == "remove-selection-label":
            tracker.remove_selection_label(issue_number)
        elif write_kind == "add-ready-for-human":
            tracker.add_ready_for_human(issue_number)
        elif write_kind == "release-assignment":
            try:
                assigned_by_us = ledger.get_attempt(attempt_id).assigned_by_us is True
            except Exception:
                assigned_by_us = False
            if assigned_by_us:
                tracker.release_assignment(issue_number)
        else:
            raise AssertionError(f"unknown terminal write kind: {write_kind!r}")
