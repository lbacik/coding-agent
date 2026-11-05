"""The Publication Gate verdict, the Delivery Intent and the tail rendering.

Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the Publication
Gate re-reads the Target Issue and decides the verdict; a pull request
requires, in order, the Implementation Outcome ``verified completion``, no
Drift, the review gate, green final checks and a clean tree; the first unmet
condition names the reason and every unmet condition is recorded for the
terminal comment; base-branch movement does not block but is recorded for
disclosure; ``delivery_intent`` records the outcome ``delivered`` as a
Delivery Intent before any delivery write: the Attempt id, branch, head sha,
verdict and the writes the outcome calls for; writing the Intent is the point
of no return), §13 (the write sequence keyed by ``(attempt_id, write_kind)``;
a pull request opens only once, found by head branch plus the Attempt Marker),
§14 (the Implementation Outcome → Terminal Outcome table) and §15 (the pull
request and terminal comment contents, with the Attempt Marker) and §16 (a
rebase conflict ends the Continuation ``failed`` / ``continuation-conflict``,
with one sentence on what a human must do next), and
[ADR 0003](../adr/0003-delivery-intent.md) (the Intent is written before the
first delivery write and a resumption finishes it rather than re-deciding).

MVP row ids: P-DEL-1, P-DEL-2, P-DEL-5, P-DEL-6, P-DEL-7, P-DEL-8,
P-DEL-9, P-DEL-10, C-PUB-1, C-PUB-2, G-TDD-1, L3-DEL-5, L3-DEL-14,
L3-DEL-21, C-OUT-1, L2-17, L2-18, C-LIM-1, P-LIM-1, P-HND-2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from coding_agent.runtime.context_handoff import (
    CONTINUATION_SNAPSHOT_VERSION,
    HandoffFailure,
)
from coding_agent.runtime.footprint import render_footprint_section
from coding_agent.runtime.redaction import Redactable


PUBLICATION_GATE_KIND = "publication_gate"
"""Run Ledger event kind for the Publication Gate verdict (§12)."""

DELIVERY_INTENT_KIND = "delivery_intent"
"""Run Ledger event kind for the Delivery Intent (§12, ADR 0003)."""

PUBLISH_WRITE_KIND = "open-pull-request"
"""The ``effects`` write kind for opening the pull request (§13).

Recorded through the same intent/receipt sequence as every other external
write; the GitHub adapter performs it (M6).
"""

DRIFT_REASON = "drift"
REVIEW_MISSING_REASON = "review-missing"
FAILING_VALIDATION_REASON = "failing-validation"
DIRTY_TREE_REASON = "dirty-tree"

VERIFIED_COMPLETION_REASON = "verified-completion"
WITHDRAWAL_REASON = "withdrawal"
"""The Terminal Outcome reason when a human closed our pull request (§13).

The Attempt ends ``abandoned``: a withdrawal is a human's reversal of our
effect, not an absence — the pull request is never reopened (L3-DEL-14).
"""

TERMINAL_COMMENT_KIND = "terminal"
DELIVERY_COMMENT_KIND = "delivery"

TERMINAL_COMMENT_WRITE_KIND = "terminal-comment"
"""The ``effects``-style write kind for the terminal comment (§14)."""

REMOVE_SELECTION_LABEL_WRITE_KIND = "remove-selection-label"
"""The write kind for removing the Workflow's Selection Label (§14)."""

ADD_READY_FOR_HUMAN_WRITE_KIND = "add-ready-for-human"
"""The write kind for ``ready-for-human`` on every outcome without a PR (§14)."""

RELEASE_ASSIGNMENT_WRITE_KIND = "release-assignment"
"""The write kind for removing the Worker's own assignment (§14)."""

@dataclass(frozen=True)
class PublicationVerdict:
    """The Publication Gate's deterministic verdict (§12).

    ``ready`` is true only when every gate condition holds. ``reason`` is the
    Terminal Outcome reason: ``verified-completion`` when ready, else the
    first unmet condition in the contract order Drift → review → final checks
    → clean tree. ``unmet`` carries every unmet condition for the terminal
    comment. ``base_moved`` records whether the base head differs from the
    Base Revision; it never blocks and the Worker never rebases or merges.
    """

    ready: bool
    reason: str
    unmet: tuple[str, ...]
    base_moved: bool
    base_revision: str
    base_head: str | None


