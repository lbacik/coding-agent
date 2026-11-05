"""The review-snapshot push behind ``activate_code_review``'s in-tool Gate.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (when the model
calls ``activate_code_review`` the runtime passes the in-tool Gate, then
commits the current tree and pushes the Attempt's branch as review snapshot
X; where the tree is unchanged since the last snapshot no new commit is
made and X is kept; the activation records X and returns the installed
``code-review`` skill body byte-identical; it runs no reviewers, carries no
reports and has no invocation limit), §13 (the write sequence: pass the
Gate in front of it; record the intent keyed by
``(attempt_id, write_kind)``; look the remote ref up with ``ls-remote``;
push only when it is absent or behind; record the receipt carrying the
remote sha; a kind that can repeat, the review-snapshot push, carries its
ordinal; push refusals are classified by write kind and target path, never
by error string) and §18 (one branch per Attempt,
``agent/<issue>/<n>-<slug>``; fast-forward only, never force; author and
committer are the id-based noreply address; every commit carries the
``Attempt: #<issue>/<n>`` trailer; unsigned and never squashed; no branch
is ever deleted).

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (credentials reach
git through a credential helper rather than a remote URL, and every failure
message passes through the redaction registry).

MVP row ids: L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10, L3-ERR-11, C-REV-1,
G-REV-1, W-STP-6, P-CAN-1, P-CAN-2, W-STP-3, G-REV-3.

A **Review** is the activation plus the first ``spawn_subagent`` group
after it, in the same work node, provided the working tree at the start of
that group equals X (tracked changes plus untracked, non-ignored files —
the Delivery Snapshot set). A later group, a step boundary or a
Context-Window Handoff before the group leaves the activation without a
Review. A Review is completed when its group holds at least one Sub-agent
and every one ended ``reported``. The review status of §10 is measured
against the latest completed Review: ``not-invoked`` until the first
activation, ``incomplete`` while none completed, ``fresh`` where the latest
completed Review is of the current tree, and ``stale`` where a later
snapshot or a later change to the workspace moved past its X. Either meets
the review gate of §10, §12.
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
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
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
activations produce two ``effects`` rows with distinct ordinals.
"""

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


