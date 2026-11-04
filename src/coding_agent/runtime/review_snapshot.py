"""The review-snapshot push behind ``activate_code_review``'s in-tool Gate.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (when the model
calls ``activate_code_review`` the runtime commits the current tree and
pushes the Attempt's branch before any reviewer starts), §13 (the write
sequence: pass the Gate in front of it; record the intent keyed by
``(attempt_id, write_kind)``; look the remote ref up with ``ls-remote``;
push only when it is absent or behind; record the receipt carrying the
remote sha; a kind that can repeat, the review-snapshot push, carries its
ordinal; push refusals are classified by write kind and target path, never
by error string) and §18 (one branch per Attempt,
``agent/<issue>/<n>-<slug>``; fast-forward only, never force; author and
committer are the id-based noreply address; every commit carries the
``Attempt: #<issue>/<n>`` trailer; unsigned and never squashed; no branch
is ever deleted).

MVP row ids: L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10, P-DEL-4, C-REV-1,
G-REV-1, W-STP-6, P-CAN-1, P-CAN-2, W-STP-3, G-REV-3.

The review status of §10 is measured against the latest completed review
(both seats reported): ``incomplete`` until one exists, then ``fresh`` or
``stale`` depending on whether a later snapshot or a later change to the
workspace moved past its commit. Either meets the review gate of §10, §12.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from coding_agent.identity.commit_identity import (
    attempt_branch_name,
    noreply_author,
    review_snapshot_message,
)
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.gates import check_cancellation, check_shutdown
from coding_agent.tracker.port import TrackerPort
from coding_agent.runtime.git import (
    GitFailure,
    Workspace,
    commit_delivery_snapshot,
    commit_touches_workflows_path,
    ensure_attempt_branch,
    has_uncommitted_changes,
    head_revision,
    push_branch,
    remote_ref_sha,
    tree_touches_workflows_path,
)

REVIEW_SNAPSHOT_KIND_PREFIX = "review-snapshot-push/"
"""``write_kind`` prefix for the review-snapshot push (§13).

The kind carries its ordinal (``review-snapshot-push/1``,
``review-snapshot-push/2``, …), unique within an Attempt, so two
invocations produce two ``effects`` rows with distinct ordinals.
"""

MAX_REVIEW_INVOCATIONS = 3
"""At most three ``activate_code_review`` invocations per Attempt (§10)."""

WORKFLOWS_PATH = ".github/workflows/"
WORKFLOWS_FAILURE_REASON = "workflows-path"
"""The permanent Terminal Outcome reason for both push refusals (§13, §14).

A commit touching ``.github/workflows/`` is refused by the Worker itself,
and a push rejected for missing signatures ends in the same permanent
class. Both are classified by write kind and target path, never by error
string (L3-ERR-9, L3-ERR-10).
"""


REVIEW_STATUS_KIND = "review_status"
"""Run Ledger event kind for the Attempt's review disclosure (§10, §15).