def evaluate_publication_gate(
    *,
    is_verified_completion: bool,
    fingerprint_match: bool,
    review_gate_met: bool,
    final_checks_green: bool,
    dirty_files: tuple[str, ...],
    base_revision: str,
    base_head: str | None,
) -> PublicationVerdict:
    """Decide the Publication Gate verdict from its ordered conditions (§12).

    The order is Drift → review → final checks → clean tree. Base-branch
    movement is compared and recorded but never blocks. Where the
    Implementation Outcome is not ``verified completion`` there is no verdict
    to decide here: the caller passes that through untouched, so this helper
    only runs for ``verified completion`` and ``is_verified_completion`` is
    always true at the call site. It is kept explicit so the ordering proof
    reads the contract order.
    """
    _ = is_verified_completion
    unmet: list[str] = []
    if not fingerprint_match:
        unmet.append(DRIFT_REASON)
    if not review_gate_met:
        unmet.append(REVIEW_MISSING_REASON)
    if not final_checks_green:
        unmet.append(FAILING_VALIDATION_REASON)
    if dirty_files:
        unmet.append(DIRTY_TREE_REASON)
    base_moved = base_head is not None and base_head != base_revision
    if not unmet:
        return PublicationVerdict(
            ready=True,
            reason=VERIFIED_COMPLETION_REASON,
            unmet=(),
            base_moved=base_moved,
            base_revision=base_revision,
            base_head=base_head,
        )
    return PublicationVerdict(
        ready=False,
        reason=unmet[0],
        unmet=tuple(unmet),
        base_moved=base_moved,
        base_revision=base_revision,
        base_head=base_head,
    )


def attempt_marker(attempt_id: str, *, kind: str) -> str:
    """The machine-readable Attempt Marker line (§15)."""
    return f"<!-- coding-agent attempt={attempt_id} kind={kind} -->"


def render_pull_request_body(
    *,
    attempt_id: str,
    issue_number: int,
    workflow_id: str,
    configuration_digest: str,
    base_revision: str,
    base_head: str | None,
    base_moved: bool,
    acceptance_criteria: list[dict[str, str]],
    claimed_tests: dict[str, list[str]],
    criterion_results: dict[str, str],
    ever_failed: dict[str, bool],
    validation_commands: list[dict[str, object]],
    review_status: str,
    review_invocations: int,
    seam_set: list[str],
    working_area: str,
    profile_changed: bool | None,
    footprint_paths: list[str] | None = None,
) -> str:
    """Render the pull-request body once, before any delivery write (§15).

    The body carries, in order: ``Closes #N`` and the Attempt Marker
    (``kind=delivery``); the Attempt id, Workflow id and digest; the Base
    Revision sha and a base-movement note when the base moved; per-criterion
    evidence with red history; Validation Evidence; the Execution Footprint
    (the deduplicated paths changed by executed project code during the
    Attempt, bounded with an explicit truncation marker); the review status
    and the activation count; the Seam Set; the declared working area and a
    note when the issue changed the profile. The full review reports live in
    the Run Ledger ``review_status`` event (§10, §17), never in the body.

    MVP row ids: C-FP-6.
    """
    lines: list[str] = [f"Closes #{issue_number}", attempt_marker(attempt_id, kind=DELIVERY_COMMENT_KIND), ""]
    lines.append(f"Attempt: {attempt_id}")
    lines.append(f"Workflow: {workflow_id} (digest `{configuration_digest}`)")
    lines.append(f"Base Revision: `{base_revision}`")
    if base_moved:
        lines.append(
            f"Base-branch movement: the base has moved since Claim "
            f"(Base Revision `{base_revision}`, base head `{base_head}`); "
            f"this work was implemented and verified against `{base_revision}`. "
            f"No rebase or merge was performed."
        )
    else:
        lines.append("Base-branch movement: none.")
    lines.append("")
    lines.append("## Acceptance Criteria")
    if not acceptance_criteria:
        lines.append("(none recorded)")
    for criterion in acceptance_criteria:
        criterion_id = str(criterion.get("id", ""))
        text = str(criterion.get("text", ""))
        claimed = claimed_tests.get(criterion_id, [])
        result = criterion_results.get(criterion_id, "unknown")
        red = ever_failed.get(criterion_id, False)
        red_note = " (observed failing before this Attempt)" if red else ""
        lines.append(f"- {criterion_id}: {text}")
        lines.append(f"  claimed tests: {claimed!r}; latest evidence: {result}{red_note}")
    lines.append("")
    lines.append("## Validation Evidence")
    if not validation_commands:
        lines.append("(none recorded)")
    for entry in validation_commands:
        lines.append(
            f"- {entry.get('name')}: exit={entry.get('exit_code')} "
            f"executed={entry.get('executed')} failures={entry.get('failure_ids')}"
        )
    lines.append("")
    lines.append(render_footprint_section(list(footprint_paths or [])))
    lines.append("")
    lines.append(f"Review: {review_status} after {review_invocations} invocation(s)")
    lines.append(
        "Full review reports live in the Run Ledger review_status event "
        f"for {attempt_id} (§10, §17), never in this body."
    )
    lines.append("")
    lines.append(f"Seam Set: {seam_set!r}")
    lines.append(f"Working area: `{working_area}`")
    if profile_changed:
        lines.append(
            "Note: the candidate edited `docs/agents/project-profile.yml`; "
            "validation still ran the Validation Contract pinned at the Base Revision."
        )
    return "\n".join(lines)


