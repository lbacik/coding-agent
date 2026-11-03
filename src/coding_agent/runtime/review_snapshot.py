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
G-REV-1, W-STP-6.

Until the reviewers land, the activation result returns the pushed sha and
states that no review ran, and the review status stays unmet (``incomplete``
once invoked, so the review gate of §10 and §12 is not met).
"""

from __future__ import annotations

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


def check_in_tool_gate() -> None:
    """The in-tool Gate in front of the review-snapshot push (§10, §11).

    A pass-through until M7 adds its stop signals: cancellation, SIGTERM
    and ceiling stops detected at the Gate before the push end the Attempt
    there, keeping whatever is already pushed (G-REV-3).
    """
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
    first call, ``incomplete`` while no invocation completed, ``fresh``
    where the latest completed review is of the latest pushed snapshot, and
    ``stale`` where a later snapshot moved past it: a ``stale`` review still
    meets the review gate (§10, §12).

    MVP row ids: G-REV-1, C-REV-1, W-STP-6, P-DEL-4.
    """

    invocations: int = 0
    snapshots: list[str] = field(default_factory=list)
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

    @property
    def status(self) -> str:
        """The review status (§10): ``not-invoked``, ``incomplete``, ``fresh`` or ``stale``."""
        if self.invocations == 0:
            return "not-invoked"
        if not self.completed_reviews:
            return "incomplete"
        latest = self.completed_reviews[-1]
        if self.snapshots and latest.commit_sha == self.snapshots[-1]:
            return "fresh"
        return "stale"

    @property
    def review_gate_met(self) -> bool:
        """Whether the Attempt holds at least one completed review (§10, §12).

        Findings never block; a ``stale`` completed review still meets the
        gate. MVP row ids: P-DEL-3, P-DEL-5, C-REV-1.
        """
        return any(review.completed for review in self.completed_reviews)

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
) -> ReviewSnapshotOutcome:
    """Commit the current tree and push the Attempt's branch (§10, §13).

    Runs the write sequence of §13: pass the in-tool Gate (a pass-through
    until M7), record the intent with the sha about to be pushed, look the
    remote ref up with ``ls-remote``, push only when it is absent or behind,
    and record the receipt carrying the remote sha. Re-entering a
    ``write_kind`` whose receipt exists pushes nothing and returns the
    recorded sha.

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
    check_in_tool_gate()
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