ATTRIBUTION_PENDING = "pending"
ATTRIBUTION_GROUP = "group"
ATTRIBUTION_NO_GROUP = "no-group"
ATTRIBUTION_TREE_CHANGED = "tree-changed"
"""Attribution results for one review activation (§10).

``pending``: the activation still awaits its first ``spawn_subagent``
group in the same work node. ``group``: the first group after the
activation, in the same work node, started on a tree equal to X, owns the
Review. ``no-group``: no group followed in the same work node — a later
group, a step boundary or a Context-Window Handoff came first, or the
Attempt ended. ``tree-changed``: the first group after the activation
started on a tree that differs from X.
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


@dataclass
class AttributedSubagent:
    """One Sub-agent of a Review's attributed group (§10, §17).

    ``description`` and ``instructions`` are the call's inputs verbatim;
    ``status`` is ``reported`` or ``no-report`` and ``reason`` names why;
    ``report`` is the verbatim report where one was made; ``budget`` is the
    last Attempt-wide budget snapshot as a readable mapping.
    """

    call_id: str
    description: str
    instructions: str
    status: str
    reason: str
    report: str | None = None
    budget: dict[str, object] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        """Whether this Sub-agent made its report (§10)."""
        return self.report is not None


@dataclass
class ReviewActivation:
    """One ``activate_code_review`` call's record (§10).

    ``commit_sha`` is the review snapshot X the activation recorded;
    ``work_node`` is the work node the activation ran in; ``attribution``
    is one of the ``ATTRIBUTION_*`` results; ``group_ordinal`` is the
    Attempt-wide group ordinal of the attributed group, if any; and
    ``subagents`` holds every Sub-agent of that group in call order.
    """

    ordinal: int
    commit_sha: str
    branch_name: str
    work_node: int = 0
    attribution: str = ATTRIBUTION_NO_GROUP
    group_ordinal: int | None = None
    subagents: list[AttributedSubagent] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        """Whether this activation owns a completed Review (§10).

        A Review is completed when its group holds at least one Sub-agent
        and every one ended ``reported``.
        """
        return (
            self.attribution == ATTRIBUTION_GROUP
            and len(self.subagents) >= 1
            and all(subagent.completed for subagent in self.subagents)
        )

    @property
    def cause(self) -> str:
        """Why this activation holds no completed Review (§9, P-DEL-3).

        One of: no attributed group, the tree changed since X, or the
        first ``no-report`` Sub-agent's description and reason.
        """
        if self.attribution == ATTRIBUTION_TREE_CHANGED:
            return "tree changed since X"
        if self.attribution == ATTRIBUTION_GROUP:
            for subagent in self.subagents:
                if not subagent.completed:
                    return (
                        f"Sub-agent {subagent.description!r} ended "
                        f"{subagent.status} ({subagent.reason})"
                    )
            if not self.subagents:
                return "no attributed group"
            return "no attributed group"
        return "no attributed group"


@dataclass
class ReviewSnapshotState:
    """Attempt-wide review-activation and attribution state (contract §10).

    Lives in ``AttemptContext`` (runtime context, never graph state): the
    activations record each pushed X, the pending activation awaits the
    first ``spawn_subagent`` group in its work node, ``current_work_node``
    names the work node the Attempt is running (a step boundary or a
    Context-Window Handoff opens a fresh one), ``claim_review_returned``
    keeps the P-DEL-3 return to once per Attempt, and ``failure`` carries
    a ``workflows-path`` refusal for ``run_step`` to end the Attempt
    ``failed`` / ``workflows-path``. ``status`` is ``not-invoked`` until
    the first activation, ``incomplete`` while no activation owns a
    completed Review, ``fresh`` where the latest completed Review is of
    the current tree, and ``stale`` where a later snapshot or a later
    change to the workspace moved past its X: a ``stale`` review still
    meets the review gate (§10, §12).

    MVP row ids: G-REV-1, C-REV-1, W-STP-6, P-DEL-3.
    """

    activations: list[ReviewActivation] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    """Each activation's X in order, kept beside ``activations``."""

    current_work_node: int = 0
    """The work node the Attempt is running (§10).

    ``run_step`` advances it before every fresh work node, so an
    activation only ever attributes the first group in its own node: a
    step boundary or a Context-Window Handoff before the group leaves the
    activation without a Review.
    """

    pending_ordinal: int | None = None
    """The latest activation still awaiting its first group, if any (§10).

    Only the latest activation can own the next group: a second
    activation supersedes the first, and the first group after an
    activation ends the wait for every activation.
    """

    stopped_at_gate: str | None = None
    """The Gate-stop reason when a Gate ended the Attempt (§11).

    Set by the in-tool Gate path when :func:`check_in_tool_gate` raises:
    the Attempt ends ``abandoned`` / the recorded reason with whatever is
    already pushed kept (G-REV-3). ``run_step`` reads this after the work
    node to end the Attempt at that Gate. MVP row ids: G-REV-3, P-CAN-1.
    """
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
    change after the latest completed review, with no new activation,
    makes the status ``stale``. ``None`` (scripted unit tests) leaves only
    a later snapshot to move a review past ``fresh``. MVP row ids: C-REV-1.
    """

    @property
    def invocations(self) -> int:
        """The activation count (§10, §17)."""
        return len(self.activations)

    @property
    def latest_completed_activation(self) -> ReviewActivation | None:
        """The latest activation owning a completed Review, if any (§10)."""
        return next(
            (
                activation
                for activation in reversed(self.activations)
                if activation.completed
            ),
            None,
        )

    @property
    def status(self) -> str:
        """The review status (§10): ``not-invoked``, ``incomplete``, ``fresh`` or ``stale``.

        Measured against the latest *completed* Review: an activation
        whose group a seat never reported — here, whose Sub-agent ended
        ``no-report`` — never makes the status ``fresh``. The review is
        ``stale`` once a later snapshot or a later change to the workspace
        moved past its X. MVP row ids: C-REV-1, G-REV-1.
        """
        if not self.activations:
            return "not-invoked"
        latest = self.latest_completed_activation
        if latest is None:
            return "incomplete"
        if self.snapshots and self.snapshots[-1] != latest.commit_sha:
            return "stale"
        if self.changed_since is not None and self.changed_since(latest.commit_sha):
            return "stale"
        return "fresh"

    @property
    def review_gate_met(self) -> bool:
        """Whether the Attempt holds at least one completed Review (§10, §12).

        Findings never block; a ``stale`` completed Review still meets the
        gate. MVP row ids: P-DEL-3, P-DEL-5, C-REV-1.
        """
        return self.latest_completed_activation is not None

    def record_activation(
        self, *, commit_sha: str, branch_name: str
    ) -> ReviewActivation:
        """File one ``activate_code_review`` call's X (§10).

        The activation supersedes any still-pending earlier one: only the
        latest activation can own the next group. The new activation starts
        pending in the current work node.
        """
        ordinal = len(self.activations) + 1
        activation = ReviewActivation(
            ordinal=ordinal,
            commit_sha=commit_sha,
            branch_name=branch_name,
            work_node=self.current_work_node,
            attribution=ATTRIBUTION_PENDING,
        )
        self.activations.append(activation)
        self.snapshots.append(commit_sha)
        self.pending_ordinal = ordinal
        return activation

    def close_work_node(self) -> None:
        """End the wait for a group the finished work node never started.

        A step boundary or a Context-Window Handoff before the group
        leaves every pending activation without a Review (§10): the
        activation keeps ``no-group`` and no later group can claim it.
        """
        pending = self._pending_activation()
        if pending is not None and pending.work_node != self.current_work_node:
            pending.attribution = ATTRIBUTION_NO_GROUP
            self.pending_ordinal = None

    def attribute_group(self, *, group_ordinal: int, tree_equal: bool) -> ReviewActivation | None:
        """Attribute one ``spawn_subagent`` group to the pending activation.

        The group is the first ``spawn_subagent`` group after the latest
        activation: where it runs in the same work node and the tree at
        its start equals X, the group owns the activation's Review. Where
        the tree differs, the activation is left with ``tree-changed``.
        A group in a later work node, or with no pending activation,
        attributes nothing. Later groups never re-attribute.
        """
        pending = self._pending_activation()
        if pending is None:
            return None
        if pending.work_node != self.current_work_node:
            pending.attribution = ATTRIBUTION_NO_GROUP
            self.pending_ordinal = None
            return None
        self.pending_ordinal = None
        if not tree_equal:
            pending.attribution = ATTRIBUTION_TREE_CHANGED
            return pending
        pending.attribution = ATTRIBUTION_GROUP
        pending.group_ordinal = group_ordinal
        return pending

    def record_attributed_subagent(
        self,
        activation: ReviewActivation,
        *,
        call_id: str,
        description: str,
        instructions: str,
        status: str,
        reason: str,
        report: str | None,
        budget: dict[str, object] | None = None,
    ) -> AttributedSubagent:
        """File one attributed Sub-agent's end state verbatim (§10, §17)."""
        subagent = AttributedSubagent(
            call_id=call_id,
            description=description,
            instructions=instructions,
            status=status,
            reason=reason,
            report=report,
            budget=dict(budget or {}),
        )
        activation.subagents.append(subagent)
        return subagent

    def review_return_cause(self) -> str:
        """Why the Attempt holds no completed Review (§9, P-DEL-3).

        One of: no activation, no attributed group, the tree changed
        since X, or the ``no-report`` Sub-agent's description and reason.
        """
        if not self.activations:
            return "no activation"
        return self.activations[-1].cause

    def _pending_activation(self) -> ReviewActivation | None:
        """The activation still awaiting its first group, if any."""
        if self.pending_ordinal is None:
            return None
        for activation in self.activations:
            if activation.ordinal == self.pending_ordinal:
                if activation.attribution != ATTRIBUTION_PENDING:
                    return None
                return activation
        return None