def render_terminal_comment(
    *,
    attempt_id: str,
    outcome: str,
    reason: str,
    pull_request_url: str | None,
    branch_name: str | None,
    workflow_id: str,
    configuration_digest: str,
    unmet_conditions: tuple[str, ...] = (),
    next_action: str | None = None,
    detail: str | None = None,
    snapshot_block: str | None = None,
) -> str:
    """Render the one terminal comment carrying the Attempt Marker (§15).

    It states the Terminal Outcome and its reason, a link to the pull request
    or to the pushed branch, the Attempt/Workflow/digest pinning, one sentence
    on what a human must do next for a failure, and any reason-specific
    detail: the unmet Publication Gate conditions, the failing baseline
    commands, or a Continuation Snapshot where the Attempt ends without a
    pull request. Exactly one Attempt Marker (``kind=terminal``) is emitted.
    """
    lines: list[str] = [attempt_marker(attempt_id, kind=TERMINAL_COMMENT_KIND), ""]
    if outcome == "delivered" and reason == "verified-completion":
        lines.append(f"Attempt {attempt_id}: PASS {outcome} ({reason})")
    else:
        lines.append(f"Attempt {attempt_id}: {outcome} ({reason})")
    if pull_request_url is not None:
        lines.append(f"Pull request: {pull_request_url}")
    elif branch_name is not None:
        lines.append(f"Branch (no pull request): `{branch_name}`")
    else:
        lines.append("No pull request and no pushed branch.")
    lines.append(f"Workflow: {workflow_id} (digest `{configuration_digest}`)")
    if unmet_conditions:
        lines.append(f"Unmet Publication Gate conditions: {', '.join(unmet_conditions)}")
    if detail:
        lines.append(f"Detail: {detail}")
    if outcome != "delivered":
        lines.append(f"Next: {next_action or 'a human must intervene; see the reason above.'}")
    if snapshot_block is not None:
        lines.append("")
        lines.append(snapshot_block)
    return "\n".join(lines)