Recorded once per Attempt by ``terminal_writes``, before the Terminal
Outcome and any tracker write, so the pull-request body is rendered from a
durable record rather than from memory. MVP row ids: P-DEL-5.
"""


def review_snapshot_write_kind(ordinal: int) -> str:
    """The ``effects`` write kind for the ``ordinal``-th review snapshot."""
    return f"{REVIEW_SNAPSHOT_KIND_PREFIX}{ordinal}"


def next_review_snapshot_ordinal(ledger: RunLedger, attempt_id: str) -> int:
    """The next review-snapshot ordinal for ``attempt_id`` (1-based)."""
    count = sum(
        1
        for effect in ledger.list_effects(attempt_id)
        if effect.write_kind.startswith(REVIEW_SNAPSHOT_KIND_PREFIX)
    )
    return count + 1


def check_in_tool_gate(
    *,
    tracker: TrackerPort | None = None,
    issue_number: int | None = None,
    selection_label: str | None = None,
) -> None:
    """The in-tool Gate in front of the review-snapshot push (§10, §11).

    Re-reads the issue's open state and labels through the port and checks
    for cancellation: a stop found here raises :class:`GateStop` and ends
    the Attempt at this Gate, keeping whatever is already pushed (G-REV-3).
    Cancellation is checked first and keeps precedence: a concurrently
    cancelled issue still ends ``abandoned`` / ``cancelled``, while SIGTERM
    alone ends ``failed`` / ``interrupted``.
    With no tracker bound (the unit-test seam) it stays a pass-through.

    MVP row ids: P-CAN-1, P-CAN-2, P-CAN-3, W-STP-3, G-REV-3, REC-SIG-1.
    """
    if tracker is None or issue_number is None or selection_label is None:
        return None
    check_cancellation(
        tracker=tracker, issue_number=issue_number, selection_label=selection_label
    )
    check_shutdown()
    return None


@dataclass(frozen=True)
class CompletedReview:
    """One invocation's completed-review record (contract §10).

    ``standards_findings`` and ``spec_findings`` hold each seat's Review
    Report where that seat called ``report_review`` with a non-blank report,
    else ``None``. An invocation is a completed review only when both seats
    reported. ``usage`` is the two reviewer conversations' Attempt-wide
    meter spend (model usage plus reviewer tool calls, with no reserved
    review budget).

    MVP row ids: G-REV-1, C-REV-1, W-STP-6.
    """

    ordinal: int
    commit_sha: str
    branch_name: str
    standards_findings: str | None
    spec_findings: str | None
    usage: UsageTotals = field(default_factory=UsageTotals)

    @property
    def completed(self) -> bool:
        """Whether both seats made a Review Report (§10)."""
        return self.standards_findings is not None and self.spec_findings is not None


@dataclass
class ReviewSnapshotState:
    """Attempt-wide review-snapshot and invocation state (contract §10).

    Lives in ``AttemptContext`` (runtime context, never graph state):
    the invocation count bounds the Attempt at three, the snapshots record
    each pushed sha, ``completed_reviews`` records every invocation's seat
    reports, ``pending_review_usage`` stages reviewer meter spend for
    ``run_step`` to fold into the Attempt-wide meters, and ``failure``
    carries a ``workflows-path`` refusal for ``run_step`` to end the Attempt
    ``failed`` / ``workflows-path``. ``status`` is ``not-invoked`` until the
    first call, ``incomplete`` while no invocation had both seats report,
    ``fresh`` where the latest completed review is of the current tree, and
    ``stale`` where a later snapshot or a later change to the workspace
    moved past it: a ``stale`` review still meets the review gate (§10, §12).

    MVP row ids: G-REV-1, C-REV-1, W-STP-6, P-DEL-4.
    """

    invocations: int = 0
    snapshots: list[str] = field(default_factory=list)
    stopped_at_gate: str | None = None
    """The Gate-stop reason when a Gate ended the Attempt (§11).

    Set by the in-tool Gate path when :func:`check_in_tool_gate` raises:
    the Attempt ends ``abandoned`` / the recorded reason with whatever is
    already pushed kept (G-REV-3). ``run_step`` reads this after the work
    node to end the Attempt at that Gate. MVP row ids: G-REV-3, P-CAN-1.
    """
    completed_reviews: list[CompletedReview] = field(default_factory=list)
    pending_review_usage: UsageTotals = field(default_factory=UsageTotals)
    failure: str | None = None
    failure_write_kind: str | None = None
    failure_target: str | None = None
    claim_review_returned: bool = False
    """Whether a Completion Claim was already returned for a missing review.

    The review return happens once per Attempt (§9 claim check 2), so it
    lives here, Attempt-wide, rather than on the work node's
    ``ClaimRecord``, which a handoff or a fresh step replaces.
    MVP row ids: P-DEL-3.
    """
    changed_since: Callable[[str], bool] | None = None
    """Whether the reviewed workspace now differs from a commit (§10).

    Bound by ``activate_code_review`` to the workspace it snapshots, so a
    change after the latest completed review, with no new invocation, makes
    the status ``stale``. ``None`` (scripted unit tests) leaves only a later
    snapshot to move a review past ``fresh``. MVP row ids: C-REV-1.
    """

    @property
    def latest_completed_review(self) -> CompletedReview | None:
        """The latest invocation both seats reported on, if any (§10)."""
        return next(
            (review for review in reversed(self.completed_reviews) if review.completed),
            None,
        )

    @property
    def status(self) -> str:
        """The review status (§10): ``not-invoked``, ``incomplete``, ``fresh`` or ``stale``.

        Measured against the latest *completed* review: an invocation where
        a seat made no report never makes the status ``fresh``. The review is
        ``stale`` once a later snapshot or a later change to the workspace
        moved past its commit. MVP row ids: C-REV-1, G-REV-1.
        """
        if self.invocations == 0:
            return "not-invoked"
        latest = self.latest_completed_review
        if latest is None:
            return "incomplete"
        if self.snapshots and self.snapshots[-1] != latest.commit_sha:
            return "stale"
        if self.changed_since is not None and self.changed_since(latest.commit_sha):
            return "stale"
        return "fresh"

    @property
    def review_gate_met(self) -> bool:
        """Whether the Attempt holds at least one completed review (§10, §12).

        Findings never block; a ``stale`` completed review still meets the
        gate. MVP row ids: P-DEL-3, P-DEL-5, C-REV-1.
        """
        return self.latest_completed_review is not None

    def record_completed_review(self, review: CompletedReview) -> None:
        """File one invocation's seat reports and stage its meter spend.

        The reviewer conversations' ``usage`` accumulates in
        ``pending_review_usage`` for ``run_step`` to fold into the
        Attempt-wide meters; ``take_pending_review_usage`` consumes it.
        """
        self.completed_reviews.append(review)
        pending = self.pending_review_usage
        self.pending_review_usage = UsageTotals(
            tokens=pending.tokens + review.usage.tokens,
            effective_tokens=pending.effective_tokens + review.usage.effective_tokens,
            cost_usd=pending.cost_usd + review.usage.cost_usd,
            tool_calls=pending.tool_calls + review.usage.tool_calls,
        )

    def take_pending_review_usage(self) -> UsageTotals:
        """Consume the reviewer meter spend not yet folded into the Attempt."""
        pending = self.pending_review_usage
        self.pending_review_usage = UsageTotals()
        return pending


def review_disclosure(state: ReviewSnapshotState) -> dict[str, object]:
    """The review status, invocation count and every Review Report (§10, §15).

    Findings never block, whatever their severity: they are carried here in
    full, verbatim, for disclosure, and read by nothing that decides an
    outcome. ``reviews`` lists every invocation that ran its seats, completed
    or not; ``latest_completed_ordinal`` names the one whose two reports the
    pull-request body shows. MVP row ids: P-DEL-5, C-REV-1.
    """
    latest = state.latest_completed_review
    return {
        "status": state.status,
        "invocations": state.invocations,
        "review_gate_met": state.review_gate_met,
        "latest_completed_ordinal": latest.ordinal if latest is not None else None,
        "reviews": [
            {
                "ordinal": review.ordinal,
                "commit_sha": review.commit_sha,
                "branch_name": review.branch_name,
                "completed": review.completed,
                "standards_report": review.standards_findings,
                "spec_report": review.spec_findings,
            }
            for review in state.completed_reviews
        ],
    }


@dataclass(frozen=True)
class ReviewSnapshotOutcome:
    """The result of one review-snapshot push (§10, §13)."""

    branch_name: str
    commit_sha: str
    remote_sha: str
    write_kind: str
    pushed: bool


class ReviewSnapshotRefused(Exception):
    """A review-snapshot push refusal, classified by kind and path (§13).

    Carries the ``(write_kind, target_path)`` classification rather than an
    error string: ``.github/workflows/`` for a tree touching Workflows, the
    Attempt branch otherwise. Both refusals end the Attempt ``failed`` /
    ``workflows-path``, non-transient (L3-ERR-9, L3-ERR-10).
    """

    def __init__(self, write_kind: str, target_path: str) -> None:
        super().__init__(
            f"review snapshot {write_kind} refused: {WORKFLOWS_FAILURE_REASON} ({target_path})"
        )
        self.write_kind = write_kind
        self.target_path = target_path
        self.reason = WORKFLOWS_FAILURE_REASON


def publish_review_snapshot(
    *,
    workspace: Workspace,
    ledger: RunLedger,
    attempt_id: str,
    issue_number: int,
    attempt_number: int,
    issue_title: str,
    identity: Identity,
    remote_url: str,
    ordinal: int | None = None,
    redact: str | None = None,
    tracker: TrackerPort | None = None,
    selection_label: str | None = None,
) -> ReviewSnapshotOutcome:
    """Commit the current tree and push the Attempt's branch (§10, §13).

    Runs the write sequence of §13: pass the in-tool Gate — which re-reads
    the issue through ``tracker`` and raises :class:`GateStop` where the
    issue is cancelled (G-REV-3) — record the intent with the sha about to
    be pushed, look the remote ref up with ``ls-remote``, push only when it
    is absent or behind, and record the receipt carrying the remote sha.
    Re-entering a ``write_kind`` whose receipt exists pushes nothing and
    returns the recorded sha.

    Branch identity is ``agent/<issue>/<n>-<slug>`` (L3-DEL-19),
    fast-forward only: no force flag is ever passed and no branch is ever
    deleted. Commit identity is the Agent Identity's id-based noreply
    address for both author and committer, with the ``Attempt:
    #<issue>/<n>`` trailer, unsigned and never squashed (L3-DEL-20).

    Push refusals are classified by write kind and target path, never by
    error string: a tree touching ``.github/workflows/`` is refused by the
    Worker itself, and a push rejected for missing signatures ends in the
    same permanent class (L3-ERR-9, L3-ERR-10). Both raise
    :class:`ReviewSnapshotRefused` with reason ``workflows-path``.
    """
    check_in_tool_gate(
        tracker=tracker, issue_number=issue_number, selection_label=selection_label
    )
    resolved_ordinal = ordinal if ordinal is not None else next_review_snapshot_ordinal(
        ledger, attempt_id
    )
    write_kind = review_snapshot_write_kind(resolved_ordinal)
    branch_name = attempt_branch_name(issue_number, attempt_number, issue_title)

    existing = ledger.get_effect(attempt_id, write_kind)
    if existing is not None and existing.status == "receipt":
        sha = existing.remote_id or ""
        return ReviewSnapshotOutcome(
            branch_name=branch_name,
            commit_sha=sha,
            remote_sha=sha,
            write_kind=write_kind,
            pushed=False,
        )

    if tree_touches_workflows_path(workspace.path):
        raise ReviewSnapshotRefused(write_kind, WORKFLOWS_PATH)

    ensure_attempt_branch(workspace, branch_name)
    if has_uncommitted_changes(workspace):
        author_name, author_email = noreply_author(identity)
        message = review_snapshot_message(issue_number, issue_title, attempt_number)
        commit_delivery_snapshot(
            workspace, message=message, author_name=author_name, author_email=author_email
        )
        if commit_touches_workflows_path(workspace.path):
            raise ReviewSnapshotRefused(write_kind, WORKFLOWS_PATH)

    local_sha = head_revision(workspace.path)
    payload: dict[str, object] = {
        "branch": branch_name,
        "commit_sha": local_sha,
        "attempt_id": attempt_id,
    }
    ledger.record_effect_intent(
        attempt_id, write_kind, remote_id=local_sha, payload=payload
    )

    remote_sha = remote_ref_sha(remote_url, branch_name, redact=redact)
    if remote_sha == local_sha:
        receipt = ledger.record_effect_receipt(
            attempt_id, write_kind, remote_id=local_sha, payload=payload
        )
        kept = receipt.remote_id or local_sha
        return ReviewSnapshotOutcome(
            branch_name=branch_name,
            commit_sha=local_sha,
            remote_sha=kept,
            write_kind=write_kind,
            pushed=False,
        )

    try:
        push_branch(workspace, remote_url, branch_name, redact=redact)
    except GitFailure:
        # Classified by write kind and target path, never by error string:
        # any push rejection for this write kind (missing signatures,
        # or any other transport refusal) ends in the same permanent class
        # as a Workflows-path refusal (L3-ERR-10).
        raise ReviewSnapshotRefused(write_kind, branch_name) from None

    after = remote_ref_sha(remote_url, branch_name, redact=redact) or local_sha
    ledger.record_effect_receipt(
        attempt_id, write_kind, remote_id=after, payload={**payload, "remote_sha": after}
    )
    return ReviewSnapshotOutcome(
        branch_name=branch_name,
        commit_sha=local_sha,
        remote_sha=after,
        write_kind=write_kind,
        pushed=True,
    )


def workspace_from_path(path: Path, *, base_branch: str = "main", base_revision: str = "") -> Workspace:
    """Wrap a bare path as a :class:`Workspace` for scripted tests."""
    revision = base_revision or head_revision(path)
    return Workspace(path=path, base_branch=base_branch, base_revision=revision)