def review_disclosure(state: ReviewSnapshotState) -> dict[str, object]:
    """The review status, activation count and every attributed report (§10, §17).

    Findings never block, whatever their severity: they are carried here in
    full, verbatim, for disclosure, and read by nothing that decides an
    outcome. ``activations`` lists every activation's X with its
    attribution result (``group`` / ``no-group`` / ``tree-changed``) and,
    for an attributed group, every Sub-agent's description, end status
    and reason, report, ``instructions`` and last Attempt-wide budget
    snapshot. ``latest_completed_ordinal`` names the latest completed
    Review. MVP row ids: P-DEL-5, C-REV-1.
    """
    latest = state.latest_completed_activation
    return {
        "status": state.status,
        "activations": len(state.activations),
        "invocations": len(state.activations),
        "review_gate_met": state.review_gate_met,
        "latest_completed_ordinal": latest.ordinal if latest is not None else None,
        "reviews": [
            {
                "ordinal": activation.ordinal,
                "commit_sha": activation.commit_sha,
                "branch_name": activation.branch_name,
                "attribution": activation.attribution,
                "group_ordinal": activation.group_ordinal,
                "completed": activation.completed,
                "cause": None if activation.completed else activation.cause,
                "subagents": [
                    {
                        "call_id": subagent.call_id,
                        "description": subagent.description,
                        "status": subagent.status,
                        "reason": subagent.reason,
                        "report": subagent.report,
                        "instructions": subagent.instructions,
                        "budget": dict(subagent.budget),
                    }
                    for subagent in activation.subagents
                ],
            }
            for activation in state.activations
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
    redact: Redactable = None,
    tracker: TrackerPort | None = None,
    selection_label: str | None = None,
    helper: GitCredentialHelper | None = None,
) -> ReviewSnapshotOutcome:
    """Commit the current tree and push the Attempt's branch (§10, §13).

    Runs the write sequence of §13: pass the in-tool Gate — which re-reads
    the issue through ``tracker`` and raises :class:`GateStop` where the
    issue is cancelled (G-REV-3) — record the intent with the sha about to
    be pushed, look the remote ref up with ``ls-remote``, push only when it
    is absent or behind, and record the receipt carrying the remote sha.
    Re-entering a ``write_kind`` whose receipt exists pushes nothing and
    returns the recorded sha.

    Where the tree is unchanged since the last snapshot no new commit is
    made and X is kept: the intent and receipt still record this
    activation's ordinal against the kept sha. Branch identity is
    ``agent/<issue>/<n>-<slug>`` (L3-DEL-19), fast-forward only: no force
    flag is ever passed and no branch is ever deleted. Commit identity is
    the Agent Identity's id-based noreply address for both author and
    committer, with the ``Attempt: #<issue>/<n>`` trailer, unsigned and
    never squashed (L3-DEL-20).

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

    remote_sha = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper)
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
        push_branch(workspace, remote_url, branch_name, redact=redact, helper=helper)
    except GitFailure:
        # Classified by write kind and target path, never by error string:
        # any push rejection for this write kind (missing signatures,
        # or any other transport refusal) ends in the same permanent class
        # as a Workflows-path refusal (L3-ERR-10).
        raise ReviewSnapshotRefused(write_kind, branch_name) from None

    after = remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper) or local_sha
    ledger.record_effect_receipt(
        attempt_id, write_kind, remote_id=after, payload={**payload, "remote_sha": after}
    )
    return ReviewSnapshotOutcome(
        branch_name=branch_name, commit_sha=local_sha, remote_sha=after, pushed=True
    )


def workspace_from_path(path: Path, *, base_branch: str = "main", base_revision: str = "") -> Workspace:
    """Wrap a bare path as a :class:`Workspace` for scripted tests."""
    revision = base_revision or head_revision(path)
    return Workspace(path=path, base_branch=base_branch, base_revision=base_revision)