def terminal_next_action(reason: str) -> str:
    """One sentence on what a human must do next for each terminal reason (§15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (for a failure,
    one sentence on what a human must do next) and §11 (a crossed ceiling
    ends ``failed-limit`` naming the limit, with no pull request and
    ``ready-for-human``) and §16 (a rebase conflict ends the Continuation
    ``failed`` / ``continuation-conflict`` with ``ready-for-human`` and no
    model call).

    MVP row ids: P-LIM-1, C-LIM-1, P-CAN-1, W-STP-3, G-REV-3, P-HND-2.
    """
    actions: dict[str, str] = {
        DRIFT_REASON: "re-apply the Selection Label after reviewing the Target Issue edit; a Continuation starts from the pushed work.",
        REVIEW_MISSING_REASON: "re-apply the Selection Label; the next Attempt must complete a review before delivery.",
        FAILING_VALIDATION_REASON: "inspect the Validation Evidence in the Run Ledger and fix the failing check or test.",
        DIRTY_TREE_REASON: "inspect the leftover files named in the Validation Evidence and re-apply the Selection Label.",
        "unverified": "inspect the force-accepted claim and its missing evidence, then re-apply the Selection Label.",
        "already-satisfied": "no change was needed; re-apply the Selection Label only for new work.",
        "criteria-uncovered": "cover the explicitly uncovered criteria with tests, then re-apply the Selection Label.",
        "claims-unverified": "name covering tests that Criterion Evidence records as passing, then re-apply the Selection Label.",
        "no-completion-evidence": "declare completion with passing Criterion Evidence for every criterion, then re-apply the Selection Label.",
        "undeclared": "declare completion with passing Criterion Evidence for every criterion, then re-apply the Selection Label.",
        "step-cannot-continue": "read the step justification in the Continuation Snapshot, unblock it, then re-apply the Selection Label.",
        "saved-partial-work": "read the Continuation Snapshot and re-apply the Selection Label to continue from the pushed work.",
        "red-baseline": "fix the failing baseline commands named above so the Base Revision is green, then re-apply the Selection Label.",
        "missing-readiness-fact": "supply the missing Readiness Fact named above, then re-apply the Selection Label.",
        "invalid-profile": "fix the Project Profile at the base branch, then re-apply the Selection Label.",
        "unsupported-environment": "align the profile with the Supported Toolchain Matrix, then re-apply the Selection Label.",
        "abandoned": "re-apply the Selection Label to start a Continuation from the pushed work.",
        "cancelled": "the issue was closed or the Selection Label removed; re-apply the Selection Label to start a Continuation from the pushed work.",
        "withdrawal": "the pull request was closed by a human and will not be reopened; re-apply the Selection Label to start a Continuation from the pushed work.",
        "interrupted": "re-apply the Selection Label to start a Continuation from the pushed work.",
        "branch-moved": "inspect the existing remote branch; re-apply the Selection Label only after resolving which push is yours, otherwise choose a new attempt number.",
        "remote-branch-collision": "choose a new attempt number or inspect the existing remote branch.",
        "continuation-conflict": "resolve the rebase conflict against the current Base Revision, then re-apply the Selection Label; the previous branch was left unchanged.",
        "wall_clock": "the Attempt ran out of wall-clock time; read the Continuation Snapshot and re-apply the Selection Label to continue from the pushed work.",
        "cost": "the Attempt ran out of cost budget; read the Continuation Snapshot and re-apply the Selection Label to continue from the pushed work.",
        "tokens": "the Attempt ran out of token budget; read the Continuation Snapshot and re-apply the Selection Label to continue from the pushed work.",
        "tool_calls": "the Attempt ran out of tool calls; read the Continuation Snapshot and re-apply the Selection Label to continue from the pushed work.",
    }
    stripped = reason
    for marker in ("failed-limit:", "failed-limit ", "failed-limit"):
        if stripped == marker:
            stripped = ""
            break
        if stripped.startswith(marker):
            stripped = stripped[len(marker):].strip(" :")
            break
    for prefix, action in actions.items():
        for candidate in (reason, stripped):
            if candidate == prefix or candidate.startswith(prefix + ":") or candidate.startswith(prefix + " "):
                return action
    return "inspect the Run Ledger for this Attempt and re-apply the Selection Label for a Continuation."


def terminal_writes_for_outcome(*, outcome: str) -> tuple[str, ...]:
    """The §14 terminal writes in order for one Terminal Outcome (§14, C-OUT-1).

    The comment comes first, then the Selection Label removal, then
    ``ready-for-human`` on every outcome without a pull request (``delivered``
    is the only outcome with one), then removal of the Worker's own
    assignment.
    """
    writes = [TERMINAL_COMMENT_WRITE_KIND, REMOVE_SELECTION_LABEL_WRITE_KIND]
    if outcome != "delivered":
        writes.append(ADD_READY_FOR_HUMAN_WRITE_KIND)
    writes.append(RELEASE_ASSIGNMENT_WRITE_KIND)
    return tuple(writes)


def baseline_detail_for_comment(baseline_payload: Mapping[str, object] | None) -> str | None:
    """The reason-specific baseline detail for a ``red-baseline`` comment (§15).

    Names the failing baseline commands and, where evidence provides them,
    the individual failure identifiers — the same facts the ``red-baseline``
    reason string carries, rendered for a human reading the comment.
    """
    if not isinstance(baseline_payload, Mapping):
        return None
    raw_commands = baseline_payload.get("commands")
    if not isinstance(raw_commands, list):
        return None
    failing: list[str] = []
    for entry in raw_commands:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        exit_code = entry.get("exit_code")
        failures = entry.get("failure_ids")
        reported = (
            [str(item) for item in failures]
            if isinstance(failures, list) and failures
            else []
        )
        if exit_code == 0 and not reported:
            continue
        piece = f"{name} (exit {exit_code})"
        if reported:
            piece += f"; failures: {', '.join(sorted(reported))}"
        failing.append(piece)
    bootstrap = baseline_payload.get("bootstrap")
    if isinstance(bootstrap, Mapping) and bootstrap.get("exit_code") != 0:
        failing.append(
            f"bootstrap {bootstrap.get('command')!r} (exit {bootstrap.get('exit_code')})"
        )
    if not failing:
        return None
    return f"failing baseline commands: {'; '.join(failing)}"


def compose_terminal_snapshot(
    *,
    attempt_id: str,
    base_revision: str,
    branch: str,
    commit: str,
    changed_files: list[str],
    commands: list[dict[str, str]],
    validation_evidence: list[dict[str, object]],
    unresolved_questions: list[str] | None = None,
    next_intended_action: str | None = None,
    step_justification: str = "",
    redactions: Redactable = None,
) -> tuple[str, str, int] | None:
    """Compose the terminal Continuation Snapshot deterministically (§15).

    No model call: the block holds the runtime-known facts (the Attempt id,
    the Base Revision, the branch and pushed head, changed files, commands
    and results, Validation Evidence), with unresolved questions and the next
    intended action carried from the latest model-produced snapshot where one
    exists. Composition enforces redaction and the 32 KiB post-redaction cap
    (§11, C-CTX-1); where it fails there is no snapshot and the comment
    carries none. Returns ``(snapshot_json, digest, version)``.
    """
    from coding_agent.runtime.context_handoff import ContinuationSnapshot

    payload: dict[str, object] = {
        "version": CONTINUATION_SNAPSHOT_VERSION,
        "attempt_id": attempt_id,
        "base_revision": base_revision,
        "branch": branch,
        "commit": commit,
        "changed_files": list(changed_files),
        "commands": [dict(command) for command in commands],
        "validation_evidence": list(validation_evidence),
        "unresolved_questions": list(unresolved_questions or []),
        "next_intended_action": next_intended_action
        or "a human continues from the pushed branch",
        "step_justification": step_justification,
    }
    try:
        snapshot = ContinuationSnapshot.from_payload(payload, redactions=redactions)
    except HandoffFailure:
        return None
    return (snapshot.to_json(), snapshot.digest, snapshot.version)


def render_snapshot_block(
    *,
    snapshot_json: str,
    digest: str,
    version: int,
) -> str:
    """Render a terminal Continuation Snapshot as a versioned, digest-carrying block (§15).

    The block holds the snapshot's SHA-256 digest beside its JSON so a
    Continuation can verify what it reads back; the JSON itself is capped at
    32 KiB when composed (§11, C-CTX-1), so the comment carrying it always
    fits.
    """
    return "\n".join(
        [
            "## Continuation Snapshot",
            f"- digest: `{digest}`",
            f"- version: {version}",
            "",
            "```json",
            snapshot_json,
            "```",
        ]
    )
